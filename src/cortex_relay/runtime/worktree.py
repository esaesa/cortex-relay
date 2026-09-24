from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


_SAFE_ID = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base_commit: str = ""


class WorktreeManager:
    """Create isolated git worktrees for write-capable delegated tasks."""

    def __init__(self) -> None:
        self._git_lock = threading.Lock()

    def create(
        self,
        repository: Path,
        *,
        task_id: str,
        attempt: int = 1,
        base_ref: str = "HEAD",
        root: Path | None = None,
    ) -> Worktree:
        repository = repository.expanduser().resolve()
        if attempt < 1:
            raise ValueError("worktree attempt must be positive")
        safe_id = (_SAFE_ID.sub("-", task_id).strip("-") or "task") + f"-a{attempt}"
        branch = f"cortex/{safe_id}"
        parent = (
            root
            or repository.parent / ".cortex-worktrees" / repository.name
        ).resolve()
        path = parent / safe_id
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            raise FileExistsError(f"worktree path already exists: {path}")

        with self._git_lock:
            base_commit = subprocess.run(
                ["git", "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
                cwd=repository, check=True, text=True, capture_output=True,
            ).stdout.strip()
            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(path), base_commit],
                cwd=repository,
                check=True,
                text=True,
                capture_output=True,
            )
        return Worktree(path=path, branch=branch, base_commit=base_commit)

    def remove(self, repository: Path, worktree: Worktree, *, force: bool = False) -> None:
        argv = ["git", "worktree", "remove"]
        if force:
            argv.append("--force")
        argv.append(str(worktree.path))
        with self._git_lock:
            subprocess.run(
                argv,
                cwd=repository.expanduser().resolve(),
                check=True,
                text=True,
                capture_output=True,
            )
