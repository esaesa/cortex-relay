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
from cortex_relay.runtime.worktree import WorktreeManager


class ProviderRegistry:
    def __init__(
        self,
        providers: list[ProviderAdapter] | None = None,
        *,
        policy: RoutingPolicy | None = None,
        worktrees: WorktreeManager | None = None,
        profiles: ProfileResolver | None = None,
    ) -> None:
        self._providers: dict[str, ProviderAdapter] = {}
        self.policy = policy or RoutingPolicy()
        self.worktrees = worktrees or WorktreeManager()
        self.profiles = profiles or ProfileResolver()
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

        if profile is not None:
            return self._execute_profile_chain(task, config, profile)
        return self._execute_provider(task)

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

        for candidate in chain:
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
            return provider.execute(task)

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
