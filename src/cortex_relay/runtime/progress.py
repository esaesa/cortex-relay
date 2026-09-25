"""Bounded dashboard progress projected from canonical durable agent events."""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any, Callable

from cortex_relay.core.agents import AgentEvent
from cortex_relay.runtime.agent_events import normalize_agent_events


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|authorization|password|secret)\b(\s*[:=]\s*)(\S+)")
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_LONG_TOKEN = re.compile(r"(?<![\w])[A-Za-z0-9_+/=-]{48,}(?![\w])")


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
    sequence: int | None = None
    provider_event: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    output_preview: str | None = None
    error_preview: str | None = None
    files: tuple[str, ...] = ()
    subagents: tuple[dict[str, Any], ...] = ()
    plan: tuple[dict[str, Any], ...] = ()
    total_tokens: int | None = None
    cache_tokens: int | None = None
    step_index: int | None = None


def runner_progress_kwargs(task: Any, provider: str) -> dict[str, Any]:
    callback = task.metadata.get("_progress_line")
    heartbeat = task.metadata.get("_progress_heartbeat")
    if not callable(callback):
        return {}
    callbacks: dict[str, Callable[..., None]] = {
        "on_stdout_line": lambda line: callback(provider, line, "stdout"),
        "on_stderr_line": lambda line: callback(provider, line, "stderr"),
    }
    if callable(heartbeat):
        callbacks["on_heartbeat"] = heartbeat
    idle_timeout = getattr(getattr(task, "budget", None), "max_idle_seconds", None)
    if idle_timeout is not None:
        callbacks["idle_timeout_seconds"] = float(idle_timeout)
    return callbacks


def normalize_progress(
    provider: str,
    line: str,
    task_id: str,
    stream: str = "stdout",
) -> ProgressEvent | None:
    """Project one provider line through the canonical AgentEvent parser.

    Raw OpenCode/Codex/Antigravity JSON shapes are interpreted only by
    normalize_agent_events(). This module intentionally understands only the
    provider-neutral semantic events emitted by that parser.
    """
    events = normalize_agent_events(provider, line, task_id, stream)
    if not events:
        return None

    diagnostic = _diagnostic(task_id, events)
    if diagnostic is not None:
        return diagnostic

    if provider == "antigravity":
        return _antigravity(events, task_id)
    if provider == "codex":
        return _codex(events, task_id)
    if provider == "opencode":
        return _opencode(events, task_id)
    return None


def _antigravity(
    events: tuple[AgentEvent, ...],
    task_id: str,
) -> ProgressEvent | None:
    lifecycle = _first(events, "lifecycle")
    if lifecycle is not None:
        data = lifecycle.data
        if lifecycle.provider_event == "init":
            return ProgressEvent(
                task_id,
                "startup",
                "active",
                "Provider started",
                provider_event="init",
            )
        if lifecycle.provider_event == "result":
            failed = str(data.get("state") or "").lower() == "failed"
            return ProgressEvent(
                task_id,
                "result",
                "error" if failed else "done",
                "Provider failed" if failed else "Provider finished",
                provider_event="result",
                duration_seconds=_number(data.get("duration_seconds")),
                error_preview=_preview(_message(data.get("error"))),
                **_usage(data.get("usage")),
            )

    response = _first(events, "text_delta")
    if response is not None:
        return ProgressEvent(
            task_id,
            "response",
            "active",
            "Agent response",
            provider_event=response.provider_event,
            step_index=_integer(response.data.get("step_index")),
            output_preview=_preview(response.data.get("text")),
        )

    children = _of_kind(events, "child_update")
    tool_event = _first(events, "tool")
    if tool_event is not None:
        data = tool_event.data
        params = _dict(data.get("parameters"))
        tool = _preview(data.get("name"), 80)
        error = _message(data.get("error"))
        state = _semantic_state(data.get("state"), error=error)
        path = _preview(
            _param(
                params,
                "path",
                "file_path",
                "AbsolutePath",
                "FilePath",
                "TargetFile",
            ),
            180,
        )
        return ProgressEvent(
            task_id,
            "tool",
            state,
            f"{tool or 'Tool'} {state}",
            tool=tool,
            command=_preview(_param(params, "CommandLine", "command", "cmd"), 180),
            path=path,
            provider_event=tool_event.provider_event,
            step_index=_integer(data.get("step_index")),
            duration_seconds=_number(data.get("duration_seconds")),
            output_preview=_preview(_message(data.get("output"))),
            error_preview=_preview(error),
            files=(path,) if path and _changes_file(tool) and state == "done" else (),
            subagents=_child_rows(children),
            **_usage(data.get("usage")),
        )

    if children:
        first = children[0].data
        state = _children_state(children)
        return ProgressEvent(
            task_id,
            "subagent",
            state,
            f"{len(children)} subagent(s) {state}",
            provider_event=children[0].provider_event,
            step_index=_integer(first.get("step_index")),
            duration_seconds=_number(first.get("duration_seconds")),
            subagents=_child_rows(children),
            **_usage(first.get("usage")),
        )
    return None


def _codex(
    events: tuple[AgentEvent, ...],
    task_id: str,
) -> ProgressEvent | None:
    lifecycle = _first(events, "lifecycle")
    if lifecycle is not None:
        state_name = str(lifecycle.data.get("state") or "")
        state = (
            "active"
            if state_name == "running"
            else ("error" if state_name == "failed" else "done")
        )
        provider_event = lifecycle.provider_event or "turn"
        return ProgressEvent(
            task_id,
            "turn",
            state,
            provider_event.replace("/", " ").replace(".", " "),
            provider_event=provider_event,
            **_usage(lifecycle.data.get("usage")),
        )

    delta = _first(events, "text_delta")
    if delta is not None:
        return ProgressEvent(
            task_id,
            "response",
            "active",
            "Agent response",
            provider_event=delta.provider_event,
            output_preview=_preview(delta.data.get("text")),
        )

    message = _first(events, "message")
    if message is not None:
        done = str(message.provider_event or "").endswith("completed")
        return ProgressEvent(
            task_id,
            "response",
            "done" if done else "active",
            "Agent message",
            provider_event=message.provider_event,
            output_preview=_preview(message.data.get("text")),
        )

    children = _of_kind(events, "child_update")
    tool_event = _first(events, "tool")
    if tool_event is not None:
        return _codex_item_progress(
            task_id,
            tool_event,
            subagents=_child_rows(children),
        )

    if children:
        data = children[0].data
        item = data.get("item")
        if isinstance(item, dict):
            synthetic = AgentEvent(
                session_id=task_id,
                kind="tool",
                data={
                    "item_type": data.get("item_type") or item.get("type"),
                    "state": (
                        "done"
                        if str(children[0].provider_event or "").endswith("completed")
                        else "running"
                    ),
                    "item": item,
                },
                provider_event=children[0].provider_event,
            )
            return _codex_item_progress(
                task_id,
                synthetic,
                subagents=_child_rows(children),
            )
        state = _children_state(children)
        return ProgressEvent(
            task_id,
            "subagent",
            state,
            f"{len(children)} subagent(s) {state}",
            provider_event=children[0].provider_event,
            subagents=_child_rows(children),
        )
    return None


def _codex_item_progress(
    task_id: str,
    event: AgentEvent,
    *,
    subagents: tuple[dict[str, Any], ...] = (),
) -> ProgressEvent | None:
    data = event.data
    item = _dict(data.get("item"))
    item_type = str(data.get("item_type") or item.get("type") or "")

    if item_type == "todo_list":
        entries = item.get("items")
        plan = tuple(
            {
                "text": _preview(entry.get("text"), 120),
                "completed": bool(entry.get("completed")),
            }
            for entry in (entries if isinstance(entries, list) else [])[:12]
            if isinstance(entry, dict)
        )
        done = sum(bool(entry["completed"]) for entry in plan)
        return ProgressEvent(
            task_id,
            "plan",
            "active",
            f"Plan updated ({done}/{len(plan)} done)",
            provider_event=event.provider_event,
            plan=plan,
        )

    if item_type == "error":
        return ProgressEvent(
            task_id,
            "diagnostic",
            "error",
            "Codex error",
            provider_event=event.provider_event,
            error_preview=_preview(_message(item.get("error") or item.get("message"))),
        )

    raw_state = str(data.get("state") or "")
    state = "done" if raw_state == "done" else "active"
    exit_code = _integer(item.get("exit_code"))
    if item.get("status") in {"failed", "error"} or (
        exit_code is not None and exit_code != 0
    ):
        state = "error"

    changes = item.get("changes")
    files = tuple(
        path
        for change in (changes if isinstance(changes, list) else [])[:12]
        if isinstance(change, dict)
        for path in [_preview(change.get("path"), 180)]
        if path
    )
    tool = item_type or None
    return ProgressEvent(
        task_id,
        "tool",
        state,
        f"{(tool or 'item').replace('_', ' ')} {state}",
        tool=tool,
        command=_preview(item.get("command"), 180),
        path=files[0] if files else None,
        files=files,
        exit_code=exit_code,
        provider_event=event.provider_event,
        output_preview=_preview(item.get("aggregated_output")),
        error_preview=_preview(_message(item.get("error"))),
        subagents=subagents,
    )


def _opencode(
    events: tuple[AgentEvent, ...],
    task_id: str,
) -> ProgressEvent | None:
    lifecycle = _first(events, "lifecycle")
    if lifecycle is not None:
        active = str(lifecycle.data.get("state") or "") == "running"
        return ProgressEvent(
            task_id,
            "turn",
            "active" if active else "done",
            "Step started" if active else "Step finished",
            provider_event=lifecycle.provider_event,
            **_usage(lifecycle.data.get("usage")),
        )

    message = _first(events, "message")
    if message is not None:
        return ProgressEvent(
            task_id,
            "response",
            "active",
            "Agent response",
            provider_event=message.provider_event,
            output_preview=_preview(message.data.get("text")),
        )

    tool_event = _first(events, "tool")
    if tool_event is not None:
        data = tool_event.data
        tool = _preview(data.get("name"), 80)
        status = data.get("status")
        state = (
            "error"
            if status == "error"
            else ("done" if status == "completed" else "active")
        )
        params = _dict(data.get("input"))
        times = _dict(data.get("time"))
        start = _number(times.get("start"))
        end = _number(times.get("end"))
        duration = (
            (end - start) / 1000
            if start is not None and end is not None
            else None
        )
        path = _preview(_param(params, "filePath", "path", "file_path"), 180)
        return ProgressEvent(
            task_id,
            "tool",
            state,
            f"{tool or 'Tool'} {state}",
            tool=tool,
            command=_preview(params.get("command"), 180),
            path=path,
            provider_event=tool_event.provider_event,
            duration_seconds=duration,
            output_preview=_preview(_message(data.get("output"))),
            error_preview=_preview(_message(data.get("error"))),
            files=(path,) if path and _changes_file(tool) and state == "done" else (),
            subagents=_child_rows(_of_kind(events, "child_update")),
        )
    return None


def _diagnostic(
    task_id: str,
    events: tuple[AgentEvent, ...],
) -> ProgressEvent | None:
    event = _first(events, "diagnostic")
    if event is None:
        return None
    detail = event.data.get("error")
    if detail is None:
        detail = event.data.get("text")
    preview = _preview(_message(detail))
    if not preview:
        return None
    error = event.provider_event != "stderr" or any(
        word in preview.lower() for word in ("error", "failed", "denied")
    )
    return ProgressEvent(
        task_id,
        "diagnostic",
        "error" if error else "warning",
        "Provider diagnostic",
        provider_event=event.provider_event,
        error_preview=preview,
    )


def _first(
    events: tuple[AgentEvent, ...],
    kind: str,
) -> AgentEvent | None:
    return next((event for event in events if event.kind == kind), None)


def _of_kind(
    events: tuple[AgentEvent, ...],
    kind: str,
) -> tuple[AgentEvent, ...]:
    return tuple(event for event in events if event.kind == kind)


def _child_rows(events: tuple[AgentEvent, ...]) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for event in events[:8]:
        data = event.data
        provider_id = _preview(data.get("provider_session_id"), 80)
        if not provider_id:
            continue
        rows.append(
            {
                "conversation_id": provider_id,
                "role": _preview(data.get("role"), 80),
                "type": _preview(data.get("type"), 80),
                "state": _preview(data.get("state"), 40),
                "log_uri": _preview(data.get("log_uri"), 180),
                "workspace_uris": data.get("workspace_uris"),
            }
        )
    return tuple(rows)


def _children_state(events: tuple[AgentEvent, ...]) -> str:
    states = {
        str(event.data.get("state") or "").strip().lower()
        for event in events
    }
    if any(state in {"failed", "error", "interrupted"} for state in states):
        return "error"
    if states and all(
        state in {"done", "idle", "completed", "success"}
        for state in states
    ):
        return "done"
    return "active"


def _semantic_state(value: Any, *, error: Any = None) -> str:
    if error:
        return "error"
    state = str(value or "").strip().lower()
    if state in {"done", "idle", "completed", "success"}:
        return "done"
    if state in {"failed", "error", "interrupted"}:
        return "error"
    return "active"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("message", "text", "output", "detail"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        for key in ("error", "data"):
            nested = _message(value.get(key))
            if nested:
                return nested
        return None
    return value


def _changes_file(tool: str | None) -> bool:
    return bool(
        tool
        and any(
            word in tool.lower()
            for word in ("write", "edit", "patch", "replace")
        )
    )


def _param(params: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in params.items()}
    return next(
        (
            lowered[name.lower()]
            for name in names
            if name.lower() in lowered
        ),
        None,
    )


def _preview(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(_ANSI.sub("", value).split())
    text = _SECRET.sub(r"\1\2[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _LONG_TOKEN.sub("[redacted]", text)
    return text[:limit] if text else None


def _integer(value: Any) -> int | None:
    return (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _usage(value: Any) -> dict[str, int | None]:
    data = _dict(value)
    cache = _dict(data.get("cache"))
    return {
        "input_tokens": _integer(
            data.get("input_tokens")
            if data.get("input_tokens") is not None
            else data.get("input")
        ),
        "output_tokens": _integer(
            data.get("output_tokens")
            if data.get("output_tokens") is not None
            else data.get("output")
        ),
        "total_tokens": _integer(
            data.get("total_tokens")
            if data.get("total_tokens") is not None
            else data.get("total")
        ),
        "cache_tokens": _integer(
            data.get("cache_read_tokens")
            if data.get("cache_read_tokens") is not None
            else (
                data.get("cached_input_tokens")
                if data.get("cached_input_tokens") is not None
                else cache.get("read")
            )
        ),
    }
