from __future__ import annotations

import subprocess

from dataclasses import replace
from typing import Any
from uuid import uuid4

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.policy import RoutingPolicy
from cortex_relay.core.profiles import ExecutionProfile, ProfileResolver, RuntimeProfileConfig
from cortex_relay.providers.antigravity import AntigravityAdapter
from cortex_relay.providers.base import ProviderAdapter
from cortex_relay.providers.codex import CodexAdapter
from cortex_relay.providers.opencode import OpenCodeAdapter
from cortex_relay.observability import RunStore
from cortex_relay.runtime.progress import normalize_progress
from cortex_relay.runtime.worktree import WorktreeManager


class ProviderRegistry:
    def __init__(
        self,
        providers: list[ProviderAdapter] | None = None,
        *,
        policy: RoutingPolicy | None = None,
        worktrees: WorktreeManager | None = None,
        profiles: ProfileResolver | None = None,
        run_store: RunStore | None = None,
    ) -> None:
        self._providers: dict[str, ProviderAdapter] = {}
        self.policy = policy or RoutingPolicy()
        self.worktrees = worktrees or WorktreeManager()
        self.profiles = profiles or ProfileResolver()
        self.run_store = run_store or RunStore()
        for provider in providers or []:
            self.register(provider)

    def register(self, provider: ProviderAdapter) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def capabilities(self) -> list[dict[str, object]]:
        return [self._providers[name].capabilities().to_dict() for name in self.names()]

    def available_names(self) -> set[str]:
        return {
            name
            for name, provider in self._providers.items()
            if provider.capabilities().available
        }

    def resolve(self, task: TaskSpec) -> ProviderAdapter:
        name = self.policy.select(task, self.available_names())
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown runtime provider: {name}") from exc

    def profile_config(
        self,
        workspace,
        *,
        preset: str | None = None,
    ) -> dict[str, Any]:
        config = self.profiles.load(workspace)
        return config.to_dict(preset=preset)

    def execute(self, task: TaskSpec) -> TaskResult:
        source_workspace = task.workspace
        task_id = task.metadata.get("_task_id") if task.metadata.get("_prestarted") else None
        if not isinstance(task_id, str):
            task_id = self.run_store.start_task(task)

        metadata = dict(task.metadata)
        metadata["_task_id"] = task_id
        metadata["_observability_workspace"] = str(source_workspace)
        def progress_line(provider: str, line: str, stream: str = "stdout") -> None:
            event = normalize_progress(provider, line, task_id, stream)
            if event is not None:
                self.run_store.record_progress(source_workspace, event)

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
                    result = self._execute_provider(task)
        except Exception as exc:
            self.run_store.update_task(
                source_workspace,
                task_id,
                status="error",
                error=f"Unhandled CortexRelay runtime error: {exc}",
            )
            raise

        result_metadata = dict(result.metadata)
        result_metadata["task_id"] = task_id
        result = replace(result, metadata=result_metadata)

        record = self.run_store.complete_task(source_workspace, task_id, result)
        result_metadata = dict(result.metadata)
        result_metadata["observability"] = {
            "task_id": task_id,
            "session_id": record.get("session_id"),
            "status": result.status,
            "dashboard_command": "cortex-relay status --watch",
            "history_command": "cortex-relay history",
        }
        return replace(result, metadata=result_metadata)

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

        if task.access != "workspace_write" or not task.isolate_write:
            self._observe(
                task,
                status="running",
                provider=provider.name,
                model=task.model,
                reasoning=task.reasoning,
            )
            return provider.execute(task)

        self._observe(task, status="preparing", provider=provider.name)
        task_id = f"{task.role}-{uuid4().hex[:10]}"
        try:
            worktree = self.worktrees.create(task.workspace, task_id=task_id)
        except (OSError, subprocess.CalledProcessError) as exc:
            return TaskResult(
                status="error",
                provider=provider.name,
                model=task.model,
                summary="Could not create an isolated git worktree.",
                error=str(exc),
            )

        self._observe(
            task,
            status="running",
            provider=provider.name,
            model=task.model,
            reasoning=task.reasoning,
            worktree_path=str(worktree.path),
            worktree_branch=worktree.branch,
        )
        if cancel_event is not None and cancel_event.is_set():
            return TaskResult(
                status="cancelled", provider=provider.name, model=task.model,
                summary="Delegation cancelled before provider launch.",
                metadata={"worktree_path": str(worktree.path), "worktree_branch": worktree.branch},
            )
        isolated = replace(task, workspace=worktree.path, isolate_write=False)
        result = provider.execute(isolated)
        metadata = dict(result.metadata)
        metadata.update(
            {
                "worktree_path": str(worktree.path),
                "worktree_branch": worktree.branch,
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
