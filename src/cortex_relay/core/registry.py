from __future__ import annotations

import subprocess
from dataclasses import replace
from uuid import uuid4

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.policy import RoutingPolicy
from cortex_relay.providers.antigravity import AntigravityAdapter
from cortex_relay.providers.base import ProviderAdapter
from cortex_relay.providers.codex import CodexAdapter
from cortex_relay.runtime.worktree import WorktreeManager


class ProviderRegistry:
    def __init__(
        self,
        providers: list[ProviderAdapter] | None = None,
        *,
        policy: RoutingPolicy | None = None,
        worktrees: WorktreeManager | None = None,
    ) -> None:
        self._providers: dict[str, ProviderAdapter] = {}
        self.policy = policy or RoutingPolicy()
        self.worktrees = worktrees or WorktreeManager()
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

    def execute(self, task: TaskSpec) -> TaskResult:
        try:
            provider = self.resolve(task)
        except KeyError as exc:
            return TaskResult(
                status="unavailable",
                provider=task.provider,
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


def default_registry() -> ProviderRegistry:
    return ProviderRegistry([AntigravityAdapter(), CodexAdapter()])
