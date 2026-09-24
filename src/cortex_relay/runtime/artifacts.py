"""Immutable task artifacts for passing exact worker state through a DAG."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex_relay.observability import RunStore
from cortex_relay.runtime.state_lock import FileLock
from cortex_relay.runtime.worktree_handoff import WorktreeHandoff


@dataclass(frozen=True)
class TaskArtifact:
    artifact_id: str
    source_task_id: str
    workspace: str
    attempt: int | None
    base_commit: str
    worker_head: str
    patch_sha256: str
    files: tuple[str, ...]
    patch_path: str
    provider: str | None
    model: str | None
    tests: tuple[str, ...]
    result_sha256: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["files"] = list(self.files)
        data["tests"] = list(self.tests)
        return data


class ArtifactStore:
    """Persist exact worker patches outside the repository checkout."""

    def __init__(self, store: RunStore) -> None:
        self.store = store
        self.handoff = WorktreeHandoff(store)
        self.root = store.root / "artifacts"

    def create(self, task_id: str, attempt: int | None = None) -> dict[str, Any]:
        workspace, record = self.store.find_task(task_id)
        if record.get("status") != "success":
            raise ValueError("only successful tasks can produce workflow artifacts")

        existing = record.get("artifact_id")
        if isinstance(existing, str):
            artifact = self.get(existing)
            if artifact.get("source_task_id") == task_id:
                return artifact

        snapshot = self.handoff.artifact_snapshot(task_id, attempt)
        patch = snapshot["patch"]
        if not patch:
            raise ValueError("worker produced no patch to persist as an artifact")

        artifact_id = f"artifact-{uuid4().hex}"
        self.root.mkdir(parents=True, exist_ok=True)
        patch_path = self.root / f"{artifact_id}.patch"
        metadata_path = self.root / f"{artifact_id}.json"
        lock = FileLock(self.root / f"{artifact_id}.lock")
        with lock:
            _atomic_write_bytes(patch_path, patch)
            result = self.store.get_result(workspace, task_id)
            result_sha = (
                hashlib.sha256(
                    json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                if isinstance(result, dict)
                else None
            )
            artifact = TaskArtifact(
                artifact_id=artifact_id,
                source_task_id=task_id,
                workspace=str(workspace),
                attempt=snapshot.get("attempt"),
                base_commit=str(snapshot["base_commit"]),
                worker_head=str(snapshot["worker_head"]),
                patch_sha256=str(snapshot["patch_sha256"]),
                files=tuple(str(item) for item in snapshot["files"]),
                patch_path=str(patch_path),
                provider=record.get("provider"),
                model=record.get("model"),
                tests=tuple(str(item) for item in record.get("tests") or []),
                result_sha256=result_sha,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            _atomic_write_json(metadata_path, artifact.to_dict())
        self.store.update_task(
            workspace,
            task_id,
            artifact_id=artifact_id,
            artifact_sha256=artifact.patch_sha256,
        )
        return artifact.to_dict()

    def get(self, artifact_id: str) -> dict[str, Any]:
        _validate_artifact_id(artifact_id)
        path = self.root / f"{artifact_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unknown or corrupt artifact: {artifact_id}") from exc
        if not isinstance(data, dict) or data.get("artifact_id") != artifact_id:
            raise ValueError(f"artifact metadata mismatch: {artifact_id}")
        patch_path = Path(str(data.get("patch_path", ""))).expanduser().resolve()
        expected = (self.root / f"{artifact_id}.patch").resolve()
        if patch_path != expected or not patch_path.is_file():
            raise ValueError(f"artifact patch is missing or misplaced: {artifact_id}")
        patch = patch_path.read_bytes()
        if hashlib.sha256(patch).hexdigest() != data.get("patch_sha256"):
            raise ValueError(f"artifact patch digest mismatch: {artifact_id}")
        return data

    def for_task(self, task_id: str, *, create: bool = True) -> dict[str, Any]:
        _, record = self.store.find_task(task_id)
        artifact_id = record.get("artifact_id")
        if isinstance(artifact_id, str):
            return self.get(artifact_id)
        if not create:
            raise ValueError(f"task has no artifact: {task_id}")
        return self.create(task_id)

    def apply_to_worktree(self, artifact_id: str, target: Path) -> dict[str, Any]:
        artifact = self.get(artifact_id)
        target = Path(target).expanduser().resolve()
        patch = Path(artifact["patch_path"]).read_bytes()
        if subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=target,
            capture_output=True,
        ).stdout:
            raise ValueError("target worktree must be clean before artifact inheritance")
        checked = subprocess.run(
            ["git", "apply", "--check", "--binary", "-"],
            cwd=target,
            input=patch,
            capture_output=True,
        )
        if checked.returncode != 0:
            detail = checked.stderr.decode("utf-8", "replace").strip()
            raise ValueError(f"artifact does not apply cleanly to target worktree: {detail}")
        applied = subprocess.run(
            ["git", "apply", "--binary", "-"],
            cwd=target,
            input=patch,
            capture_output=True,
        )
        if applied.returncode != 0:
            detail = applied.stderr.decode("utf-8", "replace").strip()
            raise ValueError(f"artifact application failed: {detail}")
        return {
            "artifact_id": artifact_id,
            "source_task_id": artifact["source_task_id"],
            "patch_sha256": artifact["patch_sha256"],
            "files": artifact["files"],
        }


def _validate_artifact_id(value: str) -> None:
    if not value.startswith("artifact-") or len(value) != len("artifact-") + 32:
        raise ValueError("invalid artifact id")
    suffix = value[len("artifact-"):]
    if any(char not in "0123456789abcdef" for char in suffix):
        raise ValueError("invalid artifact id")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)
