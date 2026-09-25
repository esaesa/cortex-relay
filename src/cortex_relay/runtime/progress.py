"""Bounded dashboard progress projected from durable semantic agent events."""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any, Callable

from cortex_relay.core.agents import AgentEvent
from cortex_relay.runtime.agent_events import normalize_agent_events


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|authorization|password|secret)"
    r"\b(\s*[:=]\s*)(\S+)"
)
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


def runner_progress_kwargs(task: Any, provider: str) -> dict[str, Callable[..., None]]:
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
    return callbacks


def normalize_progress_events(
    provider: str,
    line: str,
    task_id: str,
    stream: str = "stdout",
) -> tuple[ProgressEvent, ...]:
    """Parse provider output once, then project semantic events into task progress."""

    semantic = normalize_agent_events(
        provider,
        line,
        f"task-progress:{task_id}",
        stream,
    )
    projected: list[ProgressEvent] = []
    for event in semantic:
        progress = project_agent_event(event, task_id)
        if progress is not None:
            projected.append(progress)
    return tuple(projected)


def normalize_progress(
    provider: str,
    line: str,
    task_id: str,
    stream: str = "stdout",
) -> ProgressEvent | None:
    """Compatibility wrapper returning the most informative projected event."""

    events = normalize_progress_events(provider, line, task_id, stream)
    if not events:
        return None
    priority = {
        "diagnostic": 8,
        "tool": 7,
        "plan": 6,
        "response": 5,
        "subagent": 4,
        "result": 3,
        "turn": 2,
        "startup": 1,
    }
    return max(events, key=lambda item: priority.get(item.phase, 0))


def project_agent_event(event: AgentEvent, task_id: str) -> ProgressEvent | None:
    """Project one durable semantic event into bounded dashboard/task progress."""

    kind = event.kind
    data = event.data if isinstance(event.data, dict) else {}
    provider_event = event.provider_event

    if kind == "diagnostic":
        detail = _message(data.get("error")) or _message(data.get("text"))
        preview = _preview(detail)
        lowered = (preview or "").lower()
        state = (
            "error"
            if data.get("error") is not None
            or any(word in lowered for word in ("error", "failed", "denied"))
            else "warning"
        )
        return ProgressEvent(
            task_id,
            "diagnostic",
            state,
            "Provider diagnostic",
            provider_event=provider_event,
            error_preview=preview,
        )

    if kind == "provider_session":
        provider_id = _preview(data.get("provider_session_id"), 120)
        activity = "Provider session established"
        if provider_id:
            activity += f" ({provider_id})"
        return ProgressEvent(
            task_id,
            "startup",
            "active",
            activity,
            provider_event=provider_event,
        )

    if kind == "lifecycle":
        semantic_state = str(data.get("state") or "").strip().lower()
        state = _lifecycle_state(semantic_state)
        phase = _lifecycle_phase(provider_event)
        if provider_event == "init":
            activity = "Provider started"
        elif provider_event == "result":
            activity = "Provider failed" if state == "error" else "Provider finished"
        else:
            activity = _event_activity(provider_event, state)
        return ProgressEvent(
            task_id,
            phase,
            state,
            activity,
            provider_event=provider_event,
            duration_seconds=_number(data.get("duration_seconds")),
            error_preview=_preview(_message(data.get("error"))),
            **_usage(data.get("usage")),
        )

    if kind in {"text_delta", "message"}:
        text = _preview(data.get("text"))
        if not text:
            return None
        done = kind == "message" and _provider_event_is_completed(provider_event)
        return ProgressEvent(
            task_id,
            "response",
            "done" if done else "active",
            "Agent message" if kind == "message" else "Agent response",
            provider_event=provider_event,
            output_preview=text,
            duration_seconds=_number(data.get("duration_seconds")),
            step_index=_integer(data.get("step_index")),
            **_usage(data.get("usage")),
        )

    if kind == "plan":
        plan = _plan_items(data.get("items"))
        done = sum(bool(item.get("completed")) for item in plan)
        state = _progress_state(data.get("state"))
        return ProgressEvent(
            task_id,
            "plan",
            state,
            f"Plan updated ({done}/{len(plan)} done)",
            provider_event=provider_event,
            plan=plan,
        )

    if kind == "child_update":
        raw_state = data.get("state")
        state = _progress_state(raw_state)
        role = _preview(data.get("role"), 80) or "subagent"
        child = {
            "role": role,
            "type": _preview(data.get("type"), 80),
            "state": _preview(raw_state, 40),
            "conversation_id": _preview(data.get("provider_session_id"), 120),
            "log_uri": _preview(data.get("log_uri"), 180),
            "workspace_uris": data.get("workspace_uris"),
        }
        return ProgressEvent(
            task_id,
            "subagent",
            state,
            f"{role} subagent {state}",
            provider_event=provider_event,
            subagents=(child,),
            duration_seconds=_number(data.get("duration_seconds")),
            step_index=_integer(data.get("step_index")),
            **_usage(data.get("usage")),
        )

    if kind == "untracked_child":
        tool = _preview(data.get("tool"), 80) or "native subagent tool"
        return ProgressEvent(
            task_id,
            "subagent",
            "warning",
            f"Untracked native child activity via {tool}",
            tool=tool,
            provider_event=provider_event,
            error_preview=_preview(data.get("reason")),
        )

    if kind == "tool":
        return _tool_progress(event, task_id)

    return None


def _tool_progress(event: AgentEvent, task_id: str) -> ProgressEvent:
    data = event.data if isinstance(event.data, dict) else {}
    item = _dict(data.get("item"))
    params = _dict(
        data.get("parameters")
        if data.get("parameters") is not None
        else data.get("input")
    )
    if not params and item:
        params = item

    tool = _preview(
        data.get("name")
        or data.get("item_type")
        or item.get("type"),
        80,
    )
    exit_code = _integer(item.get("exit_code") or item.get("exitCode"))
    raw_error = data.get("error")
    if raw_error is None:
        raw_error = item.get("error")
    state = _progress_state(
        data.get("state") or data.get("status") or item.get("status"),
        error=raw_error,
        exit_code=exit_code,
    )

    changes = item.get("changes")
    files = tuple(
        path
        for change in (changes if isinstance(changes, list) else [])[:12]
        if isinstance(change, dict)
        for path in [_preview(change.get("path"), 180)]
        if path
    )
    path = files[0] if files else _preview(
        _param(
            params,
            "path",
            "filePath",
            "file_path",
            "AbsolutePath",
            "FilePath",
            "TargetFile",
        ),
        180,
    )
    if not files and path and _changes_file(tool) and state == "done":
        files = (path,)

    time_data = _dict(data.get("time"))
    start = _number(time_data.get("start"))
    end = _number(time_data.get("end"))
    duration = _number(data.get("duration_seconds"))
    if duration is None and start is not None and end is not None:
        duration = (end - start) / 1000.0

    output = data.get("output")
    if output is None:
        output = item.get("aggregated_output") or item.get("output")

    return ProgressEvent(
        task_id,
        "tool",
        state,
        f"{(tool or 'Tool').replace('_', ' ')} {state}",
        tool=tool,
        command=_preview(
            _param(params, "CommandLine", "command", "cmd")
            or item.get("command"),
            180,
        ),
        path=path,
        provider_event=event.provider_event,
        exit_code=exit_code,
        duration_seconds=duration,
        output_preview=_preview(_message(output)),
        error_preview=_preview(_message(raw_error)),
        files=files,
        step_index=_integer(data.get("step_index")),
        **_usage(data.get("usage")),
    )


def _lifecycle_state(value: str) -> str:
    if value in {"idle", "done", "completed", "success"}:
        return "done"
    if value in {"failed", "error", "interrupted", "cancelled", "canceled"}:
        return "error"
    return "active"


def _lifecycle_phase(provider_event: str | None) -> str:
    if provider_event == "init":
        return "startup"
    if provider_event == "result":
        return "result"
    return "turn"


def _event_activity(provider_event: str | None, state: str) -> str:
    if not provider_event:
        return "Provider state update"
    label = provider_event.replace("/", " ").replace(".", " ").replace("_", " ")
    return f"{label} {state}".strip()


def _provider_event_is_completed(provider_event: str | None) -> bool:
    if not provider_event:
        return False
    lowered = provider_event.lower()
    return any(token in lowered for token in ("completed", "finish", "finished"))


def _progress_state(
    value: Any,
    *,
    error: Any = None,
    exit_code: int | None = None,
) -> str:
    if error not in (None, "", {}, []):
        return "error"
    if exit_code is not None and exit_code != 0:
        return "error"
    lowered = str(value or "").strip().lower()
    if lowered in {"error", "failed", "failure", "interrupted", "cancelled", "canceled"}:
        return "error"
    if lowered in {"done", "completed", "complete", "idle", "success", "succeeded"}:
        return "done"
    if lowered == "warning":
        return "warning"
    return "active"


def _plan_items(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    result: list[dict[str, Any]] = []
    for item in value[:12]:
        if not isinstance(item, dict):
            continue
        text = _preview(item.get("text"), 120)
        if not text:
            continue
        completed = bool(
            item.get("completed")
            or str(item.get("status") or "").lower() in {"done", "completed"}
        )
        result.append({"text": text, "completed": completed})
    return tuple(result)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("message") or value.get("text") or value.get("output")
    return value


def _changes_file(tool: str | None) -> bool:
    return bool(
        tool
        and any(
            word in tool.lower()
            for word in ("write", "edit", "patch", "replace", "filechange", "file_change")
        )
    )


def _param(params: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in params.items()}
    return next(
        (lowered[name.lower()] for name in names if name.lower() in lowered),
        None,
    )


def _preview(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        if value is None:
            return None
        value = str(value)
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
    tokens = _dict(data.get("tokens"))
    cache = _dict(tokens.get("cache"))

    input_tokens = _integer(
        data.get("input_tokens")
        or data.get("inputTokens")
        or data.get("input")
        or tokens.get("input")
        or tokens.get("input_tokens")
    )
    output_tokens = _integer(
        data.get("output_tokens")
        or data.get("outputTokens")
        or data.get("output")
        or tokens.get("output")
        or tokens.get("output_tokens")
    )
    total_tokens = _integer(
        data.get("total_tokens")
        or data.get("totalTokens")
        or tokens.get("total")
        or tokens.get("total_tokens")
    )
    cache_tokens = _integer(
        data.get("cache_read_tokens")
        or data.get("cached_input_tokens")
        or data.get("cacheReadTokens")
        or cache.get("read")
    )

    if total_tokens is None and (
        input_tokens is not None
        or output_tokens is not None
        or cache_tokens is not None
    ):
        total_tokens = (
            int(input_tokens or 0)
            + int(output_tokens or 0)
            + int(cache_tokens or 0)
        )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cache_tokens": cache_tokens,
    }
