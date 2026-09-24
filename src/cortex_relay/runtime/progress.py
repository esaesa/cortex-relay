"""Normalize observable provider work without retaining model reasoning."""

from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ProgressEvent:
    task_id: str
    phase: str
    state: str
    activity: str
    tool: str | None = None
    command: str | None = None
    path: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def runner_progress_kwargs(task: Any, provider: str) -> dict[str, Callable[[str], None]]:
    """Enable callbacks only when a registry supplied an observer."""

    callback = task.metadata.get("_progress_line")
    if not callable(callback):
        return {}
    return {"on_stdout_line": lambda line: callback(provider, line)}


def normalize_progress(provider: str, line: str, task_id: str) -> ProgressEvent | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    if provider == "antigravity":
        return _antigravity(event, task_id)
    if provider == "codex":
        return _codex(event, task_id)
    if provider == "opencode":
        return _opencode(event, task_id)
    return None


def _antigravity(event: dict[str, Any], task_id: str) -> ProgressEvent | None:
    kind = event.get("event")
    if kind == "init":
        return ProgressEvent(task_id, "startup", "active", "Provider started")
    if kind == "result":
        result = event.get("result")
        if not isinstance(result, dict):
            return None
        usage = result.get("usage")
        return ProgressEvent(
            task_id, "result", "done", "Provider finished",
            input_tokens=_token(usage, "input_tokens"),
            output_tokens=_token(usage, "output_tokens"),
        )
    if kind != "step_update":
        return None
    step = event.get("step_update")
    if not isinstance(step, dict):
        return None
    step_type = step.get("step_type")
    if step_type == "agent_response":
        # Never retain text_delta; it may contain private model content.
        return ProgressEvent(task_id, "response", "active", "Preparing response")
    if step_type != "tool":
        return None
    tool = _short(step.get("tool_name"))
    info = step.get("tool_info")
    params = info.get("parameters") if isinstance(info, dict) else None
    params = params if isinstance(params, dict) else {}
    command = _short(params.get("CommandLine") or params.get("command"))
    path = _short(params.get("path") or params.get("file_path"))
    state = "done" if step.get("state") == "DONE" else "active"
    usage = step.get("usage")
    return ProgressEvent(
        task_id, "tool", state, f"{tool or 'Tool'} {state}",
        tool=tool, command=command, path=path,
        input_tokens=_token(usage, "input_tokens"),
        output_tokens=_token(usage, "output_tokens"),
    )


def _codex(event: dict[str, Any], task_id: str) -> ProgressEvent | None:
    kind = event.get("type")
    if kind in {"turn.started", "turn.completed", "turn.failed"}:
        state = "active" if kind == "turn.started" else "done"
        return ProgressEvent(task_id, "turn", state, kind.replace(".", " "))
    if kind not in {"item.started", "item.completed"}:
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type not in {"command_execution", "file_change", "mcp_tool_call", "web_search"}:
        return None
    state = "active" if kind == "item.started" else "done"
    changes = item.get("changes")
    first_change = changes[0] if isinstance(changes, list) and changes else None
    path = _short(first_change.get("path")) if isinstance(first_change, dict) else None
    return ProgressEvent(
        task_id, "tool", state, f"{str(item_type).replace('_', ' ')} {state}",
        tool=str(item_type), command=_short(item.get("command")), path=path,
    )


def _opencode(event: dict[str, Any], task_id: str) -> ProgressEvent | None:
    kind = event.get("type")
    part = event.get("part")
    if kind == "step_start":
        return ProgressEvent(task_id, "turn", "active", "Step started")
    if kind == "step_finish":
        return ProgressEvent(task_id, "turn", "done", "Step finished")
    if not isinstance(part, dict) or part.get("type") != "tool":
        return None
    tool = _short(part.get("tool"))
    state_data = part.get("state")
    state_data = state_data if isinstance(state_data, dict) else {}
    state = "done" if state_data.get("status") in {"completed", "error"} else "active"
    params = state_data.get("input")
    params = params if isinstance(params, dict) else {}
    return ProgressEvent(
        task_id, "tool", state, f"{tool or 'Tool'} {state}",
        tool=tool,
        command=_short(params.get("command")),
        path=_short(params.get("filePath") or params.get("path")),
    )


def _short(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:160] if text else None


def _token(usage: Any, key: str) -> int | None:
    if not isinstance(usage, dict):
        return None
    value = usage.get(key)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
