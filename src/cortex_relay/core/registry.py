from __future__ import annotations

import logging
import subprocess
import threading
import time

from dataclasses import replace
from typing import Any
from pathlib import Path
from datetime import datetime, timezone

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.runtime.agent_backend import AgentStoreBackend
from cortex_relay.runtime.agent_store import AgentStore
from cortex_relay.runtime.agent_events import normalize_agent_events
from cortex_relay.core.policy import RoutingPolicy
from cortex_relay.core.profiles import ExecutionProfile, ProfileResolver, RuntimeProfileConfig
from cortex_relay.providers.antigravity import AntigravityAdapter
from cortex_relay.providers.base import ProviderAdapter
from cortex_relay.providers.codex import CodexAdapter
from cortex_relay.providers.opencode import OpenCodeAdapter
from cortex_relay.observability import RunStore, summarize_usage
from cortex_relay.runtime.artifacts import ArtifactStore
from cortex_relay.runtime.progress import normalize_progress
from cortex_relay.runtime.supervision import SupervisionTracker
from cortex_relay.runtime.worktree import WorktreeManager


logger = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(
        self,
        providers: list[ProviderAdapter] | None = None,
        *,
        policy: RoutingPolicy | None = None,
        worktrees: WorktreeManager | None = None,
        profiles: ProfileResolver | None = None,
        run_store: RunStore | None = None,
        agent_store: AgentStoreBackend | None = None,
    ) -> None:
        self._providers: dict[str, ProviderAdapter] = {}
        self._health_lock = threading.Lock()
        self._provider_health: dict[str, dict[str, Any]] = {}
        self.policy = policy or RoutingPolicy()
        self.worktrees = worktrees or WorktreeManager()
        self.profiles = profiles or ProfileResolver()
        self.run_store = run_store or RunStore()
        # Keep the backend injectable so a future multi-host implementation can
        # provide the same AgentStore contract with PostgreSQL or another
        # transactional coordinator. SQLite remains the default single-host store.
        self.agent_store = (
            agent_store
            or self.run_store.agent_store
            or AgentStore(self.run_store.root)
        )
        # RunStore owns the CLI/status join. Point it at the exact same backend
        # so status/history/gc cannot diverge from direct-agent control state.
        self.run_store.agent_store = self.agent_store
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: ProviderAdapter) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def capabilities(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for name in self.names():
            row = self._providers[name].capabilities().to_dict()
            row["runtime_health"] = self.provider_health(name)
            rows.append(row)
        return rows

    def provider(self, name: str) -> ProviderAdapter:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown runtime provider: {name}") from exc

    def available_names(self) -> set[str]:
        return {
            name
            for name, provider in self._providers.items()
            if provider.capabilities().available
        }

    def provider_health(self, name: str) -> dict[str, Any]:
        with self._health_lock:
            raw = dict(self._provider_health.get(name, {}))
        attempts = int(raw.get("attempts") or 0)
        successes = int(raw.get("successes") or 0)
        timeouts = int(raw.get("timeouts") or 0)
        failures = int(raw.get("failures") or 0)
        return {
            "attempts": attempts,
            "successes": successes,
            "success_rate": (successes / attempts) if attempts else None,
            "timeouts": timeouts,
            "timeout_rate": (timeouts / attempts) if attempts else None,
            "failures": failures,
            "failure_rate": (failures / attempts) if attempts else None,
            "consecutive_failures": int(raw.get("consecutive_failures") or 0),
            "ema_latency_seconds": raw.get("ema_latency_seconds"),
            "last_status": raw.get("last_status"),
        }

    def _record_provider_health(
        self,
        name: str,
        result: TaskResult,
        duration_seconds: float,
    ) -> None:
        with self._health_lock:
            health = dict(self._provider_health.get(name, {}))
            attempts = int(health.get("attempts") or 0) + 1
            successes = int(health.get("successes") or 0)
            timeouts = int(health.get("timeouts") or 0)
            failures = int(health.get("failures") or 0)
            consecutive = int(health.get("consecutive_failures") or 0)
            if result.status == "success":
                successes += 1
                consecutive = 0
            else:
                consecutive += 1
                if result.status == "timeout":
                    timeouts += 1
                elif result.status not in {"cancelled", "budget_exceeded"}:
                    failures += 1
            previous_latency = health.get("ema_latency_seconds")
            ema_latency = (
                duration_seconds
                if not isinstance(previous_latency, (int, float))
                else (0.7 * float(previous_latency) + 0.3 * duration_seconds)
            )
            self._provider_health[name] = {
                "attempts": attempts,
                "successes": successes,
                "timeouts": timeouts,
                "failures": failures,
                "consecutive_failures": consecutive,
                "ema_latency_seconds": round(ema_latency, 3),
                "last_status": result.status,
            }

    def _health_score(self, name: str, *, preferred: str | None = None) -> float:
        health = self.provider_health(name)
        attempts = int(health["attempts"])
        score = 0.35 if name == preferred else 0.0
        if not attempts:
            return score
        success_rate = float(health["success_rate"] or 0.0)
        timeout_rate = float(health["timeout_rate"] or 0.0)
        failure_rate = float(health["failure_rate"] or 0.0)
        latency = float(health["ema_latency_seconds"] or 0.0)
        score += success_rate
        score -= 1.25 * timeout_rate
        score -= 0.75 * failure_rate
        score -= min(latency / 600.0, 0.5)
        score -= min(int(health["consecutive_failures"]) * 0.2, 0.8)
        return score

    def _ordered_provider_names(self, task: TaskSpec) -> list[str]:
        available = self.available_names()
        preferred = self.policy.select(task, available)
        if task.provider != "auto":
            return [task.provider]
        # An explicit model ID is provider-specific in practice. Preserve the
        # deterministic policy-selected provider rather than trying that same
        # model ID against unrelated providers during health-aware routing.
        if task.model is not None:
            return [preferred]
        if not available:
            return [preferred]
        return sorted(
            available,
            key=lambda name: (
                self._health_score(name, preferred=preferred),
                name == preferred,
                name,
            ),
            reverse=True,
        )

    def resolve(self, task: TaskSpec) -> ProviderAdapter:
        name = self._ordered_provider_names(task)[0]
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown runtime provider: {name}") from exc

    def _run_provider_session(
        self,
        provider: ProviderAdapter,
        task: TaskSpec,
    ) -> TaskResult:
        started = time.monotonic()
        try:
            result = provider.execute_session(task)
        except Exception:
            synthetic = TaskResult(
                status="error",
                provider=provider.name,
                model=task.model,
                summary="Provider session raised before returning a normalized result.",
                termination_reason="provider_exception",
            )
            self._record_provider_health(
                provider.name,
                synthetic,
                time.monotonic() - started,
            )
            raise
        self._record_provider_health(
            provider.name,
            result,
            time.monotonic() - started,
        )
        return result

    def continue_provider_session(
        self,
        provider: ProviderAdapter,
        task: TaskSpec,
        provider_session_id: str,
    ) -> TaskResult:
        """Continue a durable provider session while feeding runtime health."""
        started = time.monotonic()
        try:
            result = provider.continue_session(task, provider_session_id)
        except Exception:
            synthetic = TaskResult(
                status="error",
                provider=provider.name,
                model=task.model,
                summary="Provider continuation raised before returning a normalized result.",
                termination_reason="provider_exception",
            )
            self._record_provider_health(
                provider.name,
                synthetic,
                time.monotonic() - started,
            )
            raise
        self._record_provider_health(
            provider.name,
            result,
            time.monotonic() - started,
        )
        return result

    def profile_config(
        self,
        workspace,
        *,
        preset: str | None = None,
    ) -> dict[str, Any]:
        config = self.profiles.load(workspace)
        return config.to_dict(preset=preset)


    def start_agent(self, task: TaskSpec) -> TaskResult:
        """Start one direct provider-backed agent session without creating a workflow task.

        This is the session-first path. It deliberately skips DAG scheduling,
        worktree isolation, token/cost workflow budgets, quality gates, and task
        lifecycle records. Direct semantic supervision budgets still apply. Callers that
        need those workflow guarantees should use the task/delegation API instead.
        """
        task = replace(task, isolate_write=False)
        try:
            config = self.profiles.load(task.workspace)
            profile = config.profile_for_task(task)
        except (OSError, ValueError) as exc:
            return TaskResult(
                status="error",
                provider=task.provider,
                model=task.model,
                summary="CortexRelay execution-profile configuration is invalid.",
                error=str(exc),
            )

        if profile is None:
            return self._start_direct_agent_provider(task)

        try:
            chain = config.fallback_chain(profile)
        except ValueError as exc:
            return TaskResult(
                status="error",
                provider=profile.provider,
                model=profile.model,
                summary="CortexRelay profile fallback configuration is invalid.",
                error=str(exc),
            )

        attempts: list[dict[str, Any]] = []
        source = "explicit" if task.profile else f"role:{task.role}"
        last: TaskResult | None = None
        for index, candidate in enumerate(chain, start=1):
            if task.access == "workspace_write" and candidate.access == "read_only":
                result = TaskResult(
                    status="error",
                    provider=candidate.provider,
                    model=candidate.model,
                    summary="Selected CortexRelay profile does not permit workspace writes.",
                    error=(
                        f"profile {candidate.name!r} has access=read_only but the "
                        "agent session requires workspace_write"
                    ),
                )
            else:
                effective = replace(
                    self._task_for_profile(task, candidate),
                    isolate_write=False,
                )
                result = self._start_direct_agent_provider(effective)

            attempts.append(
                {
                    "attempt": index,
                    "profile": candidate.name,
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "status": result.status,
                    "error": result.error,
                }
            )
            result = self._with_routing_metadata(
                result,
                candidate,
                task,
                attempts,
                source=source,
            )
            last = result
            if result.metadata.get("agent_session_id"):
                # A direct persistent session has acquired identity. Never silently
                # replace it with a different provider/session after launch.
                return result
            if result.ok or result.status == "cancelled":
                return result
            if result.status not in candidate.fallback_on:
                return result

        assert last is not None
        return last

    def execute(self, task: TaskSpec) -> TaskResult:
        if (
            task.budget.max_runtime_seconds is not None
            and task.timeout_seconds > task.budget.max_runtime_seconds
        ):
            task = replace(task, timeout_seconds=task.budget.max_runtime_seconds)
        source_workspace = task.workspace
        task_id = task.metadata.get("_task_id") if task.metadata.get("_prestarted") else None
        if not isinstance(task_id, str):
            task_id = self.run_store.start_task(task)

        metadata = dict(task.metadata)
        metadata["_task_id"] = task_id
        metadata["_observability_workspace"] = str(source_workspace)
        external_progress = metadata.get("_external_progress")
        cancel_event = metadata.get("_cancel_event")
        if cancel_event is None:
            cancel_event = threading.Event()
            metadata["_cancel_event"] = cancel_event
        supervision = SupervisionTracker(task.budget)

        def trip_budget(reason: str, activity: str) -> None:
            if hasattr(cancel_event, "set"):
                cancel_event.set()
            self.run_store.update_task(
                source_workspace,
                task_id,
                budget_exceeded=True,
                budget_reason=reason,
                termination_reason="supervisor_budget",
                current_activity=activity,
            )

        def progress_line(provider: str, line: str, stream: str = "stdout") -> None:
            task_record = self.run_store.get_task(source_workspace, task_id) or {}
            agent_session_id = task_record.get("agent_session_id")
            if isinstance(agent_session_id, str):
                for agent_event in normalize_agent_events(
                    provider, line, agent_session_id, stream
                ):
                    try:
                        self.record_agent_event(provider, agent_event)
                    except (OSError, ValueError) as exc:
                        logger.warning(
                            "failed to persist agent event for %s: %s",
                            agent_session_id,
                            exc,
                        )

            event = normalize_progress(provider, line, task_id, stream)
            if event is None:
                return
            self.run_store.record_progress(source_workspace, event)
            violation = supervision.observe(event)
            if violation is not None:
                trip_budget(violation.reason, violation.activity)

            if callable(external_progress):
                try:
                    external_progress(event)
                except Exception:
                    pass

        metadata["_progress_line"] = progress_line
        metadata["_progress_heartbeat"] = lambda pid, alive: self.run_store.record_heartbeat(
            source_workspace, task_id, pid, alive
        )
        task = replace(task, metadata=metadata)

        try:
            try:
                config = self.profiles.load(task.workspace)
                profile = config.profile_for_task(task)
            except (OSError, ValueError) as exc:
                result = TaskResult(
                    status="error",
                    provider=task.provider,
                    model=task.model,
                    summary="CortexRelay execution-profile configuration is invalid.",
                    error=str(exc),
                )
            else:
                if profile is not None:
                    result = self._execute_profile_chain(task, config, profile)
                else:
                    result = (
                        self._execute_auto_chain(task)
                        if task.provider == "auto"
                        else self._execute_provider(task)
                    )
        except Exception as exc:
            updates: dict[str, Any] = {
                "error": f"Unhandled CortexRelay runtime error: {exc}",
                "current_activity": "Runtime error; finalizing task",
            }
            if not task.metadata.get("_prestarted"):
                updates["status"] = "error"
            self.run_store.update_task(source_workspace, task_id, **updates)
            raise

        usage_summary = summarize_usage(result.usage)
        budget_error: str | None = None
        if task.budget.max_tokens is not None:
            total = usage_summary.get("total_tokens")
            if isinstance(total, int) and total > task.budget.max_tokens:
                budget_error = (
                    f"token budget exceeded: {total} > {task.budget.max_tokens}"
                )
        if task.budget.max_cost is not None:
            cost = usage_summary.get("cost")
            if isinstance(cost, (int, float)) and float(cost) > task.budget.max_cost:
                budget_error = (
                    f"cost budget exceeded: {float(cost):.6f} > {task.budget.max_cost:.6f}"
                )
        stored = self.run_store.get_task(source_workspace, task_id) or {}
        if stored.get("budget_exceeded") and not budget_error:
            budget_error = str(stored.get("budget_reason") or "task budget exceeded")
        if budget_error and result.status != "timeout":
            result = TaskResult(
                status="budget_exceeded",
                provider=result.provider,
                model=result.model,
                summary="CortexRelay stopped or rejected work after its configured budget was exceeded.",
                final_text=result.final_text,
                evidence=result.evidence,
                changed_files=result.changed_files,
                commands=result.commands,
                tests=result.tests,
                risks=result.risks,
                conversation_id=result.conversation_id,
                error=budget_error,
                termination_reason="supervisor_budget",
                duration_seconds=result.duration_seconds,
                usage=result.usage,
                metadata=result.metadata,
            )

        result_metadata = dict(result.metadata)
        result_metadata["task_id"] = task_id
        result_metadata["observability"] = {
            "task_id": task_id,
            "session_id": (self.run_store.get_task(source_workspace, task_id) or {}).get("session_id"),
            "status": result.status,
            "dashboard_command": "cortex-relay status --watch",
            "history_command": "cortex-relay history",
        }
        result = replace(result, metadata=result_metadata)
        self.run_store.complete_task(source_workspace, task_id, result)
        return result

    def status_snapshot(
        self,
        workspace,
        *,
        limit: int = 20,
        active_only: bool = False,
        completed_only: bool = False,
    ) -> dict[str, Any]:
        return self.run_store.snapshot(
            workspace,
            limit=limit,
            active_only=active_only,
            completed_only=completed_only,
        )

    def clear_completed_status(self, workspace) -> int:
        return self.run_store.clear_completed(workspace)

    def record_agent_event(self, provider: str, event: Any) -> None:
        saved = self.agent_store.record_event(event)
        kind = event.kind
        data = event.data if isinstance(event.data, dict) else {}
        if kind == "provider_session":
            provider_session_id = data.get("provider_session_id")
            if isinstance(provider_session_id, str) and provider_session_id:
                self.agent_store.bind_provider_session(
                    event.session_id, provider_session_id
                )
        elif kind == "child_update":
            provider_session_id = data.get("provider_session_id")
            if isinstance(provider_session_id, str) and provider_session_id:
                self.agent_store.upsert_child(
                    parent_session_id=event.session_id,
                    provider_session_id=provider_session_id,
                    provider=provider,
                    state=str(data.get("state") or "running"),
                    role=str(data.get("role") or "subagent"),
                    metadata={
                        key: value
                        for key, value in data.items()
                        if key not in {"provider_session_id", "state", "role"}
                    },
                )
        elif kind == "untracked_child":
            session = self.agent_store.get(event.session_id)
            metadata = dict(session.metadata)
            metadata["untracked_native_work"] = True
            metadata["untracked_native_work_count"] = (
                int(metadata.get("untracked_native_work_count") or 0) + 1
            )
            metadata["last_untracked_native_work"] = {
                "provider": provider,
                "event": event.provider_event,
                "data": data,
                "sequence": saved.get("sequence"),
            }
            self.agent_store.update(event.session_id, metadata=metadata)
            logger.warning(
                "provider-native child activity could not be correlated for session %s",
                event.session_id,
            )
        elif kind == "message":
            text = data.get("text")
            if isinstance(text, str) and text:
                self.agent_store.add_message(
                    event.session_id,
                    direction="agent_to_host",
                    content=text,
                    sender_session_id=event.session_id,
                    metadata={
                        "provider_event": event.provider_event,
                        "sequence": saved.get("sequence"),
                        "phase": data.get("phase"),
                    },
                )

    def _start_agent_session(
        self,
        task: TaskSpec,
        provider: ProviderAdapter,
    ) -> tuple[TaskSpec, str]:
        parent_session_id = task.metadata.get("_parent_agent_session_id")
        parent = parent_session_id if isinstance(parent_session_id, str) else None
        root_session_id = None
        if parent:
            try:
                root_session_id = (
                    self.agent_store.get(parent).root_session_id or parent
                )
            except ValueError:
                parent = None
        session = self.agent_store.create(
            provider=provider.name,
            workspace=task.workspace,
            task_id=str(task.metadata.get("_task_id") or "") or None,
            model=task.model,
            reasoning=task.reasoning,
            access=task.access,
            role=task.role,
            objective=task.objective,
            parent_session_id=parent,
            root_session_id=root_session_id,
            metadata={
                "transport": provider.capabilities().session_mode,
                "profile": task.profile,
                "budget": task.budget.to_dict(),
            },
        )
        self.agent_store.add_message(
            session.session_id,
            direction="host_to_agent",
            content=task.objective,
            recipient_session_id=session.session_id,
            metadata={"initial": True},
        )
        on_started = task.metadata.get("_agent_session_started")
        if callable(on_started):
            try:
                on_started(session.session_id)
            except Exception:
                pass
        task_id = task.metadata.get("_task_id")
        source_workspace = task.metadata.get("_observability_workspace")
        if isinstance(task_id, str) and isinstance(source_workspace, str):
            current = self.run_store.get_task(Path(source_workspace), task_id) or {}
            prior = [
                str(item)
                for item in (current.get("agent_session_ids") or [])
                if isinstance(item, str)
            ]
            self.run_store.update_task(
                Path(source_workspace),
                task_id,
                agent_session_id=session.session_id,
                agent_session_ids=list(dict.fromkeys([*prior, session.session_id])),
            )
        return replace(
            task,
            metadata={**task.metadata, "_agent_session_id": session.session_id},
        ), session.session_id

    def _finish_agent_session(
        self,
        session_id: str,
        result: TaskResult,
    ) -> TaskResult:
        if result.conversation_id:
            try:
                self.agent_store.bind_provider_session(
                    session_id, result.conversation_id
                )
            except ValueError:
                pass

        metadata = dict(result.metadata)
        metadata["agent_session_id"] = session_id
        finalized = replace(result, metadata=metadata)

        try:
            if finalized.final_text:
                self.agent_store.add_message(
                    session_id,
                    direction="agent_to_host",
                    content=finalized.final_text,
                    sender_session_id=session_id,
                    metadata={"final": True, "status": finalized.status},
                )
            # Persist the durable result before exposing a terminal session state.
            # Consumers may treat idle/failed as meaning agent_result is ready.
            self.agent_store.save_result(session_id, finalized.to_dict())
            self.agent_store.update(
                session_id,
                state=(
                    "idle"
                    if finalized.status == "success"
                    else (
                        "interrupted"
                        if finalized.status in {"cancelled", "timeout", "budget_exceeded"}
                        else "failed"
                    )
                ),
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "failed to persist complete agent result for %s before terminal state: %s",
                session_id,
                exc,
            )
            try:
                self.agent_store.update(
                    session_id,
                    state=(
                        "idle"
                        if finalized.status == "success"
                        else (
                            "interrupted"
                            if finalized.status in {"cancelled", "timeout", "budget_exceeded"}
                            else "failed"
                        )
                    ),
                )
            except (OSError, ValueError) as state_exc:
                logger.error(
                    "failed to publish terminal agent state for %s: %s",
                    session_id,
                    state_exc,
                )
        return finalized

    def _observe(self, task: TaskSpec, **updates: Any) -> None:
        task_id = task.metadata.get("_task_id")
        workspace = task.metadata.get("_observability_workspace")
        if not isinstance(task_id, str) or not isinstance(workspace, str):
            return
        self.run_store.update_task(workspace, task_id, **updates)

    def _execute_profile_chain(
        self,
        task: TaskSpec,
        config: RuntimeProfileConfig,
        profile: ExecutionProfile,
    ) -> TaskResult:
        try:
            chain = config.fallback_chain(profile)
        except ValueError as exc:
            return TaskResult(
                status="error",
                provider=profile.provider,
                model=profile.model,
                summary="CortexRelay profile fallback configuration is invalid.",
                error=str(exc),
            )

        attempts: list[dict[str, Any]] = []
        source = "explicit" if task.profile else f"role:{task.role}"
        last: TaskResult | None = None

        for index, candidate in enumerate(chain, start=1):
            self._observe(
                task,
                status="fallback" if index > 1 else "preparing",
                profile=candidate.name,
                provider=candidate.provider,
                model=candidate.model,
                reasoning=candidate.reasoning,
                billing_class=candidate.billing_class,
                attempt=index,
            )
            if task.access == "workspace_write" and candidate.access == "read_only":
                result = TaskResult(
                    status="error",
                    provider=candidate.provider,
                    model=candidate.model,
                    summary="Selected CortexRelay profile does not permit workspace writes.",
                    error=(
                        f"profile {candidate.name!r} has access=read_only but the task "
                        "requires workspace_write"
                    ),
                )
            else:
                effective = self._task_for_profile(task, candidate)
                effective = replace(
                    effective,
                    metadata={**effective.metadata, "_worktree_attempt": index},
                )
                result = self._execute_provider(effective)

            attempts.append(
                {
                    "attempt": index,
                    "profile": candidate.name,
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "status": result.status,
                    "error": result.error,
                    "worktree_path": result.metadata.get("worktree_path"),
                    "worktree_branch": result.metadata.get("worktree_branch"),
                    "worktree_base_commit": result.metadata.get("worktree_base_commit"),
                }
            )
            result = self._with_routing_metadata(
                result,
                candidate,
                task,
                attempts,
                source=source,
            )
            self._observe(
                task,
                attempts=list(attempts),
                worktree_path=result.metadata.get("worktree_path"),
                worktree_branch=result.metadata.get("worktree_branch"),
            )
            last = result

            if result.ok or result.status == "cancelled":
                return result
            if result.status not in candidate.fallback_on:
                return result

        assert last is not None
        return last

    def _task_for_profile(
        self,
        task: TaskSpec,
        profile: ExecutionProfile,
    ) -> TaskSpec:
        metadata = dict(task.metadata)
        metadata.update(
            {
                "resolved_profile": profile.name,
                "billing_class": profile.billing_class,
                "profile_options": dict(profile.options),
            }
        )
        isolate_write = (
            task.access == "workspace_write"
            and (task.isolate_write or profile.isolate_write)
        )
        return replace(
            task,
            profile=profile.name,
            provider=profile.provider,
            model=profile.model,
            reasoning=profile.reasoning,
            isolate_write=isolate_write,
            metadata=metadata,
        )

    def _start_direct_agent_provider(self, task: TaskSpec) -> TaskResult:
        try:
            provider = self.resolve(task)
        except KeyError as exc:
            return TaskResult(
                status="unavailable",
                provider=task.provider,
                model=task.model,
                summary="No matching CortexRelay runtime provider is registered.",
                error=str(exc),
            )

        capabilities = provider.capabilities()
        if not capabilities.available:
            return TaskResult(
                status="unavailable",
                provider=provider.name,
                model=task.model,
                summary="Selected provider is unavailable.",
                error=capabilities.detail,
            )
        if not capabilities.persistent_sessions:
            return TaskResult(
                status="unavailable",
                provider=provider.name,
                model=task.model,
                summary="Selected provider does not expose persistent agent sessions.",
                error=(
                    f"{provider.name} is configured as {capabilities.session_mode}; "
                    "use delegate/delegate_async for closed-end execution"
                ),
                metadata={
                    "session_mode": capabilities.session_mode,
                    "persistent_sessions": False,
                    "workflow_task": False,
                },
            )

        task, agent_session_id = self._start_agent_session(
            replace(task, isolate_write=False),
            provider,
        )

        supervision = SupervisionTracker(task.budget)

        def trip_direct_budget(reason: str, activity: str) -> None:
            cancel_event = task.metadata.get("_cancel_event")
            if cancel_event is not None and hasattr(cancel_event, "set"):
                cancel_event.set()
            try:
                self.agent_store.merge_metadata(
                    agent_session_id,
                    {
                        "budget_exceeded": True,
                        "budget_reason": reason,
                        "termination_reason": "supervisor_budget",
                        "current_activity": activity,
                    },
                )
            except (OSError, ValueError):
                pass

        def progress_line(
            provider_name: str,
            line: str,
            stream: str = "stdout",
        ) -> None:
            semantic = normalize_progress(
                provider_name,
                line,
                agent_session_id,
                stream,
            )
            for event in normalize_agent_events(
                provider_name,
                line,
                agent_session_id,
                stream,
            ):
                try:
                    self.record_agent_event(provider_name, event)
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "failed to persist direct agent event for %s: %s",
                        agent_session_id,
                        exc,
                    )

            progress_hook = task.metadata.get("_agent_progress")
            if semantic is not None and callable(progress_hook):
                try:
                    progress_hook(agent_session_id)
                except Exception:
                    pass

            if semantic is None:
                return
            violation = supervision.observe(semantic)
            if violation is not None:
                trip_direct_budget(violation.reason, violation.activity)

        def progress_heartbeat(pid: int, alive: bool) -> None:
            try:
                self.agent_store.merge_metadata(
                    agent_session_id,
                    {
                        "provider_pid": pid,
                        "provider_process_alive": alive,
                        "provider_heartbeat_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except (OSError, ValueError) as exc:
                logger.warning(
                    "failed to persist provider heartbeat for %s: %s",
                    agent_session_id,
                    exc,
                )

        task = replace(
            task,
            metadata={
                **task.metadata,
                "_progress_line": progress_line,
                "_progress_heartbeat": progress_heartbeat,
            },
        )
        try:
            provider_result = self._run_provider_session(provider, task)
            try:
                session_metadata = self.agent_store.get(agent_session_id).metadata
            except (OSError, ValueError):
                session_metadata = {}
            if session_metadata.get("budget_exceeded"):
                provider_result = replace(
                    provider_result,
                    status="budget_exceeded",
                    summary="CortexRelay stopped the direct agent after a supervision budget was exceeded.",
                    error=str(
                        session_metadata.get("budget_reason")
                        or "direct-agent supervision budget exceeded"
                    ),
                    termination_reason="supervisor_budget",
                )
            result = self._finish_agent_session(
                agent_session_id,
                provider_result,
            )
        except Exception as exc:
            result = self._finish_agent_session(
                agent_session_id,
                TaskResult(
                    status="error",
                    provider=provider.name,
                    model=task.model,
                    summary="Direct agent execution failed.",
                    error=str(exc),
                ),
            )

        finalized = replace(
            result,
            metadata={
                **result.metadata,
                "session_mode": capabilities.session_mode,
                "persistent_sessions": capabilities.persistent_sessions,
                "workflow_task": False,
            },
        )
        try:
            self.agent_store.save_result(
                agent_session_id,
                finalized.to_dict(),
            )
        except (OSError, ValueError):
            pass
        return finalized

    def _execute_auto_chain(self, task: TaskSpec) -> TaskResult:
        """Try healthy available providers for an unprofiled auto-routed workflow task."""
        names = self._ordered_provider_names(task)
        last: TaskResult | None = None
        attempts: list[dict[str, Any]] = []
        for index, name in enumerate(names, start=1):
            effective = replace(
                task,
                provider=name,
                metadata={**task.metadata, "_worktree_attempt": index},
            )
            result = self._execute_provider(effective)
            attempts.append(
                {
                    "attempt": index,
                    "provider": name,
                    "status": result.status,
                    "termination_reason": result.termination_reason,
                    "error": result.error,
                }
            )
            result = replace(
                result,
                metadata={
                    **result.metadata,
                    "auto_routing_attempts": list(attempts),
                    "health_routed": True,
                },
            )
            last = result
            if result.ok or result.status in {"cancelled", "budget_exceeded"}:
                return result
            if result.status not in {"unavailable", "timeout", "error", "interrupted"}:
                return result
        assert last is not None
        return last

    def _execute_provider(self, task: TaskSpec) -> TaskResult:
        cancel_event = task.metadata.get("_cancel_event")
        if cancel_event is not None and cancel_event.is_set():
            return TaskResult(
                status="cancelled", provider=task.provider, model=task.model,
                summary="Delegation cancelled before provider launch.",
            )
        try:
            provider = self.resolve(task)
        except KeyError as exc:
            return TaskResult(
                status="unavailable",
                provider=task.provider,
                model=task.model,
                summary="No matching CortexRelay runtime provider is registered.",
                error=str(exc),
            )

        inherited_artifact = task.metadata.get("_inherit_artifact_id")
        needs_inherited_workspace = isinstance(inherited_artifact, str)
        if (
            not needs_inherited_workspace
            and (task.access != "workspace_write" or not task.isolate_write)
        ):
            self._observe(
                task,
                status="running",
                provider=provider.name,
                model=task.model,
                reasoning=task.reasoning,
            )
            task, agent_session_id = self._start_agent_session(task, provider)
            return self._finish_agent_session(
                agent_session_id, self._run_provider_session(provider, task)
            )

        self._observe(task, status="preparing", provider=provider.name)
        task_id = task.metadata.get("_task_id")
        if not isinstance(task_id, str):
            raise ValueError("isolated execution requires an owning task ID")
        attempt = int(task.metadata.get("_worktree_attempt", 1))
        try:
            worktree = self.worktrees.create(
                task.workspace, task_id=task_id, attempt=attempt
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            return TaskResult(
                status="error",
                provider=provider.name,
                model=task.model,
                summary="Could not create an isolated git worktree.",
                error=str(exc),
            )

        source_workspace = task.metadata.get("_observability_workspace")
        if isinstance(source_workspace, str):
            self.run_store.record_worktree_attempt(
                source_workspace,
                task_id,
                {
                    "attempt": attempt,
                    "profile": task.profile,
                    "source_repository": str(worktree.source_repository or task.workspace.resolve()),
                    "path": str(worktree.path),
                    "branch": worktree.branch,
                    "base_commit": worktree.base_commit,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "handoff_status": None,
                },
            )

        if isinstance(inherited_artifact, str):
            try:
                inheritance = ArtifactStore(self.run_store).apply_to_worktree(
                    inherited_artifact, worktree.path
                )
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                return TaskResult(
                    status="error",
                    provider=provider.name,
                    model=task.model,
                    summary="Could not inherit the requested workflow artifact.",
                    error=str(exc),
                    metadata={
                        "worktree_path": str(worktree.path),
                        "worktree_branch": worktree.branch,
                        "worktree_base_commit": worktree.base_commit,
                    },
                )
            self._observe(
                task,
                inherited_artifact_id=inherited_artifact,
                inherited_from_task=inheritance.get("source_task_id"),
            )

        self._observe(
            task,
            status="running",
            provider=provider.name,
            model=task.model,
            reasoning=task.reasoning,
            worktree_path=str(worktree.path),
            worktree_branch=worktree.branch,
            worktree_base_commit=worktree.base_commit,
        )
        if cancel_event is not None and cancel_event.is_set():
            return TaskResult(
                status="cancelled", provider=provider.name, model=task.model,
                summary="Delegation cancelled before provider launch.",
                metadata={"worktree_path": str(worktree.path),
                          "worktree_branch": worktree.branch,
                          "worktree_base_commit": worktree.base_commit},
            )
        isolated = replace(task, workspace=worktree.path, isolate_write=False)
        isolated, agent_session_id = self._start_agent_session(isolated, provider)
        result = self._finish_agent_session(
            agent_session_id, self._run_provider_session(provider, isolated)
        )
        metadata = dict(result.metadata)
        metadata.update(
            {
                "worktree_path": str(worktree.path),
                "worktree_branch": worktree.branch,
                "worktree_base_commit": worktree.base_commit,
                "source_workspace": str(task.workspace),
            }
        )
        return replace(result, metadata=metadata)

    @staticmethod
    def _with_routing_metadata(
        result: TaskResult,
        profile: ExecutionProfile,
        task: TaskSpec,
        attempts: list[dict[str, Any]],
        *,
        source: str,
    ) -> TaskResult:
        metadata = dict(result.metadata)
        metadata.update(
            {
                "profile": profile.name,
                "profile_source": source,
                "preset": task.preset,
                "billing_class": profile.billing_class,
                "routing_attempts": list(attempts),
            }
        )
        return replace(result, metadata=metadata)


def default_registry() -> ProviderRegistry:
    return ProviderRegistry(
        [
            AntigravityAdapter(),
            CodexAdapter(),
            OpenCodeAdapter(),
        ]
    )
