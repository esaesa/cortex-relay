from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_SAFE_ID = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


class WorktreeManager:
    """Create isolated git worktrees for write-capable delegated tasks."""

    def create(
        self,
        repository: Path,
        *,
        task_id: str,
        base_ref: str = "HEAD",
        root: Path | None = None,
    ) -> Worktree:
        repository = repository.expanduser().resolve()
        safe_id = _SAFE_ID.sub("-", task_id).strip("-") or "task"
        branch = f"cortex/{safe_id}"
        parent = (
            root
            or repository.parent / ".cortex-worktrees" / repository.name
        ).resolve()
        path = parent / safe_id
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            raise FileExistsError(f"worktree path already exists: {path}")

        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(path), base_ref],
            cwd=repository,
            check=True,
            text=True,
            capture_output=True,
        )
        return Worktree(path=path, branch=branch)

    def remove(self, repository: Path, worktree: Worktree, *, force: bool = False) -> None:
        argv = ["git", "worktree", "remove"]
        if force:
            argv.append("--force")
        argv.append(str(worktree.path))
        subprocess.run(
            argv,
            cwd=repository.expanduser().resolve(),
            check=True,
            text=True,
            capture_output=True,
        )
