from __future__ import annotations

import subprocess

from dataclasses import replace
from typing import Any
from datetime import datetime, timezone

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.policy import RoutingPolicy
from cortex_relay.core.profiles import ExecutionProfile, ProfileResolver, RuntimeProfileConfig
from cortex_relay.providers.antigravity import AntigravityAdapter
from cortex_relay.providers.base import ProviderAdapter
from cortex_relay.providers.codex import CodexAdapter
from cortex_relay.providers.opencode import OpenCodeAdapter
from cortex_relay.observability import RunStore, summarize_usage
from cortex_relay.runtime.artifacts import ArtifactStore
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
        external_progress = metadata.get("_external_progress")
        def progress_line(provider: str, line: str, stream: str = "stdout") -> None:
            event = normalize_progress(provider, line, task_id, stream)
            if event is None:
                return
            self.run_store.record_progress(source_workspace, event)
            if callable(external_progress):
                try:
                    external_progress(event)
                except Exception:
                    pass
            limit = task.budget.max_tokens
            if limit is None:
                return
            observed = event.total_tokens
            if observed is None:
                observed = (
                    (event.input_tokens or 0) + (event.output_tokens or 0)
                    if event.input_tokens is not None or event.output_tokens is not None
                    else None
                )
            if observed is not None and observed > limit:
                cancel_event = task.metadata.get("_cancel_event")
                if cancel_event is not None:
                    cancel_event.set()
                self.run_store.update_task(
                    source_workspace,
                    task_id,
                    budget_exceeded=True,
                    budget_reason=f"token budget exceeded: {observed} > {limit}",
                    current_activity="Token budget exceeded; cancellation requested",
                )

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
        if budget_error and result.status not in {"cancelled", "timeout"}:
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
            return provider.execute(task)

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
        result = provider.execute(isolated)
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
