from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import textwrap

from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.runtime.agent_backend import AgentStoreBackend
from cortex_relay.runtime.agent_store import AgentStore
from cortex_relay.runtime.progress import ProgressEvent
from cortex_relay.runtime.state_lock import FileLock, lock_is_held

ACTIVE_STATUSES = {"routing", "preparing", "running", "fallback", "queued"}
TERMINAL_STATUSES = {
    "success", "error", "timeout", "unavailable", "cancelled",
    "blocked", "interrupted", "failed_gate", "budget_exceeded",
}
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def _task_locked(method):
    @wraps(method)
    def locked(self, workspace, task_or_id, *args, **kwargs):
        task_id = task_or_id.task_id if isinstance(task_or_id, ProgressEvent) else task_or_id
        try:
            lock = self._task_lock(workspace, task_id)
            lock.acquire()
        except OSError:
            return None
        try:
            return method(self, workspace, task_or_id, *args, **kwargs)
        finally:
            lock.release()

    return locked


class RunStore:
    """Cross-process, per-workspace runtime observability state."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        agent_store: AgentStoreBackend | None = None,
    ) -> None:
        self.root = (root or _default_state_root()).expanduser().resolve()
        self.agent_store = agent_store
        self._session_locks: dict[str, FileLock] = {}

    def start_session(
        self,
        workspace: Path,
        *,
        profile: str | None,
        provider: str,
        model: str | None,
        reasoning: str,
        preset: str | None = None,
        session_id: str | None = None,
    ) -> str:
        session_id = session_id or f"session-{uuid4().hex[:12]}"
        now = _utc_now()
        record = {
            "schema_version": 1,
            "kind": "session",
            "session_id": session_id,
            "workspace": str(_workspace(workspace)),
            "profile": profile,
            "preset": preset,
            "provider": provider,
            "model": model,
            "reasoning": reasoning,
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "exit_code": None,
        }
        lock_path = self._session_lock_path(workspace, session_id)
        session_lock = FileLock(lock_path)
        session_lock.acquire()
        try:
            self._write_record(self._session_path(workspace, session_id), record, required=True)
        except Exception:
            session_lock.release()
            raise
        self._session_locks[str(lock_path)] = session_lock
        return session_id

    def end_session(
        self,
        workspace: Path,
        session_id: str,
        *,
        exit_code: int,
    ) -> None:
        path = self._session_path(workspace, session_id)
        record = self._read_record(path) or {}
        now = _utc_now()
        record.update(
            {
                "status": (
                    "success" if exit_code == 0 else
                    "cancelled" if exit_code == 130 else "error"
                ),
                "updated_at": now,
                "completed_at": now,
                "exit_code": exit_code,
            }
        )
        try:
            self._write_record(path, record, required=True)
        finally:
            lock_path = self._session_lock_path(workspace, session_id)
            session_lock = self._session_locks.pop(str(lock_path), None)
            if session_lock is not None:
                session_lock.release()

    def start_task(
        self,
        task: TaskSpec,
        *,
        task_id: str | None = None,
        async_task: bool = False,
        owner_instance_id: str | None = None,
        group_id: str | None = None,
        depends_on: tuple[str, ...] = (),
        priority: int = 0,
        inherit_workspace_from: str | None = None,
    ) -> str:
        task_id = _validated_task_id(task_id or f"task-{uuid4().hex}")
        path = self._task_path(task.workspace, task_id)
        if path.exists():
            raise FileExistsError(f"task id already exists: {task_id}")
        now = _utc_now()
        session = _session_from_environment()
        record = {
            "schema_version": 1,
            "kind": "task",
            "task_id": task_id,
            "async": async_task,
            "owner_instance_id": owner_instance_id,
            "owner_heartbeat_at": now if owner_instance_id else None,
            "group_id": group_id,
            "depends_on": list(depends_on),
            "blocked_by": [],
            "priority": int(priority),
            "queue_position": None,
            "queued_reason": None,
            "inherit_workspace_from": inherit_workspace_from,
            "inherited_artifact_id": None,
            "artifact_id": None,
            "artifact_sha256": None,
            "context": task.context.to_dict() if task.context else None,
            "budget": task.budget.to_dict(),
            "quality_gates": task.quality_gates.to_dict(),
            "session_id": session.get("session_id"),
            "host_profile": session.get("host_profile"),
            "host_model": session.get("host_model"),
            "host_reasoning": session.get("host_reasoning"),
            "workspace": str(_workspace(task.workspace)),
            "role": task.role,
            "objective": task.objective,
            "status": "routing",
            "profile": task.profile,
            "preset": task.preset,
            "provider": task.provider,
            "model": task.model,
            "reasoning": task.reasoning,
            "access": task.access,
            "billing_class": None,
            "attempt": 0,
            "attempts": [],
            "worktree_path": None,
            "worktree_branch": None,
            "worktree_attempts": [],
            "handoff_status": None,
            "result_path": None,
            "output_path": None,
            "output_chars": 0,
            "output_bytes": 0,
            "output_sha256": None,
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "duration_seconds": None,
            "usage": {},
            "usage_summary": {},
            "tests": [],
            "changed_files": [],
            "risks": [],
            "summary": None,
            "error": None,
            "conversation_id": None,
            "agent_session_id": None,
            "agent_session_ids": [],
            "owner_pid": os.getpid(),
            "timeout_seconds": task.timeout_seconds,
            "current_activity": None,
            "current_tool": None,
            "current_command": None,
            "current_path": None,
            "last_event_at": None,
            "progress_sequence": 0,
            "recent_events": [],
            "progress_files": [],
            "process_alive": False,
            "last_heartbeat_at": None,
        }
        # Publish the task record and global async index under the same per-task
        # lock. The index is only visible after the task record is durable, and
        # indexed readers acquire the same lock before trusting the pair.
        with self._task_lock(task.workspace, task_id):
            if path.exists():
                raise FileExistsError(f"task id already exists: {task_id}")
            self._write_record(path, record, required=async_task)
            if async_task:
                index = {
                    "schema_version": 1,
                    "task_id": task_id,
                    "workspace": str(_workspace(task.workspace)),
                    "created_at": now,
                }
                try:
                    self._write_record(self._index_path(task_id), index, required=True)
                except OSError:
                    path.unlink(missing_ok=True)
                    raise
        return task_id

    def find_task(self, task_id: str) -> tuple[Path, dict[str, Any]]:
        """Resolve a task handle without a process-local future."""

        _checked_lookup_id(task_id)
        index_path = self._index_path(task_id) if _TASK_ID.fullmatch(task_id) else None
        if index_path is not None and index_path.exists():
            index = self._read_record(index_path)
            if not index or index.get("task_id") != task_id:
                raise ValueError(f"invalid task index: {task_id}")
            workspace_value = index.get("workspace")
            if not isinstance(workspace_value, str):
                raise ValueError(f"task index lacks workspace: {task_id}")
            workspace = _workspace(Path(workspace_value))
            with self._task_lock(workspace, task_id):
                # Re-read the index after taking the task lock so an indexed
                # lookup observes the same publication boundary as start_task.
                latest_index = self._read_record(index_path)
                if (
                    not latest_index
                    or latest_index.get("task_id") != task_id
                    or latest_index.get("workspace") != str(workspace)
                ):
                    raise ValueError(f"task index changed while resolving: {task_id}")
                record = self.get_task(workspace, task_id)
                if (
                    not record
                    or record.get("task_id") != task_id
                    or record.get("workspace") != str(workspace)
                ):
                    raise ValueError(f"task index does not match its record: {task_id}")
            return workspace, record

        matches: list[tuple[Path, dict[str, Any]]] = []
        for directory in self.root.glob("*/tasks"):
            for path in directory.glob("*.json"):
                record = self._read_record(path)
                if not record or record.get("task_id") != task_id:
                    continue
                workspace_value = record.get("workspace")
                if isinstance(workspace_value, str):
                    workspace = _workspace(Path(workspace_value))
                    if self._task_path(workspace, task_id) == path:
                        matches.append((workspace, record))
        if len(matches) != 1:
            raise ValueError(f"unknown or ambiguous task id: {task_id}")
        return matches[0]

    def get_result(self, workspace: Path, task_id: str) -> dict[str, Any] | None:
        record = self.get_task(workspace, task_id)
        if not record:
            return None
        path = self._result_path(workspace, task_id)
        if record.get("result_path") and record["result_path"] != str(path):
            raise ValueError(f"task result path does not match its record: {task_id}")
        if not path.exists() and not record.get("result_path") and not record.get("async"):
            return None
        result = self._read_record(path)
        if result is None and record.get("status") in TERMINAL_STATUSES:
            raise ValueError(f"terminal task result is missing or corrupt: {task_id}")
        return result

    def get_output(
        self,
        workspace: Path,
        task_id: str,
        *,
        offset: int = 0,
        max_chars: int = 65536,
    ) -> dict[str, Any]:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if not 1 <= max_chars <= 200000:
            raise ValueError("max_chars must be between 1 and 200000")
        record = self.get_task(workspace, task_id)
        if not record:
            raise ValueError(f"unknown task id: {task_id}")
        output_path = self._output_path(workspace, task_id)
        recorded_path = record.get("output_path")
        if recorded_path and recorded_path != str(output_path):
            raise ValueError(f"task output path does not match its record: {task_id}")

        text: str | None = None
        if output_path.exists():
            try:
                text = output_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"task output is unreadable: {task_id}") from exc
        else:
            result = self.get_result(workspace, task_id)
            if result is not None and isinstance(result.get("final_text"), str):
                text = result["final_text"]

        if text is None:
            raise ValueError(f"task final output is unavailable: {task_id}")

        total = len(text)
        start = min(offset, total)
        end = min(total, start + max_chars)
        chunk = text[start:end]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {
            "task_id": task_id,
            "text": chunk,
            "offset": start,
            "next_offset": end,
            "total_chars": total,
            "complete": end >= total,
            "sha256": digest,
        }

    def indexed_task_ids(self) -> list[str]:
        directory = self.root / "task-index"
        return [path.stem for path in directory.glob("*.json")] if directory.exists() else []

    def owner_lock_path(self, owner_instance_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", owner_instance_id):
            raise ValueError("invalid owner instance id")
        return self.root / "owners" / f"{owner_instance_id}.lock"

    @_task_locked
    def record_owner_heartbeat(self, workspace: Path, task_id: str, owner_instance_id: str) -> None:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path)
        if not record or record.get("owner_instance_id") != owner_instance_id:
            return
        if record.get("status") in TERMINAL_STATUSES:
            return
        record["owner_heartbeat_at"] = _utc_now()
        self._write_record(path, record, required=True)

    @_task_locked
    def reconcile_task(self, workspace: Path, task_id: str) -> dict[str, Any]:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path)
        if not record or record.get("task_id") != task_id:
            raise ValueError(f"task record is missing or mismatched: {task_id}")
        owner_id = record.get("owner_instance_id")
        if not record.get("async") or record.get("status") in TERMINAL_STATUSES:
            return record
        if not isinstance(owner_id, str) or lock_is_held(self.owner_lock_path(owner_id)):
            return record

        result_path = self._result_path(workspace, task_id)
        result = self._read_record(result_path)
        if result_path.exists() and not _valid_stored_result(result):
            raise ValueError(f"persisted task result is corrupt: {task_id}")
        if result is None:
            result = TaskResult(
                status="interrupted", provider=str(record.get("provider") or "auto"),
                model=record.get("model"),
                summary="Task control was lost when its MCP owner stopped.",
                error="No provider completion was observed; the worker was not resumed or killed.",
                metadata={"task_id": task_id},
            ).to_dict()
            self._write_record(result_path, result, required=True)

        final_text = str(result.get("final_text") or "")
        output_path = self._output_path(workspace, task_id)
        self._write_text(output_path, final_text, required=True)
        output_bytes = final_text.encode("utf-8")
        now = _utc_now()
        record.update(
            status=result["status"], result_path=str(result_path),
            output_path=str(output_path),
            output_chars=len(final_text),
            output_bytes=len(output_bytes),
            output_sha256=hashlib.sha256(output_bytes).hexdigest(),
            completed_at=now, updated_at=now, process_alive=False,
            summary=_truncate(str(result.get("summary") or ""), 420),
            error=_truncate(str(result["error"]), 420) if result.get("error") else None,
            provider=result.get("provider", record.get("provider")),
            model=result.get("model", record.get("model")),
            duration_seconds=result.get("duration_seconds"),
            usage=result.get("usage") or {},
            usage_summary=summarize_usage(result.get("usage") or {}),
            tests=result.get("tests") or [],
            changed_files=result.get("changed_files") or [],
            risks=result.get("risks") or [],
            conversation_id=result.get("conversation_id"),
        )
        self._write_record(path, record, required=True)
        return record

    @_task_locked
    def update_task(
        self,
        workspace: Path,
        task_id: str,
        **updates: Any,
    ) -> None:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path) or {}
        if record.get("task_id") != task_id:
            raise ValueError(f"task record is missing or mismatched: {task_id}")
        record.update(updates)
        record["updated_at"] = _utc_now()
        self._write_record(path, record)

    @_task_locked
    def record_worktree_attempt(
        self, workspace: Path, task_id: str, attempt: dict[str, Any]
    ) -> None:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path)
        if not record or record.get("task_id") != task_id:
            raise ValueError(f"task record is missing or mismatched: {task_id}")
        attempts = list(record.get("worktree_attempts") or [])
        number = attempt.get("attempt")
        if any(item.get("attempt") == number for item in attempts):
            raise ValueError(f"worktree attempt already recorded: {task_id}/{number}")
        attempts.append(attempt)
        record.update(
            worktree_attempts=attempts,
            worktree_path=attempt["path"],
            worktree_branch=attempt["branch"],
            worktree_base_commit=attempt["base_commit"],
            updated_at=_utc_now(),
        )
        self._write_record(path, record, required=True)

    @_task_locked
    def update_worktree_attempt(
        self, workspace: Path, task_id: str, number: int, **updates: Any
    ) -> dict[str, Any]:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path)
        if not record or record.get("task_id") != task_id:
            raise ValueError(f"task record is missing or mismatched: {task_id}")
        attempts = list(record.get("worktree_attempts") or [])
        for index, attempt in enumerate(attempts):
            if attempt.get("attempt") == number:
                changed = {**attempt, **updates}
                attempts[index] = changed
                record["worktree_attempts"] = attempts
                record["handoff_status"] = changed.get("handoff_status")
                record["updated_at"] = _utc_now()
                self._write_record(path, record, required=True)
                return changed
        raise ValueError(f"unknown worktree attempt: {task_id}/{number}")

    @_task_locked
    def record_progress(self, workspace: Path, event: ProgressEvent) -> None:
        """Keep only bounded, observable activity for a running task."""

        path = self._task_path(workspace, event.task_id)
        record = self._read_record(path)
        if not record or record.get("status") in TERMINAL_STATUSES:
            return
        if event.phase == "response" and record.get("current_activity") == event.activity:
            previous = record.get("last_event_at")
            if isinstance(previous, str):
                try:
                    if (datetime.now(timezone.utc) - datetime.fromisoformat(previous)).total_seconds() < 2:
                        return
                except ValueError:
                    pass
        now = _utc_now()
        sequence = int(record.get("progress_sequence") or 0) + 1
        activity = asdict(event)
        activity.update(sequence=sequence, at=now)
        self._append_event(workspace, event.task_id, activity)
        recent = record.get("recent_events")
        recent = recent if isinstance(recent, list) else []
        record.update(
            current_activity=event.activity,
            current_tool=event.tool,
            current_command=event.command,
            current_path=event.path,
            current_exit_code=event.exit_code,
            current_duration_seconds=event.duration_seconds,
            current_output_preview=event.output_preview,
            current_error_preview=event.error_preview,
            current_subagents=activity["subagents"],
            current_plan=activity["plan"] if event.plan else record.get("current_plan", []),
            last_event_at=now,
            progress_sequence=sequence,
            recent_events=[*recent[-49:], activity],
            updated_at=now,
        )
        if event.input_tokens is not None:
            record["progress_input_tokens"] = event.input_tokens
        if event.output_tokens is not None:
            record["progress_output_tokens"] = event.output_tokens
        if event.total_tokens is not None:
            record["progress_total_tokens"] = event.total_tokens
        if event.cache_tokens is not None:
            record["progress_cache_tokens"] = event.cache_tokens
        if event.files:
            known = record.get("progress_files") or []
            record["progress_files"] = list(dict.fromkeys([*known, *event.files]))[:100]
        self._write_record(path, record)

    @_task_locked
    def record_heartbeat(self, workspace: Path, task_id: str, pid: int, alive: bool) -> None:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path)
        if not record or record.get("status") in TERMINAL_STATUSES:
            return
        record.update(provider_pid=pid, process_alive=alive, last_heartbeat_at=_utc_now())
        self._write_record(path, record)

    def list_events(
        self, workspace: Path, task_id: str, *, after_sequence: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        if after_sequence < 0 or not 1 <= limit <= 100:
            raise ValueError("after_sequence must be non-negative and limit must be 1..100")
        record = self.get_task(workspace, task_id)
        if record is None:
            raise ValueError(f"unknown task id: {task_id}")
        path = self._events_path(workspace, task_id)
        events: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and int(event.get("sequence") or 0) > after_sequence:
                        events.append(event)
                        if len(events) >= limit:
                            break
        except OSError:
            events = [
                event for event in record.get("recent_events", [])
                if isinstance(event, dict) and int(event.get("sequence") or 0) > after_sequence
            ][:limit]
        return {
            "task_id": task_id,
            "events": events,
            "next_sequence": int(events[-1]["sequence"]) if events else after_sequence,
        }

    def _append_event(self, workspace: Path, task_id: str, event: dict[str, Any]) -> None:
        path = self._events_path(workspace, task_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass

    @_task_locked
    def complete_task(
        self,
        workspace: Path,
        task_id: str,
        result: TaskResult,
    ) -> dict[str, Any]:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path) or {}
        if record.get("task_id") != task_id:
            raise ValueError(f"task record is missing or mismatched: {task_id}")
        now = _utc_now()
        usage_summary = summarize_usage(result.usage)
        metadata = result.metadata
        result_path = self._result_path(workspace, task_id)
        output_path = self._output_path(workspace, task_id)
        final_text = result.final_text
        output_bytes = final_text.encode("utf-8")
        self._write_text(output_path, final_text, required=True)
        self._write_record(result_path, result.to_dict(), required=True)
        record.update(
            {
                "status": result.status,
                "result_path": str(result_path),
                "output_path": str(output_path),
                "output_chars": len(final_text),
                "output_bytes": len(output_bytes),
                "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
                "process_alive": False,
                "provider": result.provider,
                "model": result.model,
                "updated_at": now,
                "completed_at": now,
                "duration_seconds": result.duration_seconds,
                "usage": result.usage,
                "usage_summary": usage_summary,
                "tests": list(result.tests),
                "changed_files": list(result.changed_files),
                "risks": list(result.risks),
                "summary": _truncate(result.summary, 420),
                "error": _truncate(result.error, 420) if result.error else None,
                "conversation_id": result.conversation_id,
                "worktree_path": metadata.get("worktree_path", record.get("worktree_path")),
                "worktree_branch": metadata.get("worktree_branch", record.get("worktree_branch")),
                "attempts": metadata.get("routing_attempts", record.get("attempts", [])),
                "billing_class": metadata.get("billing_class", record.get("billing_class")),
                "profile": metadata.get("profile", record.get("profile")),
            }
        )
        self._write_record(path, record, required=True)
        return record

    def list_tasks(
        self,
        workspace: Path,
        *,
        limit: int = 20,
        active_only: bool = False,
        completed_only: bool = False,
    ) -> list[dict[str, Any]]:
        directory = self._workspace_dir(workspace) / "tasks"
        records = self._read_many(directory, "*.json")
        if active_only:
            for index, item in enumerate(records):
                if item.get("async") and item.get("status") in ACTIVE_STATUSES:
                    try:
                        records[index] = self.reconcile_task(workspace, str(item["task_id"]))
                    except (OSError, ValueError, KeyError):
                        continue
            records = [item for item in records if item.get("status") in ACTIVE_STATUSES]
        if completed_only:
            records = [item for item in records if item.get("status") in TERMINAL_STATUSES]
        records.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        return records[: max(1, limit)]

    def get_task(self, workspace: Path, task_id: str) -> dict[str, Any] | None:
        _checked_lookup_id(task_id)
        record = self._read_record(self._task_path(workspace, task_id))
        return record if record and record.get("task_id") == task_id else None

    def list_sessions(
        self,
        workspace: Path,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        directory = self._workspace_dir(workspace) / "sessions"
        records = self._read_many(directory, "*.json")
        resolved_workspace = _workspace(workspace)
        observed: list[dict[str, Any]] = []
        for record in records:
            if record.get("status") == "running":
                session_id = record.get("session_id")
                if isinstance(session_id, str):
                    lock_path = self._session_lock_path(resolved_workspace, session_id)
                    if (str(lock_path) not in self._session_locks
                            and not lock_is_held(lock_path)):
                        # Re-read after the lease probe so a graceful shutdown that
                        # raced this snapshot keeps its recorded terminal status.
                        latest = self._read_record(self._session_path(resolved_workspace, session_id))
                        record = latest or record
                        if record.get("status") == "running":
                            record = {
                                **record,
                                "status": "interrupted",
                                "owner_unavailable": True,
                            }
            observed.append(record)
        observed.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        return observed[: max(1, limit)]

    def snapshot(
        self,
        workspace: Path,
        *,
        limit: int = 20,
        active_only: bool = False,
        completed_only: bool = False,
    ) -> dict[str, Any]:
        tasks = self.list_tasks(
            workspace,
            limit=limit,
            active_only=active_only,
            completed_only=completed_only,
        )
        sessions = self.list_sessions(workspace, limit=5)

        agent_store = self.agent_store or AgentStore(self.root)
        agents: list[dict[str, Any]] = []
        all_agents: list[dict[str, Any]] = []
        if not completed_only:
            all_agents = agent_store.list(
                workspace=_workspace(workspace),
                active_only=False,
            )
            agents = [
                agent
                for agent in all_agents
                if not active_only or agent.get("state") in {"starting", "running"}
            ][: max(1, limit)]
            for agent in agents:
                session_id = agent.get("session_id")
                if not isinstance(session_id, str):
                    continue
                try:
                    recent = agent_store.recent_events(session_id, limit=5)
                except (OSError, ValueError):
                    recent = []
                agent["recent_events"] = recent
                agent["last_event"] = recent[-1] if recent else None
                agent["current_activity"] = (
                    _agent_event_summary(recent[-1]) if recent else None
                )

        active = sum(item.get("status") in ACTIVE_STATUSES for item in tasks)
        success = sum(item.get("status") == "success" for item in tasks)
        failed = sum(
            item.get("status") in {
                "error", "timeout", "unavailable", "cancelled", "blocked",
                "interrupted", "failed_gate", "budget_exceeded",
            }
            for item in tasks
        )
        agent_running = sum(
            item.get("state") in {"starting", "running"} for item in all_agents
        )
        agent_idle = sum(item.get("state") == "idle" for item in all_agents)
        agent_failed = sum(
            item.get("state") in {"failed", "interrupted"} for item in all_agents
        )
        agent_closed = sum(item.get("state") == "closed" for item in all_agents)

        total_input = 0
        total_output = 0
        total_tokens = 0
        total_cost = 0.0
        cost_known = False
        for item in tasks:
            usage = item.get("usage_summary")
            if not isinstance(usage, dict):
                continue
            total_input += int(usage.get("input_tokens") or 0)
            total_output += int(usage.get("output_tokens") or 0)
            total_tokens += int(usage.get("total_tokens") or 0)
            cost = usage.get("cost")
            if isinstance(cost, (int, float)):
                total_cost += float(cost)
                cost_known = True

        return {
            "workspace": str(_workspace(workspace)),
            "state_directory": str(self._workspace_dir(workspace)),
            "sessions": sessions,
            "tasks": tasks,
            "agents": agents,
            "active_only": active_only,
            "summary": {
                "active": active,
                "success": success,
                "failed": failed,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_tokens,
                "cost": total_cost if cost_known else None,
            },
            "agent_summary": {
                "running": agent_running,
                "idle": agent_idle,
                "failed": agent_failed,
                "closed": agent_closed,
                "total": len(all_agents),
            },
        }

    def clear_completed(self, workspace: Path) -> int:
        directory = self._workspace_dir(workspace) / "tasks"
        removed = 0
        if not directory.exists():
            return 0
        protected_dependencies = {
            dependency
            for active in self._read_many(directory, "*.json")
            if active.get("status") in ACTIVE_STATUSES
            for dependency in (active.get("depends_on") or [])
            if isinstance(dependency, str)
        }
        for path in directory.glob("*.json"):
            record = self._read_record(path)
            if record and record.get("status") in TERMINAL_STATUSES:
                task_id = record.get("task_id")
                if not isinstance(task_id, str) or path != self._task_path(workspace, task_id):
                    continue
                if task_id in protected_dependencies:
                    continue
                try:
                    with self._task_lock(workspace, task_id):
                        current = self._read_record(path)
                        if not current or current.get("status") not in TERMINAL_STATUSES:
                            continue
                        path.unlink()
                        self._events_path(workspace, task_id).unlink(missing_ok=True)
                        self._result_path(workspace, task_id).unlink(missing_ok=True)
                        self._output_path(workspace, task_id).unlink(missing_ok=True)
                        self._remove_artifact_files(current)
                        if _TASK_ID.fullmatch(task_id):
                            index_path = self._index_path(task_id)
                            index = self._read_record(index_path)
                            if index and index.get("workspace") == str(_workspace(workspace)):
                                index_path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    pass
        return removed

    def group_tasks(self, workspace: Path, group_id: str) -> list[dict[str, Any]]:
        records = [
            item for item in self.list_tasks(workspace, limit=100000)
            if item.get("group_id") == group_id
        ]
        records.sort(
            key=lambda item: (
                int(item.get("priority") or 0),
                str(item.get("started_at", "")),
                str(item.get("task_id", "")),
            ),
            reverse=True,
        )
        return records

    def usage_totals(
        self,
        workspace: Path,
        *,
        group_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        directory = self._workspace_dir(workspace) / "tasks"
        total_tokens = 0
        total_cost = 0.0
        cost_known = False
        premium_tasks = 0
        for record in self._read_many(directory, "*.json"):
            if group_id is not None and record.get("group_id") != group_id:
                continue
            if session_id is not None and record.get("session_id") != session_id:
                continue
            usage = record.get("usage_summary")
            if isinstance(usage, dict):
                total_tokens += int(usage.get("total_tokens") or 0)
                cost = usage.get("cost")
                if isinstance(cost, (int, float)):
                    total_cost += float(cost)
                    cost_known = True
            if record.get("billing_class") and "premium" in str(record.get("billing_class")).lower():
                premium_tasks += 1
        return {
            "total_tokens": total_tokens,
            "cost": total_cost if cost_known else None,
            "premium_tasks": premium_tasks,
        }

    def gc(
        self,
        *,
        workspace: Path | None = None,
        retention_days: int = 30,
        max_completed_tasks: int = 1000,
        max_event_log_mb: int = 10,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Prune old terminal state without deleting active tasks or worktrees."""

        cutoff = datetime.now(timezone.utc).timestamp() - retention_days * 86400
        candidates: list[tuple[float, Path, dict[str, Any]]] = []
        active_dependencies: set[str] = set()

        task_dirs = (
            [self._workspace_dir(workspace) / "tasks"]
            if workspace is not None
            else list(self.root.glob("*/tasks"))
        )
        for task_dir in task_dirs:
            if not task_dir.exists():
                continue
            for path in task_dir.glob("*.json"):
                record = self._read_record(path)
                if not record:
                    continue
                if record.get("status") in ACTIVE_STATUSES:
                    active_dependencies.update(
                        item for item in (record.get("depends_on") or [])
                        if isinstance(item, str)
                    )
                    continue
                if record.get("status") not in TERMINAL_STATUSES:
                    continue
                completed = record.get("completed_at") or record.get("updated_at")
                try:
                    stamp = datetime.fromisoformat(str(completed)).timestamp()
                except (TypeError, ValueError):
                    stamp = path.stat().st_mtime
                candidates.append((stamp, path, record))

        candidates.sort(key=lambda item: item[0], reverse=True)
        keep_ids = {
            str(record.get("task_id"))
            for _, _, record in candidates[:max_completed_tasks]
            if isinstance(record.get("task_id"), str)
        }

        planned: list[dict[str, Any]] = []
        for stamp, path, record in candidates:
            task_id = record.get("task_id")
            if not isinstance(task_id, str) or task_id in active_dependencies:
                continue
            event_path = path.parent.parent / "events" / f"{_safe_id(task_id)}.jsonl"
            oversized = (
                event_path.exists()
                and event_path.stat().st_size > max_event_log_mb * 1024 * 1024
            )
            expired = stamp < cutoff
            over_count = task_id not in keep_ids
            if not (expired or over_count or oversized):
                continue
            entry = {
                "task_id": task_id,
                "workspace": record.get("workspace"),
                "expired": expired,
                "over_count": over_count,
                "oversized_events": oversized,
            }
            planned.append(entry)
            if dry_run:
                continue
            workspace_value = record.get("workspace")
            if not isinstance(workspace_value, str):
                continue
            workspace = _workspace(Path(workspace_value))
            try:
                with self._task_lock(workspace, task_id):
                    current = self._read_record(self._task_path(workspace, task_id))
                    if not current or current.get("status") not in TERMINAL_STATUSES:
                        continue
                    self._task_path(workspace, task_id).unlink(missing_ok=True)
                    self._events_path(workspace, task_id).unlink(missing_ok=True)
                    self._result_path(workspace, task_id).unlink(missing_ok=True)
                    self._output_path(workspace, task_id).unlink(missing_ok=True)
                    self._remove_artifact_files(current)
                    index = self._index_path(task_id)
                    indexed = self._read_record(index)
                    if indexed and indexed.get("workspace") == str(workspace):
                        index.unlink(missing_ok=True)
            except OSError:
                continue

        agent_store = self.agent_store or AgentStore(self.root)
        agent_gc = agent_store.gc(
            workspace=workspace,
            retention_days=retention_days,
            max_completed_roots=max_completed_tasks,
            dry_run=dry_run,
        )

        return {
            "dry_run": dry_run,
            "workspace": str(_workspace(workspace)) if workspace is not None else None,
            "retention_days": retention_days,
            "max_completed_tasks": max_completed_tasks,
            "max_event_log_mb": max_event_log_mb,
            "count": len(planned),
            "tasks": planned,
            "agent_count": agent_gc["count"],
            "agents": agent_gc["agents"],
        }

    def _remove_artifact_files(self, record: dict[str, Any]) -> None:
        artifact_id = record.get("artifact_id")
        if not isinstance(artifact_id, str):
            return
        if not re.fullmatch(r"artifact-[0-9a-f]{32}", artifact_id):
            return
        artifact_root = self.root / "artifacts"
        for suffix in (".json", ".patch", ".lock"):
            try:
                (artifact_root / f"{artifact_id}{suffix}").unlink(missing_ok=True)
            except OSError:
                continue

    def _workspace_dir(self, workspace: Path) -> Path:
        resolved = str(_workspace(workspace))
        key = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
        return self.root / key

    def _task_path(self, workspace: Path, task_id: str) -> Path:
        return self._workspace_dir(workspace) / "tasks" / f"{_safe_id(task_id)}.json"

    def _index_path(self, task_id: str) -> Path:
        return self.root / "task-index" / f"{_validated_task_id(task_id)}.json"

    def _result_path(self, workspace: Path, task_id: str) -> Path:
        return self._workspace_dir(workspace) / "results" / f"{_safe_id(task_id)}.json"

    def _output_path(self, workspace: Path, task_id: str) -> Path:
        return self._workspace_dir(workspace) / "outputs" / f"{_safe_id(task_id)}.txt"

    def _task_lock(self, workspace: Path, task_id: str) -> FileLock:
        return FileLock(self._workspace_dir(workspace) / "locks" / f"{_safe_id(task_id)}.lock")

    def _events_path(self, workspace: Path, task_id: str) -> Path:
        return self._workspace_dir(workspace) / "events" / f"{_safe_id(task_id)}.jsonl"

    def _session_path(self, workspace: Path, session_id: str) -> Path:
        return self._workspace_dir(workspace) / "sessions" / f"{_safe_id(session_id)}.json"

    def _session_lock_path(self, workspace: Path, session_id: str) -> Path:
        return self._workspace_dir(workspace) / "sessions" / f"{_safe_id(session_id)}.lock"

    def _write_record(self, path: Path, record: dict[str, Any], *, required: bool = False) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            if required:
                raise
            return

    def _write_text(self, path: Path, text: str, *, required: bool = False) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid4().hex}.tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        except OSError:
            if required:
                raise
            return

    @staticmethod
    def _read_record(path: Path) -> dict[str, Any] | None:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _read_many(self, directory: Path, pattern: str) -> list[dict[str, Any]]:
        if not directory.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in directory.glob(pattern):
            record = self._read_record(path)
            if record:
                records.append(record)
        return records


def summarize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}

    input_tokens = _first_number(
        usage,
        ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens"),
    )
    output_tokens = _first_number(
        usage,
        ("output_tokens", "outputTokens", "completion_tokens", "completionTokens"),
    )
    cache_read_tokens = _first_number(
        usage,
        ("cache_read_tokens", "cached_input_tokens", "cacheReadTokens"),
    )
    cache_write_tokens = _first_number(
        usage,
        ("cache_write_tokens", "cache_creation_input_tokens", "cacheWriteTokens"),
    )
    total_tokens = _first_number(
        usage,
        ("total_tokens", "totalTokens"),
    )
    cost = _first_number(usage, ("cost", "total_cost", "totalCost"))

    tokens = usage.get("tokens")
    if isinstance(tokens, dict):
        input_tokens = input_tokens or _first_number(
            tokens,
            ("input", "input_tokens", "inputTokens", "prompt"),
        )
        output_tokens = output_tokens or _first_number(
            tokens,
            ("output", "output_tokens", "outputTokens", "completion"),
        )
        total_tokens = total_tokens or _first_number(tokens, ("total", "total_tokens"))
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            cache_read_tokens = cache_read_tokens or _first_number(cache, ("read",))
            cache_write_tokens = cache_write_tokens or _first_number(cache, ("write",))

    cache_total = int(cache_read_tokens or 0) + int(cache_write_tokens or 0)
    raw_input = int(input_tokens or 0)
    raw_output = int(output_tokens or 0)

    if cache_total > 0:
        if input_tokens is not None:
            input_tokens = raw_input + cache_total
        else:
            input_tokens = cache_total
        # Antigravity reports total_tokens = input_tokens + output_tokens excluding cache_read_tokens.
        if total_tokens is not None and int(total_tokens) == raw_input + raw_output:
            total_tokens = int(total_tokens) + cache_total

    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)

    summary: dict[str, Any] = {}
    if input_tokens is not None:
        summary["input_tokens"] = int(input_tokens)
    if cache_read_tokens is not None and int(cache_read_tokens) > 0:
        summary["cache_read_tokens"] = int(cache_read_tokens)
    if output_tokens is not None:
        summary["output_tokens"] = int(output_tokens)
    if total_tokens is not None:
        summary["total_tokens"] = int(total_tokens)
    if cost is not None:
        summary["cost"] = float(cost)
    return summary


def _agent_event_summary(event: dict[str, Any]) -> str | None:
    kind = str(event.get("kind") or "")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if kind == "lifecycle":
        state = data.get("state")
        return f"state {state}" if state else "state update"
    if kind in {"text_delta", "message"}:
        text = data.get("text")
        if isinstance(text, str) and text.strip():
            return " ".join(text.strip().split())[:120]
    if kind == "tool":
        name = data.get("name") or data.get("item_type") or "tool"
        state = data.get("state") or data.get("status")
        return f"{name}{f' ({state})' if state else ''}"
    if kind == "child_update":
        role = data.get("role") or "subagent"
        state = data.get("state") or "running"
        return f"child {role}: {state}"
    if kind == "child_spawned":
        role = data.get("role") or "subagent"
        return f"spawned child {role}"
    if kind == "untracked_child":
        tool = data.get("tool") or "native subagent tool"
        return f"untracked native child activity via {tool}"
    if kind == "provider_session":
        provider_id = data.get("provider_session_id")
        return (
            f"provider session {provider_id}"
            if provider_id
            else "provider session established"
        )
    if kind == "diagnostic":
        detail = data.get("error") or data.get("text")
        return f"diagnostic: {detail}"[:120] if detail else "diagnostic"
    if kind == "provider_session":
        return "provider session established"
    return kind or None


def render_dashboard(
    snapshot: dict[str, Any],
    *,
    title: str = "CortexRelay status",
    completed_only: bool = False,
    verbosity: int = 0,
    live_only: bool = False,
) -> str:
    lines: list[str] = [title, "=" * len(title)]
    lines.append(f"Workspace: {snapshot.get('workspace', '')}")

    sessions = snapshot.get("sessions") or []
    active_session = next((item for item in sessions if item.get("status") == "running"), None)
    if active_session is None and sessions and not live_only:
        active_session = sessions[0]
    if active_session:
        host = _compact_model(
            active_session.get("provider"),
            active_session.get("model"),
            active_session.get("reasoning"),
        )
        lines.append(
            "Host: "
            f"{active_session.get('profile') or 'orchestrator'} → {host} "
            f"[{str(active_session.get('status', '')).upper()}]"
        )
    elif live_only:
        lines.append("Host: none active")

    summary = snapshot.get("summary") or {}
    totals = [
        f"Tasks: active {summary.get('active', 0)}",
        f"success {summary.get('success', 0)}",
        f"failed {summary.get('failed', 0)}",
    ]
    tokens = summary.get("total_tokens")
    if isinstance(tokens, int) and tokens:
        totals.append(f"tokens {_compact_number(tokens)}")
    cost = summary.get("cost")
    if isinstance(cost, (int, float)):
        totals.append("cost $" + f"{float(cost):.4f}")
    lines.append(" | ".join(totals))

    agents = snapshot.get("agents") or []
    agent_summary = snapshot.get("agent_summary") or {}
    if not completed_only:
        agent_totals = [
            f"Agents: running {agent_summary.get('running', 0)}",
            f"idle {agent_summary.get('idle', 0)}",
            f"failed {agent_summary.get('failed', 0)}",
        ]
        closed = int(agent_summary.get("closed") or 0)
        if closed:
            agent_totals.append(f"closed {closed}")
        lines.append(" | ".join(agent_totals))
    lines.append("")

    tasks = snapshot.get("tasks") or []
    if not tasks and not agents:
        if completed_only:
            lines.append("No completed tasks.")
        elif snapshot.get("active_only"):
            lines.append("No active CortexRelay requests. Watching for new requests.")
        else:
            lines.append("No CortexRelay tasks or agents recorded yet.")
        return "\n".join(lines)

    if not tasks:
        if completed_only:
            lines.append("No completed tasks.")
        elif snapshot.get("active_only"):
            lines.append("No active workflow tasks.")
        else:
            lines.append("No workflow tasks recorded.")
        if agents:
            lines.append("")

    now = time.time()
    for item in tasks:
        status = str(item.get("status") or "unknown")
        symbol = _status_symbol(status)
        elapsed = _elapsed(item, now)
        host_profile = item.get("host_profile")
        prefix = f"{host_profile} → " if host_profile else ""
        route = (
            f"{prefix}{item.get('role') or '?'} → "
            f"{item.get('profile') or item.get('provider') or '?'} → "
            f"{_compact_model(item.get('provider'), item.get('model'), item.get('reasoning'))}"
        )
        lines.append(f"{symbol} {route}")
        objective = str(item.get("objective") or "").strip()
        status_prefix = f"  {status.upper():<11} {elapsed}  "
        if objective:
            terminal_width = shutil.get_terminal_size((100, 24)).columns
            lines.extend(
                textwrap.wrap(
                    objective,
                    width=max(terminal_width, len(status_prefix) + 24),
                    initial_indent=status_prefix,
                    subsequent_indent=" " * len(status_prefix),
                    break_long_words=True,
                    break_on_hyphens=False,
                )
            )
        else:
            lines.append(status_prefix.rstrip())

        details: list[str] = []
        usage = item.get("usage_summary")
        if isinstance(usage, dict):
            total = usage.get("total_tokens")
            if isinstance(total, int) and total:
                details.append(f"{_compact_number(total)} tok")
            cost = usage.get("cost")
            if isinstance(cost, (int, float)):
                details.append("$" + f"{float(cost):.4f}")
        tests = item.get("tests")
        if isinstance(tests, list) and tests:
            details.append(f"{len(tests)} test result{'s' if len(tests) != 1 else ''}")
        files = item.get("changed_files")
        if isinstance(files, list) and files:
            details.append(f"{len(files)} file{'s' if len(files) != 1 else ''}")
        elif status in ACTIVE_STATUSES and item.get("progress_files"):
            count = len(item["progress_files"])
            details.append(f"{count} file{'s' if count != 1 else ''} observed")
        worktree = item.get("worktree_path")
        if worktree:
            details.append(f"worktree {worktree}")
        if details:
            lines.append("  " + " | ".join(details))
        if verbosity and item.get("depends_on"):
            lines.append("  Depends on: " + ", ".join(str(value) for value in item["depends_on"]))
        if item.get("blocked_by"):
            lines.append("  Blocked by: " + ", ".join(str(value) for value in item["blocked_by"]))
        if status == "queued":
            lines.append("  Current: waiting for dependencies")

        activity = item.get("current_activity")
        if status in ACTIVE_STATUSES and activity:
            lines.append(f"  Current: {_truncate(str(activity), 96)}")
            if item.get("current_command"):
                lines.append(f"  Command: {_truncate(str(item['current_command']), 120)}")
            elif item.get("current_path"):
                lines.append(f"  Path: {_truncate(str(item['current_path']), 120)}")
            input_tokens = item.get("progress_input_tokens")
            output_tokens = item.get("progress_output_tokens")
            if isinstance(input_tokens, int) or isinstance(output_tokens, int):
                lines.append(
                    f"  Tokens reported: {input_tokens or 0} in / {output_tokens or 0} out"
                )
            if verbosity:
                if item.get("current_exit_code") is not None:
                    lines.append(f"  Exit: {item['current_exit_code']}")
                if item.get("current_duration_seconds") is not None:
                    lines.append(f"  Duration: {float(item['current_duration_seconds']):.1f}s")
                if item.get("current_output_preview"):
                    lines.append(f"  Result: {_truncate(str(item['current_output_preview']), 180)}")
                if item.get("current_error_preview"):
                    lines.append(f"  Diagnostic: {_truncate(str(item['current_error_preview']), 180)}")
        if status in ACTIVE_STATUSES:
            last_event = item.get("last_event_at")
            if isinstance(last_event, str):
                sequence = int(item.get("progress_sequence") or 0)
                lines.append(
                    f"  Provider events: {sequence} | last {_elapsed({'started_at': last_event}, now)} ago"
                )
            else:
                lines.append("  Provider events: waiting")
            heartbeat = item.get("last_heartbeat_at")
            if isinstance(heartbeat, str):
                age = _elapsed({"started_at": heartbeat}, now)
                try:
                    fresh = now - datetime.fromisoformat(heartbeat).timestamp() < 15
                except ValueError:
                    fresh = False
                state = "alive" if item.get("process_alive") and fresh else "not recently observed"
                lines.append(f"  Process: {state} | heartbeat {age} ago")
            elif item.get("current_activity"):
                lines.append("  Process: heartbeat pending")

        if verbosity:
            recent = item.get("recent_events")
            if isinstance(recent, list) and recent:
                lines.append("  Recent activity:")
                for event in recent[-(15 if verbosity >= 2 else 5):]:
                    if not isinstance(event, dict):
                        continue
                    at = str(event.get("at") or "")
                    try:
                        clock = datetime.fromisoformat(at).astimezone().strftime("%H:%M:%S")
                    except ValueError:
                        clock = "--:--:--"
                    action = str(event.get("activity") or event.get("phase") or "activity")
                    target = event.get("command") or event.get("path")
                    detail = f"  {clock}  {action}"
                    if target:
                        detail += f"  {_truncate(str(target), 110)}"
                    if event.get("exit_code") is not None:
                        detail += f"  exit={event['exit_code']}"
                    if event.get("duration_seconds") is not None:
                        detail += f"  {float(event['duration_seconds']):.1f}s"
                    lines.append(detail)
                    if verbosity >= 2:
                        for key in ("output_preview", "error_preview"):
                            if event.get(key):
                                lines.append(f"    {key}: {_truncate(str(event[key]), 180)}")
                        for agent in event.get("subagents") or []:
                            if isinstance(agent, dict):
                                lines.append(
                                    f"    subagent: {agent.get('role') or agent.get('type') or agent.get('conversation_id') or '?'} "
                                    f"{agent.get('state') or ''}"
                                )
                if verbosity >= 2 and item.get("current_plan"):
                    lines.append("  Plan:")
                    for step in item["current_plan"]:
                        if isinstance(step, dict):
                            lines.append(f"    {'✓' if step.get('completed') else '○'} {step.get('text') or ''}")

        error = item.get("error")
        if error:
            lines.append(f"  error: {_truncate(str(error), 120)}")
        elif status == "success" and item.get("summary"):
            lines.append(f"  result: {_truncate(str(item.get('summary')), 120)}")
        lines.append("")

    if agents:
        lines.append("Agents")
        lines.append("------")
        for agent in agents:
            state = str(agent.get("state") or "unknown")
            symbol = {
                "starting": "◌",
                "running": "●",
                "idle": "✓",
                "failed": "!",
                "interrupted": "!",
                "closed": "○",
            }.get(state, "?")
            child_prefix = "↳ " if agent.get("parent_session_id") else ""
            route = (
                f"{child_prefix}{agent.get('role') or 'worker'} → "
                f"{_compact_model(agent.get('provider'), agent.get('model'), agent.get('reasoning'))}"
            )
            lines.append(f"{symbol} {route}")
            elapsed = _elapsed(
                {"started_at": agent.get("created_at") or agent.get("updated_at")},
                now,
            )
            objective = str(agent.get("objective") or "").strip()
            state_prefix = f"  {state.upper():<11} {elapsed}  "
            if objective:
                terminal_width = shutil.get_terminal_size((100, 24)).columns
                lines.extend(
                    textwrap.wrap(
                        objective,
                        width=max(terminal_width, len(state_prefix) + 24),
                        initial_indent=state_prefix,
                        subsequent_indent=" " * len(state_prefix),
                        break_long_words=True,
                        break_on_hyphens=False,
                    )
                )
            else:
                lines.append(state_prefix.rstrip())

            details: list[str] = [
                f"session {str(agent.get('session_id') or '')[:18]}",
                f"access {agent.get('access') or 'read_only'}",
            ]
            if agent.get("provider_session_id"):
                details.append(
                    "provider-session "
                    + _truncate(str(agent["provider_session_id"]), 36)
                )
            if agent.get("task_id"):
                details.append(f"task {agent['task_id']}")
            if agent.get("parent_session_id"):
                details.append(
                    "parent " + str(agent["parent_session_id"])[:18]
                )
            metadata = agent.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            provider_pid = metadata.get("provider_pid")
            if provider_pid is not None:
                process_state = (
                    "alive"
                    if metadata.get("provider_process_alive")
                    else "not alive"
                )
                details.append(f"pid {provider_pid} {process_state}")
            lines.append("  " + " | ".join(details))

            if metadata.get("untracked_native_work"):
                count = int(metadata.get("untracked_native_work_count") or 1)
                lines.append(
                    f"  Warning: {count} native child event"
                    f"{'s' if count != 1 else ''} could not be correlated to a child session"
                )

            activity = agent.get("current_activity")
            if activity:
                lines.append(f"  Current: {_truncate(str(activity), 120)}")
            elif state in {"starting", "running"}:
                lines.append("  Current: waiting for provider activity")

            provider_heartbeat = metadata.get("provider_heartbeat_at")
            if isinstance(provider_heartbeat, str):
                lines.append(
                    "  Provider heartbeat: "
                    + _elapsed({"started_at": provider_heartbeat}, now)
                    + " ago"
                )

            last_event = agent.get("last_event")
            if isinstance(last_event, dict):
                sequence = int(last_event.get("sequence") or 0)
                at = last_event.get("at")
                if isinstance(at, str):
                    lines.append(
                        f"  Agent events: {sequence} | last "
                        f"{_elapsed({'started_at': at}, now)} ago"
                    )
                else:
                    lines.append(f"  Agent events: {sequence}")

            if verbosity:
                recent = agent.get("recent_events")
                if isinstance(recent, list) and recent:
                    lines.append("  Recent agent activity:")
                    for event in recent[-(15 if verbosity >= 2 else 5):]:
                        if not isinstance(event, dict):
                            continue
                        at = str(event.get("at") or "")
                        try:
                            clock = datetime.fromisoformat(at).astimezone().strftime("%H:%M:%S")
                        except ValueError:
                            clock = "--:--:--"
                        summary_text = _agent_event_summary(event) or str(event.get("kind") or "activity")
                        lines.append(
                            f"    {clock}  {_truncate(summary_text, 150)}"
                        )
            lines.append("")
    elif not completed_only:
        if snapshot.get("active_only"):
            lines.append("No active direct agents.")
        else:
            lines.append("No direct agents recorded.")

    return "\n".join(lines).rstrip()


def _default_state_root() -> Path:
    override = os.environ.get("CORTEX_RELAY_STATE_DIR")
    if override:
        return Path(override)

    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "CortexRelay" / "state"

    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "cortex-relay"
    return Path.home() / ".local" / "state" / "cortex-relay"


def _workspace(value: Path) -> Path:
    return Path(value).expanduser().resolve()


def _safe_id(value: str) -> str:
    return "".join(char for char in value if char.isalnum() or char in "-_.")[:96] or "unknown"


def _validated_task_id(value: str) -> str:
    if not isinstance(value, str) or not _TASK_ID.fullmatch(value):
        raise ValueError("task_id must be an alphanumeric handle of at most 96 characters")
    return value


def _checked_lookup_id(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise ValueError("invalid task id")
    return value


def _valid_stored_result(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("status") in TERMINAL_STATUSES
        and isinstance(value.get("provider"), str)
        and isinstance(value.get("summary"), str)
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _first_number(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _session_from_environment() -> dict[str, str | None]:
    return {
        "session_id": os.environ.get("CORTEX_RELAY_SESSION_ID"),
        "host_profile": os.environ.get("CORTEX_RELAY_HOST_PROFILE"),
        "host_model": os.environ.get("CORTEX_RELAY_HOST_MODEL"),
        "host_reasoning": os.environ.get("CORTEX_RELAY_HOST_REASONING"),
    }


def _status_symbol(status: str) -> str:
    return {
        "routing": "◌",
        "preparing": "◌",
        "running": "●",
        "fallback": "↻",
        "success": "✓",
        "error": "✗",
        "timeout": "⌛",
        "unavailable": "!",
        "cancelled": "×",
        "queued": "○",
        "blocked": "⊘",
        "interrupted": "!",
        "failed_gate": "⊗",
        "budget_exceeded": "$",
    }.get(status, "?")


def _compact_model(provider: Any, model: Any, reasoning: Any) -> str:
    provider_text = str(provider or "?")
    model_text = str(model or "(default)")
    reasoning_text = str(reasoning or "")
    if "/" in model_text:
        model_text = model_text.rsplit("/", 1)[-1]
    suffix = f"/{reasoning_text}" if reasoning_text else ""
    return f"{provider_text}/{model_text}{suffix}"


def _elapsed(item: dict[str, Any], now_epoch: float) -> str:
    duration = item.get("duration_seconds")
    if isinstance(duration, (int, float)):
        return _format_duration(float(duration))

    started = item.get("started_at")
    if isinstance(started, str):
        try:
            start = datetime.fromisoformat(started).timestamp()
            end = now_epoch
            if item.get("status") in TERMINAL_STATUSES:
                completed = item.get("completed_at") or item.get("updated_at")
                if not isinstance(completed, str):
                    return "--"
                end = datetime.fromisoformat(completed).timestamp()
            return _format_duration(max(0.0, end - start))
        except ValueError:
            pass
    return "--"


def _format_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minute = divmod(minutes, 60)
    return f"{hours}h{minute:02d}m"


def _compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)
