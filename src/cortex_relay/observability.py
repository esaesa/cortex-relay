from __future__ import annotations

import hashlib
import json
import os
import time

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex_relay.core.models import TaskResult, TaskSpec

ACTIVE_STATUSES = {"routing", "preparing", "running", "fallback"}
TERMINAL_STATUSES = {"success", "error", "timeout", "unavailable", "cancelled"}


class RunStore:
    """Cross-process, per-workspace runtime observability state."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or _default_state_root()).expanduser().resolve()

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
        self._write_record(self._session_path(workspace, session_id), record)
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
                "status": "success" if exit_code == 0 else "error",
                "updated_at": now,
                "completed_at": now,
                "exit_code": exit_code,
            }
        )
        self._write_record(path, record)

    def start_task(
        self,
        task: TaskSpec,
        *,
        task_id: str | None = None,
    ) -> str:
        task_id = task_id or f"{task.role}-{uuid4().hex[:10]}"
        now = _utc_now()
        session = _session_from_environment()
        record = {
            "schema_version": 1,
            "kind": "task",
            "task_id": task_id,
            "session_id": session.get("session_id"),
            "host_profile": session.get("host_profile"),
            "host_model": session.get("host_model"),
            "host_reasoning": session.get("host_reasoning"),
            "workspace": str(_workspace(task.workspace)),
            "role": task.role,
            "objective": _truncate(task.objective, 280),
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
            "owner_pid": os.getpid(),
        }
        self._write_record(self._task_path(task.workspace, task_id), record)
        return task_id

    def update_task(
        self,
        workspace: Path,
        task_id: str,
        **updates: Any,
    ) -> None:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path) or {}
        record.update(updates)
        record["updated_at"] = _utc_now()
        self._write_record(path, record)

    def complete_task(
        self,
        workspace: Path,
        task_id: str,
        result: TaskResult,
    ) -> dict[str, Any]:
        path = self._task_path(workspace, task_id)
        record = self._read_record(path) or {}
        now = _utc_now()
        usage_summary = summarize_usage(result.usage)
        metadata = result.metadata
        record.update(
            {
                "status": result.status,
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
                "worktree_path": metadata.get("worktree_path"),
                "worktree_branch": metadata.get("worktree_branch"),
                "attempts": metadata.get("routing_attempts", record.get("attempts", [])),
                "billing_class": metadata.get("billing_class", record.get("billing_class")),
                "profile": metadata.get("profile", record.get("profile")),
            }
        )
        self._write_record(path, record)
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
            records = [item for item in records if item.get("status") in ACTIVE_STATUSES]
        if completed_only:
            records = [item for item in records if item.get("status") in TERMINAL_STATUSES]
        records.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        return records[: max(1, limit)]

    def list_sessions(
        self,
        workspace: Path,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        directory = self._workspace_dir(workspace) / "sessions"
        records = self._read_many(directory, "*.json")
        records.sort(key=lambda item: str(item.get("started_at", "")), reverse=True)
        return records[: max(1, limit)]

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
        active = sum(item.get("status") in ACTIVE_STATUSES for item in tasks)
        success = sum(item.get("status") == "success" for item in tasks)
        failed = sum(
            item.get("status") in {"error", "timeout", "unavailable", "cancelled"}
            for item in tasks
        )
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
            "summary": {
                "active": active,
                "success": success,
                "failed": failed,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "total_tokens": total_tokens,
                "cost": total_cost if cost_known else None,
            },
        }

    def clear_completed(self, workspace: Path) -> int:
        directory = self._workspace_dir(workspace) / "tasks"
        removed = 0
        if not directory.exists():
            return 0
        for path in directory.glob("*.json"):
            record = self._read_record(path)
            if record and record.get("status") in TERMINAL_STATUSES:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def _workspace_dir(self, workspace: Path) -> Path:
        resolved = str(_workspace(workspace))
        key = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
        return self.root / key

    def _task_path(self, workspace: Path, task_id: str) -> Path:
        return self._workspace_dir(workspace) / "tasks" / f"{_safe_id(task_id)}.json"

    def _session_path(self, workspace: Path, session_id: str) -> Path:
        return self._workspace_dir(workspace) / "sessions" / f"{_safe_id(session_id)}.json"

    def _write_record(self, path: Path, record: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
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

    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)

    summary: dict[str, Any] = {}
    if input_tokens is not None:
        summary["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        summary["output_tokens"] = int(output_tokens)
    if total_tokens is not None:
        summary["total_tokens"] = int(total_tokens)
    if cost is not None:
        summary["cost"] = float(cost)
    return summary


def render_dashboard(
    snapshot: dict[str, Any],
    *,
    title: str = "CortexRelay status",
    completed_only: bool = False,
) -> str:
    lines: list[str] = [title, "=" * len(title)]
    lines.append(f"Workspace: {snapshot.get('workspace', '')}")

    sessions = snapshot.get("sessions") or []
    active_session = next(
        (item for item in sessions if item.get("status") == "running"),
        sessions[0] if sessions else None,
    )
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

    summary = snapshot.get("summary") or {}
    totals = [
        f"active {summary.get('active', 0)}",
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
    lines.append("")

    tasks = snapshot.get("tasks") or []
    if not tasks:
        lines.append("No completed tasks." if completed_only else "No CortexRelay tasks recorded yet.")
        return "\n".join(lines)

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
        lines.append(f"  {status.upper():<11} {elapsed}  {_truncate(str(item.get('objective') or ''), 96)}")

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
        worktree = item.get("worktree_path")
        if worktree:
            details.append(f"worktree {worktree}")
        if details:
            lines.append("  " + " | ".join(details))

        error = item.get("error")
        if error:
            lines.append(f"  error: {_truncate(str(error), 120)}")
        elif status == "success" and item.get("summary"):
            lines.append(f"  result: {_truncate(str(item.get('summary')), 120)}")
        lines.append("")

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
            return _format_duration(max(0.0, now_epoch - start))
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
