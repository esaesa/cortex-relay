from __future__ import annotations

import threading
import time

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cortex_relay.core.models import TaskSpec
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.runtime.agent_events import normalize_agent_events


class AgentService:
    """Provider-neutral control surface for live or resumable agent sessions."""

    def __init__(self, registry: ProviderRegistry, *, max_workers: int = 32) -> None:
        self.registry = registry
        self.store = registry.agent_store
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cortex-agent",
        )
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()

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
        metadata: dict[str, Any] = {}
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
            timeout_seconds=timeout_seconds,
            isolate_write=False,
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
    ) -> dict[str, Any]:
        """Start a direct persistent provider-backed agent session and await its first turn."""
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
        )
        result = self.registry.start_agent(task)
        payload = result.to_dict()
        session_id = result.metadata.get("agent_session_id")
        if isinstance(session_id, str):
            payload["agent_session_id"] = session_id
            payload["session"] = self.store.get(session_id).to_dict()
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
    ) -> dict[str, Any]:
        """Start a direct persistent session concurrently and return its session ID.

        The Cortex session is created before provider execution proceeds far enough
        to emit observable work, allowing immediate agent_events/agent_wait usage.
        No workflow task record is created.
        """
        started = threading.Event()
        holder: dict[str, str] = {}

        def on_started(session_id: str) -> None:
            holder["session_id"] = session_id
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
            on_started=on_started,
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
        with self._lock:
            self._futures[session_id] = future
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

        while True:
            session = self.store.get(session_id).to_dict()
            result = self.store.result(session_id)
            complete = session["state"] not in {"starting", "running"}
            if complete or timeout_seconds == 0 or time.monotonic() >= deadline:
                return {
                    "agent_session_id": session_id,
                    "status": session["state"],
                    "complete": complete,
                    "session": session,
                    "result": result,
                }
            time.sleep(0.05)

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
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return self.store.messages(session_id, limit=limit)

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

        self.store.add_message(
            session_id,
            direction="host_to_agent",
            content=message,
            recipient_session_id=session_id,
        )
        self.store.update(session_id, state="running")

        def progress_line(
            provider_name: str,
            line: str,
            stream: str = "stdout",
        ) -> None:
            for event in normalize_agent_events(
                provider_name,
                line,
                session_id,
                stream,
            ):
                try:
                    self.registry.record_agent_event(provider_name, event)
                except (OSError, ValueError):
                    pass

        task = TaskSpec(
            objective=message,
            role=session.role,
            provider=session.provider,
            workspace=session.workspace,
            access=session.access,  # type: ignore[arg-type]
            reasoning=session.reasoning,
            model=session.model,
            timeout_seconds=timeout_seconds,
            isolate_write=False,
            metadata={
                "_agent_session_id": session_id,
                "_progress_line": progress_line,
                "_resume_provider_session_id": session.provider_session_id,
            },
        )
        try:
            result = provider.continue_session(
                task,
                session.provider_session_id,
            )
        except Exception:
            self.store.update(session_id, state="failed")
            raise

        if result.conversation_id:
            self.store.bind_provider_session(
                session_id,
                result.conversation_id,
            )
        self.store.update(
            session_id,
            state="idle" if result.status == "success" else "failed",
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
        try:
            self.store.save_result(session_id, payload)
        except (OSError, ValueError):
            pass
        return payload

    def close(self, session_id: str) -> dict[str, Any]:
        return self.store.close(session_id).to_dict()
