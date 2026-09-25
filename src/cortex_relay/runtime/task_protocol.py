"""Protocol-neutral task-control surface.

The MCP transport currently exposes this through custom CortexRelay tools.
A future native MCP Tasks adapter can implement the same surface without
changing the scheduler, persistence, artifact, or worktree layers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from cortex_relay.core.models import TaskSpec


class TaskControl(Protocol):
    def submit(
        self,
        task: TaskSpec,
        *,
        group_id: str | None = None,
        depends_on: tuple[str, ...] = (),
        inherit_workspace_from: str | None = None,
        priority: int = 0,
        parent_task_id: str | None = None,
        max_depth: int = 8,
    ) -> dict[str, Any]: ...

    def status(self, task_id: str) -> dict[str, Any]: ...

    def events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]: ...

    def wait(
        self,
        task_id: str,
        *,
        timeout_seconds: float = 0,
    ) -> dict[str, Any]: ...

    def cancel(self, task_id: str) -> dict[str, Any]: ...

    def tasks(
        self,
        workspace: Path | None = None,
        *,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def artifact(self, task_id: str, *, create: bool = True) -> dict[str, Any]: ...

    def worktree(
        self,
        task_id: str,
        attempt: int | None = None,
    ) -> dict[str, Any]: ...

    def diff(
        self,
        task_id: str,
        attempt: int | None = None,
        max_bytes: int = 65536,
    ) -> dict[str, Any]: ...

    def apply(
        self,
        task_id: str,
        attempt: int | None = None,
    ) -> dict[str, Any]: ...

    def discard(
        self,
        task_id: str,
        attempt: int | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]: ...
