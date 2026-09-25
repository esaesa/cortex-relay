from __future__ import annotations

import os
import threading
import time

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex_relay.core.models import TaskBudget, TaskSpec
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.runtime.agent_events import normalize_agent_events
from cortex_relay.runtime.progress import normalize_progress


class AgentService:
    """Provider-neutral control surface for live or resumable agent sessions."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        max_workers: int = 32,
        lease_ttl_seconds: float = 30.0,
        lease_heartbeat_seconds: float = 5.0,
    ) -> None:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        if not 0 < lease_heartbeat_seconds < lease_ttl_seconds:
            raise ValueError("lease_heartbeat_seconds must be positive and less than lease_ttl_seconds")

        self.registry = registry
        self.store = registry.agent_store
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cortex-agent",
        )
        self._futures: dict[str, Future[Any]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._lease_ttl_seconds = lease_ttl_seconds
        self._lease_heartbeat_seconds = lease_heartbeat_seconds
        self._lease_owners: dict[str, str] = {}
        self._lease_shutdown = threading.Event()

        # Recover direct sessions whose owning process disappeared before this
        # service started. Only sessions with an expired durable lease are touched.
        try:
            self.store.reconcile_expired()
        except (OSError, ValueError):
            pass

        self._lease_thread = threading.Thread(
            target=self._lease_loop,
            name="cortex-agent-lease",
            daemon=True,
        )
        self._lease_thread.start()

    def shutdown(self, *, wait: bool = True) -> None:
        """Release direct-agent worker threads and durable leases."""
        self.executor.shutdown(wait=wait, cancel_futures=True)
        self._lease_shutdown.set()
        self._lease_thread.join(timeout=max(1.0, self._lease_heartbeat_seconds * 2))
        with self._lock:
            leased = list(self._lease_owners.items())
            self._lease_owners.clear()
        for session_id, owner_id in leased:
            try:
                self.store.release_lease(session_id, owner_id)
            except (OSError, ValueError):
                pass

    def _lease_loop(self) -> None:
        while not self._lease_shutdown.wait(self._lease_heartbeat_seconds):
            with self._lock:
                leased = list(self._lease_owners.items())
            for session_id, owner_id in leased:
                try:
                    alive = self.store.heartbeat(
                        session_id,
                        owner_id,
                        ttl_seconds=self._lease_ttl_seconds,
                    )
                except (OSError, ValueError):
                    alive = False
                if not alive:
                    with self._lock:
                        if self._lease_owners.get(session_id) == owner_id:
                            self._lease_owners.pop(session_id, None)

    def _renew_lease_on_progress(self, session_id: str) -> None:
        """Refresh durable ownership when the provider emits meaningful progress."""
        with self._lock:
            owner_id = self._lease_owners.get(session_id)
        renewed = False
        if owner_id is not None:
            try:
                renewed = self.store.heartbeat(
                    session_id,
                    owner_id,
                    ttl_seconds=self._lease_ttl_seconds,
                )
            except (OSError, ValueError):
                renewed = False
        try:
            self.store.merge_metadata(
                session_id,
                {
                    "last_progress_at": time.time(),
                    "lease_renewed_by_progress": renewed,
                },
            )
        except (OSError, ValueError):
            pass

    def _begin_lease(self, session_id: str) -> str:
        owner_id = f"{os.getpid()}:{threading.get_ident()}:{uuid4().hex}"
        acquired = self.store.acquire_lease(
            session_id,
            owner_id,
            ttl_seconds=self._lease_ttl_seconds,
            owner_pid=os.getpid(),
        )
        if not acquired:
            raise ValueError(
                f"agent session {session_id} already has an active turn owner"
            )
        with self._lock:
            prior = self._lease_owners.get(session_id)
            if prior is not None and prior != owner_id:
                self.store.release_lease(session_id, owner_id)
                raise ValueError(
                    f"agent session {session_id} is already executing in this runtime"
                )
            self._lease_owners[session_id] = owner_id
        return owner_id

    def _end_lease(self, session_id: str, owner_id: str) -> None:
        # Release durable ownership before dropping the in-process ownership
        # marker. wait() uses that marker as its completion barrier.
        try:
            self.store.release_lease(session_id, owner_id)
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                if self._lease_owners.get(session_id) == owner_id:
                    self._lease_owners.pop(session_id, None)

    def __enter__(self) -> "AgentService":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown(wait=True)

    def _resolve_access(
        self,
        *,
        objective: str,
        role: str,
        profile: str | None,
        preset: str | None,
        provider: str,
        workspace: Path,
        access: str,
        reasoning: str,
        model: str | None,
        timeout_seconds: int,
    ) -> str:
        if access in {"read_only", "workspace_write"}:
            return access
        if access != "auto":
            raise ValueError("access must be auto, read_only or workspace_write")

        probe = TaskSpec(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=workspace,
            access="read_only",
            reasoning=reasoning,
            model=model,
            timeout_seconds=timeout_seconds,
            isolate_write=False,
        )
        config = self.registry.profiles.load(workspace)
        selected = config.profile_for_task(probe)
        return selected.access if selected is not None else "read_only"

    def _build_task(
        self,
        *,
        objective: str,
        role: str,
        profile: str | None,
        preset: str | None,
        provider: str,
        workspace: str | Path,
        access: str,
        reasoning: str,
        model: str | None,
        timeout_seconds: int,
        parent_session_id: str | None,
        max_tool_calls: int | None = None,
        max_repeated_calls: int | None = None,
        max_idle_seconds: int | None = None,
        max_runtime_seconds: int | None = None,
        max_child_agents: int | None = None,
        on_started: Any = None,
    ) -> TaskSpec:
        if not objective.strip():
            raise ValueError("objective must not be empty")
        if timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")

        resolved_workspace = Path(workspace).expanduser().resolve()
        resolved_access = self._resolve_access(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=resolved_workspace,
            access=access,
            reasoning=reasoning,
            model=model,
            timeout_seconds=timeout_seconds,
        )
        metadata: dict[str, Any] = {
            "_agent_progress": self._renew_lease_on_progress,
        }
        if parent_session_id is not None:
            parent = self.store.get(parent_session_id)
            if parent.workspace.resolve() != resolved_workspace:
                raise ValueError("parent agent session must use the same workspace")
            metadata["_parent_agent_session_id"] = parent_session_id
        if callable(on_started):
            metadata["_agent_session_started"] = on_started

        return TaskSpec(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=resolved_workspace,
            access=resolved_access,  # type: ignore[arg-type]
            reasoning=reasoning,
            model=model,
            timeout_seconds=(
                min(timeout_seconds, max_runtime_seconds)
                if max_runtime_seconds is not None
                else timeout_seconds
            ),
            isolate_write=False,
            budget=TaskBudget(
                max_tool_calls=max_tool_calls,
                max_repeated_calls=max_repeated_calls,
                max_idle_seconds=max_idle_seconds,
                max_runtime_seconds=max_runtime_seconds,
                max_child_agents=max_child_agents,
            ),
            metadata=metadata,
        )

    def start(
        self,
        *,
        objective: str,
        role: str = "reviewer",
        profile: str | None = None,
        preset: str | None = None,
        provider: str = "auto",
        workspace: str | Path = ".",
        access: str = "auto",
        reasoning: str = "high",
        model: str | None = None,
        timeout_seconds: int = 300,
        parent_session_id: str | None = None,
        max_tool_calls: int | None = None,
        max_repeated_calls: int | None = None,
        max_idle_seconds: int | None = None,
        max_runtime_seconds: int | None = None,
        max_child_agents: int | None = None,
    ) -> dict[str, Any]:
        """Start a direct persistent provider-backed agent session and await its first turn."""
        holder: dict[str, str] = {}
        cancel_event = threading.Event()

        def on_started(session_id: str) -> None:
            holder["session_id"] = session_id
            try:
                owner_id = self._begin_lease(session_id)
            except Exception as exc:
                holder["lease_error"] = str(exc)
                cancel_event.set()
                return
            holder["owner_id"] = owner_id
            with self._lock:
                self._cancel_events[session_id] = cancel_event

        task = self._build_task(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=workspace,
            access=access,
            reasoning=reasoning,
            model=model,
            timeout_seconds=timeout_seconds,
            parent_session_id=parent_session_id,
            max_tool_calls=max_tool_calls,
            max_repeated_calls=max_repeated_calls,
            max_idle_seconds=max_idle_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_child_agents=max_child_agents,
            on_started=on_started,
        )
        task = replace(
            task,
            metadata={**task.metadata, "_cancel_event": cancel_event},
        )
        try:
            result = self.registry.start_agent(task)
        finally:
            session_id = holder.get("session_id")
            owner_id = holder.get("owner_id")
            if session_id and owner_id:
                self._end_lease(session_id, owner_id)
            if session_id:
                with self._lock:
                    self._cancel_events.pop(session_id, None)

        payload = result.to_dict()
        session_id = result.metadata.get("agent_session_id")
        if isinstance(session_id, str):
            payload["agent_session_id"] = session_id
            payload["session"] = self.store.get(session_id).to_dict()
        if holder.get("lease_error"):
            payload["control_error"] = holder["lease_error"]
        return payload

    def start_async(
        self,
        *,
        objective: str,
        role: str = "reviewer",
        profile: str | None = None,
        preset: str | None = None,
        provider: str = "auto",
        workspace: str | Path = ".",
        access: str = "auto",
        reasoning: str = "high",
        model: str | None = None,
        timeout_seconds: int = 300,
        parent_session_id: str | None = None,
        max_tool_calls: int | None = None,
        max_repeated_calls: int | None = None,
        max_idle_seconds: int | None = None,
        max_runtime_seconds: int | None = None,
        max_child_agents: int | None = None,
    ) -> dict[str, Any]:
        """Start a direct persistent session concurrently and return its session ID.

        The Cortex session is created before provider execution proceeds far enough
        to emit observable work, allowing immediate agent_events/agent_wait usage.
        No workflow task record is created.
        """
        started = threading.Event()
        holder: dict[str, str] = {}
        cancel_event = threading.Event()

        def on_started(session_id: str) -> None:
            try:
                owner_id = self._begin_lease(session_id)
            except Exception as exc:
                holder["lease_error"] = str(exc)
                holder["session_id"] = session_id
                cancel_event.set()
                started.set()
                return
            holder["session_id"] = session_id
            holder["owner_id"] = owner_id
            with self._lock:
                self._cancel_events[session_id] = cancel_event
            started.set()

        task = self._build_task(
            objective=objective,
            role=role,
            profile=profile,
            preset=preset,
            provider=provider,
            workspace=workspace,
            access=access,
            reasoning=reasoning,
            model=model,
            timeout_seconds=timeout_seconds,
            parent_session_id=parent_session_id,
            max_tool_calls=max_tool_calls,
            max_repeated_calls=max_repeated_calls,
            max_idle_seconds=max_idle_seconds,
            max_runtime_seconds=max_runtime_seconds,
            max_child_agents=max_child_agents,
            on_started=on_started,
        )
        task = TaskSpec(
            **{
                **task.__dict__,
                "metadata": {**task.metadata, "_cancel_event": cancel_event},
            }
        )
        future = self.executor.submit(self.registry.start_agent, task)

        while not started.wait(0.01):
            if future.done():
                result = future.result()
                payload = result.to_dict()
                session_id = result.metadata.get("agent_session_id")
                if isinstance(session_id, str):
                    payload["agent_session_id"] = session_id
                    payload["session"] = self.store.get(session_id).to_dict()
                return payload

        session_id = holder["session_id"]
        lease_error = holder.get("lease_error")
        if lease_error:
            def _cleanup_failed_start(_future: Future[Any]) -> None:
                with self._lock:
                    self._cancel_events.pop(session_id, None)
            future.add_done_callback(_cleanup_failed_start)
            raise ValueError(lease_error)
        owner_id = holder.get("owner_id")
        with self._lock:
            self._futures[session_id] = future

        def _forget(_future: Future[Any]) -> None:
            with self._lock:
                self._futures.pop(session_id, None)
                self._cancel_events.pop(session_id, None)
            if owner_id:
                self._end_lease(session_id, owner_id)

        future.add_done_callback(_forget)
        session = self.store.get(session_id).to_dict()
        return {
            "agent_session_id": session_id,
            "status": session["state"],
            "session": session,
            "workflow_task": False,
        }

    def wait(
        self,
        session_id: str,
        *,
        timeout_seconds: float = 0,
    ) -> dict[str, Any]:
        """Poll or briefly wait for a direct agent turn to finish."""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = time.monotonic() + min(timeout_seconds, 5.0)
        poll_delay = 0.02

        while True:
            session = self.store.get(session_id).to_dict()
            complete = session["state"] not in {"starting", "running"}
            if complete:
                # A provider thread may still be unwinding after it publishes the
                # terminal session state. Join it within the caller's remaining
                # wait budget so durable result metadata and filesystem handles are
                # settled before returning.
                with self._lock:
                    future = self._futures.get(session_id)
                if future is not None and not future.done() and timeout_seconds > 0:
                    remaining = max(0.0, deadline - time.monotonic())
                    try:
                        future.result(timeout=remaining)
                    except FutureTimeoutError:
                        pass
                # A Future is considered done before its callbacks necessarily
                # finish. Wait for our local durable-lease cleanup barrier too so
                # terminal wait means no turn-owned state handle is still unwinding.
                if timeout_seconds > 0:
                    while time.monotonic() < deadline:
                        with self._lock:
                            lease_cleanup_pending = session_id in self._lease_owners
                        if not lease_cleanup_pending:
                            break
                        time.sleep(0.01)
                session = self.store.get(session_id).to_dict()
                return {
                    "agent_session_id": session_id,
                    "status": session["state"],
                    "complete": True,
                    "session": session,
                    "result": self.store.result(session_id),
                }
            if timeout_seconds == 0 or time.monotonic() >= deadline:
                return {
                    "agent_session_id": session_id,
                    "status": session["state"],
                    "complete": False,
                    "session": session,
                    "result": self.store.result(session_id),
                }
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(poll_delay, remaining))
            poll_delay = min(0.25, poll_delay * 1.6)

    @staticmethod
    def format_event(event: dict[str, Any]) -> str | None:
        """Project one semantic event into a compact human-readable update."""
        kind = str(event.get("kind") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if kind == "lifecycle":
            state = data.get("state")
            return f"Agent state: {state}" if state else None
        if kind in {"text_delta", "message"}:
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                return " ".join(text.strip().split())[:1000]
            return None
        if kind == "tool":
            name = data.get("name") or data.get("item_type") or "tool"
            state = data.get("state") or data.get("status")
            suffix = f" ({state})" if state else ""
            return f"Using {name}{suffix}"
        if kind == "child_update":
            role = data.get("role") or "subagent"
            state = data.get("state") or "running"
            provider_id = data.get("provider_session_id")
            suffix = f" [{provider_id}]" if provider_id else ""
            return f"Child {role}: {state}{suffix}"
        if kind == "child_spawned":
            role = data.get("role") or "subagent"
            child_id = data.get("child_session_id")
            provider_id = data.get("provider_session_id")
            identity = child_id or provider_id
            suffix = f" [{identity}]" if identity else ""
            return f"Spawned child {role}{suffix}"
        if kind == "diagnostic":
            error = data.get("error")
            text = data.get("text")
            detail = error if error is not None else text
            return f"Diagnostic: {detail}"[:1000] if detail else None
        if kind == "provider_session":
            provider_id = data.get("provider_session_id")
            return (
                f"Provider session established [{provider_id}]"
                if provider_id
                else "Provider session established"
            )
        if kind == "provider_event":
            event_name = data.get("type") or data.get("event")
            detail = data.get("detail") or data.get("message")
            if detail:
                return f"Provider event {event_name or ''}: {detail}"[:1000]
            if event_name:
                return f"Provider event: {event_name}"
            return "Provider event"
        if kind == "untracked_child":
            tool = data.get("tool") or "native subagent tool"
            return (
                f"Untracked native child activity via {tool}; provider did not expose "
                "a child session identifier"
            )
        return None

    def watch(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        timeout_seconds: float = 10,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Wait for semantic activity or terminal state and return visible updates."""
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")

        deadline = time.monotonic() + min(timeout_seconds, 30.0)
        poll_delay = 0.05
        while True:
            event_payload = self.events(
                session_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            session = self.get(session_id)
            complete = session["state"] not in {"starting", "running"}
            events = event_payload["events"]
            if events or complete or timeout_seconds == 0 or time.monotonic() >= deadline:
                updates = [
                    update
                    for event in events
                    if (update := self.format_event(event)) is not None
                ]
                if not updates and not complete:
                    updates = ["Agent is still running; no new semantic event yet."]
                return {
                    "agent_session_id": session_id,
                    "status": session["state"],
                    "complete": complete,
                    "authoritative_session": True,
                    "replacement_recommended": False,
                    "events": events,
                    "updates": updates,
                    "next_sequence": event_payload["next_sequence"],
                    "result": self.result(session_id) if complete else None,
                }
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(poll_delay, remaining))
            poll_delay = min(0.25, poll_delay * 1.5)

    def result(self, session_id: str) -> dict[str, Any] | None:
        return self.store.result(session_id)

    def get(self, session_id: str) -> dict[str, Any]:
        return self.store.get(session_id).to_dict()

    def list(
        self,
        *,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list(
            parent_session_id=parent_session_id,
            root_session_id=root_session_id,
        )

    def children(self, session_id: str) -> list[dict[str, Any]]:
        return self.store.children(session_id)

    def events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self.store.events(
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def messages(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.store.message_page(
            session_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def send(
        self,
        session_id: str,
        message: str,
        *,
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        if not message.strip():
            raise ValueError("message must not be empty")
        session = self.store.get(session_id)
        if session.state != "idle":
            raise ValueError(
                f"agent session {session_id} is {session.state}; follow-up messages require an idle session"
            )
        provider = self.registry.provider(session.provider)
        capabilities = provider.capabilities()
        if not capabilities.persistent_sessions:
            raise ValueError(
                f"{session.provider} session is closed-end and cannot be continued"
            )
        if session.parent_session_id and not capabilities.child_messaging:
            raise ValueError(
                f"{session.provider} does not expose direct messaging to native child sessions"
            )
        if not session.provider_session_id:
            raise ValueError(
                f"agent session {session_id} has no provider session handle yet"
            )

        lease_owner = self._begin_lease(session_id)
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[session_id] = cancel_event

        try:
            self.store.add_message(
                session_id,
                direction="host_to_agent",
                content=message,
                recipient_session_id=session_id,
            )
            self.store.update(session_id, state="running")

            raw_budget = session.metadata.get("budget")
            budget_values = raw_budget if isinstance(raw_budget, dict) else {}
            budget = TaskBudget(
                max_tokens=budget_values.get("max_tokens"),
                max_cost=budget_values.get("max_cost"),
                max_tool_calls=budget_values.get("max_tool_calls"),
                max_repeated_calls=budget_values.get("max_repeated_calls"),
                max_idle_seconds=budget_values.get("max_idle_seconds"),
                max_runtime_seconds=budget_values.get("max_runtime_seconds"),
                max_child_agents=budget_values.get("max_child_agents"),
            )
            supervision: dict[str, Any] = {
                "tool_calls": 0,
                "last_tool_fingerprint": None,
                "repeated_calls": 0,
                "child_ids": set(),
            }

            def trip_budget(reason: str, activity: str) -> None:
                cancel_event.set()
                self.store.merge_metadata(
                    session_id,
                    {
                        "budget_exceeded": True,
                        "budget_reason": reason,
                        "termination_reason": "supervisor_budget",
                        "current_activity": activity,
                    },
                )

            def progress_line(
                provider_name: str,
                line: str,
                stream: str = "stdout",
            ) -> None:
                semantic = normalize_progress(
                    provider_name,
                    line,
                    session_id,
                    stream,
                )
                for event in normalize_agent_events(
                    provider_name,
                    line,
                    session_id,
                    stream,
                ):
                    self.registry.record_agent_event(provider_name, event)
                if semantic is None:
                    return
                self._renew_lease_on_progress(session_id)

                if semantic.phase == "tool" and semantic.state == "active":
                    supervision["tool_calls"] = int(supervision["tool_calls"]) + 1
                    fingerprint = (
                        str(semantic.tool or ""),
                        str(semantic.command or ""),
                        str(semantic.path or ""),
                    )
                    if fingerprint == supervision["last_tool_fingerprint"]:
                        supervision["repeated_calls"] = int(supervision["repeated_calls"]) + 1
                    else:
                        supervision["last_tool_fingerprint"] = fingerprint
                        supervision["repeated_calls"] = 1
                    if (
                        budget.max_tool_calls is not None
                        and int(supervision["tool_calls"]) > budget.max_tool_calls
                    ):
                        trip_budget(
                            f"tool-call budget exceeded: {supervision['tool_calls']} > {budget.max_tool_calls}",
                            "Tool-call budget exceeded; cancellation requested",
                        )
                    if (
                        budget.max_repeated_calls is not None
                        and int(supervision["repeated_calls"]) > budget.max_repeated_calls
                    ):
                        trip_budget(
                            "repeated tool-call budget exceeded: "
                            f"{supervision['repeated_calls']} > {budget.max_repeated_calls}; "
                            f"fingerprint={fingerprint!r}",
                            "Repeated tool-call stall detected; cancellation requested",
                        )

                child_ids = supervision["child_ids"]
                if isinstance(child_ids, set):
                    for child in semantic.subagents:
                        if not isinstance(child, dict):
                            continue
                        child_id = (
                            child.get("provider_session_id")
                            or child.get("session_id")
                            or child.get("id")
                        )
                        if child_id:
                            child_ids.add(str(child_id))
                    if (
                        budget.max_child_agents is not None
                        and len(child_ids) > budget.max_child_agents
                    ):
                        trip_budget(
                            f"child-agent budget exceeded: {len(child_ids)} > {budget.max_child_agents}",
                            "Child-agent budget exceeded; cancellation requested",
                        )

            effective_timeout = (
                min(timeout_seconds, budget.max_runtime_seconds)
                if budget.max_runtime_seconds is not None
                else timeout_seconds
            )
            task = TaskSpec(
                objective=message,
                role=session.role,
                provider=session.provider,
                workspace=session.workspace,
                access=session.access,  # type: ignore[arg-type]
                reasoning=session.reasoning,
                model=session.model,
                timeout_seconds=effective_timeout,
                isolate_write=False,
                budget=budget,
                metadata={
                    "_agent_session_id": session_id,
                    "_progress_line": progress_line,
                    "_resume_provider_session_id": session.provider_session_id,
                    "_cancel_event": cancel_event,
                },
            )
            try:
                result = provider.continue_session(
                    task,
                    session.provider_session_id,
                )
            except Exception as exc:
                self.store.save_result(
                    session_id,
                    {
                        "status": "error",
                        "provider": session.provider,
                        "model": session.model,
                        "summary": "Agent follow-up execution failed.",
                        "final_text": "",
                        "error": str(exc),
                        "agent_session_id": session_id,
                    },
                )
                self.store.update(session_id, state="failed")
                raise

            latest_metadata = self.store.get(session_id).metadata
            if latest_metadata.get("budget_exceeded"):
                result = replace(
                    result,
                    status="budget_exceeded",
                    summary="CortexRelay stopped the agent follow-up after a supervision budget was exceeded.",
                    error=str(
                        latest_metadata.get("budget_reason")
                        or "agent follow-up supervision budget exceeded"
                    ),
                    termination_reason="supervisor_budget",
                )

            if result.conversation_id:
                self.store.bind_provider_session(
                    session_id,
                    result.conversation_id,
                )
            if result.final_text:
                self.store.add_message(
                    session_id,
                    direction="agent_to_host",
                    content=result.final_text,
                    sender_session_id=session_id,
                    metadata={"final": True, "status": result.status},
                )
            payload = result.to_dict()
            payload["agent_session_id"] = session_id
            self.store.save_result(session_id, payload)
            self.store.update(
                session_id,
                state=(
                    "idle"
                    if result.status == "success"
                    else (
                        "interrupted"
                        if result.status in {"cancelled", "timeout", "budget_exceeded"}
                        else "failed"
                    )
                ),
            )
            return payload
        finally:
            with self._lock:
                self._cancel_events.pop(session_id, None)
            self._end_lease(session_id, lease_owner)

    def cancel(self, session_id: str) -> dict[str, Any]:
        """Request cancellation of the currently running direct agent turn."""
        session = self.store.get(session_id)
        if session.state not in {"starting", "running"}:
            return {
                "agent_session_id": session_id,
                "status": session.state,
                "cancel_requested": False,
                "complete": True,
                "detail": "session has no active turn",
            }

        with self._lock:
            cancel_event = self._cancel_events.get(session_id)
        if cancel_event is None:
            metadata = dict(session.metadata)
            metadata["cancel_requested"] = True
            metadata["cancel_delivery"] = "unavailable_after_runtime_restart"
            self.store.update(session_id, metadata=metadata)
            return {
                "agent_session_id": session_id,
                "status": session.state,
                "cancel_requested": True,
                "complete": False,
                "delivered": False,
                "detail": (
                    "cancellation intent persisted, but this process does not own "
                    "the provider turn; wait for its lease to expire or reconnect to the owner"
                ),
            }

        cancel_event.set()
        return {
            "agent_session_id": session_id,
            "status": session.state,
            "cancel_requested": True,
            "complete": False,
            "delivered": True,
        }

    def close(self, session_id: str) -> dict[str, Any]:
        return self.store.close(session_id).to_dict()
