"""Inspect and explicitly hand off task-owned Git worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cortex_relay.observability import RunStore, TERMINAL_STATUSES
from cortex_relay.runtime.state_lock import FileLock


_SHA = re.compile(r"^[0-9a-f]{40,64}$")


def _git(cwd: Path, *args: str, input_bytes: bytes | None = None,
         env: dict[str, str] | None = None) -> bytes:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, input=input_bytes, env=env,
            check=True, capture_output=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}") from exc


def _text(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class WorktreeHandoff:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    def _select(self, task_id: str, attempt: int | None) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        workspace, record = self.store.find_task(task_id)
        if record.get("async") and record.get("status") not in TERMINAL_STATUSES:
            record = self.store.reconcile_task(workspace, task_id)
        attempts = record.get("worktree_attempts") or []
        if not attempts and record.get("worktree_path"):
            # Older tasks can still reveal their location, but cannot be mutated.
            attempts = [{"attempt": 1, "path": record["worktree_path"],
                         "branch": record.get("worktree_branch"),
                         "source_repository": str(workspace), "legacy": True}]
        if not attempts:
            raise ValueError(f"task has no isolated worktree: {task_id}")
        if attempt is None:
            if len(attempts) != 1:
                raise ValueError("attempt number is required when a task has multiple worktrees")
            selected = attempts[0]
        else:
            matches = [item for item in attempts if item.get("attempt") == attempt]
            if len(matches) != 1:
                raise ValueError(f"unknown worktree attempt: {task_id}/{attempt}")
            selected = matches[0]
        return workspace, record, selected

    def _inspect(self, workspace: Path, record: dict[str, Any],
                 item: dict[str, Any], *, managed: bool = False) -> dict[str, Any]:
        source = Path(str(item["source_repository"])).expanduser().resolve()
        path = Path(str(item["path"])).expanduser().resolve()
        branch = item.get("branch")
        base = item.get("base_commit")
        workspace_root = Path(_text(_git(workspace, "rev-parse", "--show-toplevel"))).resolve()
        if source != workspace_root and not (item.get("legacy") and source == workspace.resolve()):
            raise ValueError("worktree source does not match task workspace")
        root = (source.parent / ".cortex-worktrees" / source.name).resolve()
        if managed:
            if item.get("legacy") or not isinstance(base, str) or not _SHA.fullmatch(base):
                raise ValueError("task lacks managed worktree provenance")
            if not path.is_relative_to(root) or path == root:
                raise ValueError("worktree path is outside the managed root")
            if not isinstance(branch, str) or not branch.startswith("cortex/"):
                raise ValueError("worktree branch is not managed by CortexRelay")
        exists = path.is_dir()
        registered = False
        if exists:
            listing = _git(source, "worktree", "list", "--porcelain").decode("utf-8", "replace")
            entries = [part for part in listing.split("\n\n") if part.strip()]
            for entry in entries:
                lines = entry.splitlines()
                location = next((line[9:] for line in lines if line.startswith("worktree ")), None)
                listed_branch = next((line[7:] for line in lines if line.startswith("branch ")), None)
                if location and Path(location).resolve() == path and listed_branch == f"refs/heads/{branch}":
                    registered = True
                    break
            if managed and not registered:
                raise ValueError("Git no longer registers the expected worktree and branch")
            if registered and _text(_git(path, "rev-parse", "--show-toplevel")):
                actual_root = Path(_text(_git(path, "rev-parse", "--show-toplevel"))).resolve()
                if actual_root != path:
                    raise ValueError("recorded worktree path is not its Git root")
            if managed:
                _git(source, "cat-file", "-e", f"{base}^{{commit}}")
        elif managed:
            raise ValueError("managed worktree is missing")
        status = _git(path, "status", "--porcelain", "-z") if exists and registered else b""
        head = _text(_git(path, "rev-parse", "HEAD")) if exists and registered else None
        if managed and _text(_git(source, "rev-parse", f"refs/heads/{branch}")) != head:
            raise ValueError("worktree branch no longer points at its HEAD")
        return {
            "task_id": record["task_id"], "attempt": item.get("attempt"),
            "source_repository": str(source), "path": str(path),
            "branch": branch, "base_commit": base, "head": head,
            "exists": exists, "registered": registered,
            "task_status": record.get("status"),
            "handoff_status": item.get("handoff_status"),
            "status_preview": status.decode("utf-8", "replace")[:2048],
            "status_sha256": _sha(status),
            "managed": bool(base and not item.get("legacy") and path.is_relative_to(root)),
        }

    def worktree(self, task_id: str, attempt: int | None = None) -> dict[str, Any]:
        workspace, record, item = self._select(task_id, attempt)
        return self._inspect(workspace, record, item)

    def _snapshot(self, info: dict[str, Any]) -> dict[str, Any]:
        path = Path(info["path"])
        base = info["base_commit"]
        if not base:
            raise ValueError("legacy worktree has no recorded base commit")
        with tempfile.TemporaryDirectory(prefix="cortex-handoff-") as directory:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(directory) / "index")}
            _git(path, "read-tree", base, env=env)
            _git(path, "add", "-A", "--", ".", env=env)
            patch = _git(path, "diff", "--cached", "--binary", base, env=env)
            names = _git(path, "diff", "--cached", "--name-only", "-z", base, env=env)
        files = [name.decode("utf-8", "replace") for name in names.split(b"\0") if name]
        return {"patch": patch, "patch_sha256": _sha(patch), "files": files,
                "worker_head": info["head"]}

    def diff(self, task_id: str, attempt: int | None = None,
             max_bytes: int = 65536) -> dict[str, Any]:
        if not 1024 <= max_bytes <= 262144:
            raise ValueError("max_bytes must be between 1024 and 262144")
        workspace, record, item = self._select(task_id, attempt)
        info = self._inspect(workspace, record, item)
        if not info["exists"] or not info["registered"]:
            raise ValueError("worktree is missing or unregistered")
        snapshot = self._snapshot(info)
        patch = snapshot["patch"]
        return {**info, "files": snapshot["files"],
                "patch_sha256": snapshot["patch_sha256"],
                "patch_bytes": len(patch),
                "patch_preview": patch[:max_bytes].decode("utf-8", "replace"),
                "truncated": len(patch) > max_bytes}

    def _handoff_lock(self, source: Path) -> FileLock:
        key = _sha(str(source).encode("utf-8"))
        return FileLock(self.store.root / "handoff-locks" / f"{key}.lock")

    @staticmethod
    def _require_terminal(record: dict[str, Any]) -> None:
        if record.get("status") not in TERMINAL_STATUSES:
            raise ValueError("task must be terminal before worktree handoff")

    def apply(self, task_id: str, attempt: int | None = None) -> dict[str, Any]:
        workspace, record, item = self._select(task_id, attempt)
        self._require_terminal(record)
        source = workspace.resolve()
        with self._handoff_lock(source):
            workspace, record, item = self._select(task_id, attempt)
            self._require_terminal(record)
            if item.get("handoff_status") == "applied":
                return {"task_id": task_id, "attempt": item["attempt"],
                        "status": "already_applied", "application": item.get("application")}
            if item.get("handoff_status") == "discarded":
                raise ValueError("worktree was discarded")
            info = self._inspect(workspace, record, item, managed=True)
            if _git(source, "status", "--porcelain", "-z"):
                raise ValueError("source checkout has uncommitted changes")
            target_head = _text(_git(source, "rev-parse", "HEAD"))
            snapshot = self._snapshot(info)
            patch = snapshot["patch"]
            if not patch:
                raise ValueError("worker snapshot contains no changes to apply")
            # Check the exact patch against the current target, then recheck both snapshots.
            _git(source, "apply", "--check", "--binary", "-", input_bytes=patch)
            if _text(_git(source, "rev-parse", "HEAD")) != target_head or _git(source, "status", "--porcelain", "-z"):
                raise ValueError("source checkout changed during apply preflight")
            fresh = self._snapshot(self._inspect(workspace, record, item, managed=True))
            if (fresh["patch_sha256"] != snapshot["patch_sha256"]
                    or fresh["worker_head"] != snapshot["worker_head"]
                    or self._inspect(workspace, record, item, managed=True)["status_sha256"] != info["status_sha256"]):
                raise ValueError("worker snapshot changed during apply preflight")
            try:
                _git(source, "apply", "--binary", "-", input_bytes=patch)
            except ValueError as exc:
                changed = _git(source, "status", "--porcelain", "-z").decode("utf-8", "replace")
                raise ValueError(f"apply failed; current source changes: {changed}") from exc
            applied = {"target_head": target_head, "patch_sha256": snapshot["patch_sha256"],
                       "files": snapshot["files"],
                       "applied_at": datetime.now(timezone.utc).isoformat()}
            self.store.update_worktree_attempt(
                workspace, task_id, item["attempt"],
                handoff_status="applied", application=applied,
            )
            return {"task_id": task_id, "attempt": item["attempt"],
                    "status": "applied", "application": applied}

    @staticmethod
    def _snapshot_digest(info: dict[str, Any], snapshot: dict[str, Any]) -> str:
        values = {key: info.get(key) for key in
                  ("task_id", "attempt", "path", "branch", "base_commit", "head", "status_sha256")}
        values["patch_sha256"] = snapshot["patch_sha256"]
        return _sha(json.dumps(values, sort_keys=True).encode("utf-8"))

    def discard(self, task_id: str, attempt: int | None = None,
                confirmation_token: str | None = None) -> dict[str, Any]:
        workspace, record, item = self._select(task_id, attempt)
        self._require_terminal(record)
        source = workspace.resolve()
        with self._handoff_lock(source):
            workspace, record, item = self._select(task_id, attempt)
            self._require_terminal(record)
            if item.get("handoff_status") == "discarded":
                return {"task_id": task_id, "attempt": item["attempt"], "status": "already_discarded"}
            if item.get("handoff_status") == "applied":
                raise ValueError("applied worktree cannot be discarded through this tool")
            info = self._inspect(workspace, record, item, managed=True)
            snapshot = self._snapshot(info)
            digest = self._snapshot_digest(info, snapshot)
            if confirmation_token is None:
                token = secrets.token_urlsafe(32)
                self.store.update_worktree_attempt(
                    workspace, task_id, item["attempt"],
                    discard_confirmation={
                        "token_sha256": _sha(token.encode("utf-8")),
                        "snapshot_sha256": digest,
                        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                    },
                )
                return {"task_id": task_id, "attempt": item["attempt"],
                        "status": "confirmation_required", "confirmation_token": token,
                        "path": info["path"], "branch": info["branch"],
                        "files": snapshot["files"], "patch_sha256": snapshot["patch_sha256"]}
            confirmation = item.get("discard_confirmation") or {}
            if not secrets.compare_digest(
                _sha(confirmation_token.encode("utf-8")),
                str(confirmation.get("token_sha256") or ""),
            ):
                raise ValueError("discard confirmation token is invalid")
            expires = confirmation.get("expires_at")
            if not isinstance(expires, str) or datetime.now(timezone.utc) > datetime.fromisoformat(expires):
                raise ValueError("discard confirmation token has expired")
            if confirmation.get("snapshot_sha256") != digest:
                raise ValueError("worktree changed since discard confirmation")
            # The exact canonical target was checked against the managed root in _inspect.
            path = Path(info["path"])
            _git(source, "worktree", "remove", "--force", str(path))
            try:
                _git(source, "branch", "-D", str(info["branch"]))
            except ValueError as exc:
                self.store.update_worktree_attempt(
                    workspace, task_id, item["attempt"],
                    handoff_status="worktree_removed_branch_remaining",
                )
                raise ValueError(f"worktree removed but branch remains: {info['branch']}: {exc}") from exc
            discarded = {"discarded_at": datetime.now(timezone.utc).isoformat(),
                         "patch_sha256": snapshot["patch_sha256"], "files": snapshot["files"]}
            self.store.update_worktree_attempt(
                workspace, task_id, item["attempt"],
                handoff_status="discarded", discard_confirmation=None,
                discard=discarded,
            )
            return {"task_id": task_id, "attempt": item["attempt"],
                    "status": "discarded", "discard": discarded}
