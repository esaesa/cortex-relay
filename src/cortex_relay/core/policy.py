from __future__ import annotations

from dataclasses import dataclass, field

from .models import TaskSpec


@dataclass(frozen=True)
class RoutingPolicy:
    """Deterministic role-to-provider routing.

    The primary coding agent remains responsible for planning. CortexRelay only
    applies explicit routing policy to a task that has already been defined.
    """

    role_routes: dict[str, str] = field(
        default_factory=lambda: {
            "architect": "antigravity",
            "reviewer": "antigravity",
            "explorer": "antigravity",
            "implementer": "antigravity",
            "tester": "antigravity",
        }
    )
    fallback_provider: str = "antigravity"

    def select(self, task: TaskSpec, available: set[str]) -> str:
        if task.provider != "auto":
            return task.provider

        preferred = self.role_routes.get(task.role, self.fallback_provider)
        if preferred in available:
            return preferred
        if self.fallback_provider in available:
            return self.fallback_provider
        if available:
            return sorted(available)[0]
        return preferred
