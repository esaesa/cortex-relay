"""Bounded dashboard progress derived from the richer durable agent event stream."""

from __future__ import annotations

import json
import re

from dataclasses import dataclass
from typing import Any, Callable

from cortex_relay.core.agents import AgentEvent


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


def normalize_agent_events(
    provider: str,
    line: str,
    session_id: str,
    stream: str = "stdout",
) -> tuple[AgentEvent, ...]:
    """Translate provider-native output into durable semantic agent events.

    Unlike ProgressEvent, this stream preserves response text deltas and native
    child-agent handles. It intentionally excludes hidden reasoning internals.
    """

    if stream == "stderr":
        preview = _preview(line, 2000)
        if not preview:
            return ()
        return (
            AgentEvent(
                session_id=session_id,
                kind="diagnostic",
                data={"stream": "stderr", "text": preview},
                provider_event="stderr",
            ),
        )

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return ()
    if not isinstance(event, dict):
        return ()

    if provider == "antigravity":
        return _antigravity_agent_events(event, session_id)
    if provider == "codex":
        return _codex_agent_events(event, session_id)
    if provider == "opencode":
        return _opencode_agent_events(event, session_id)
    return ()


def _antigravity_agent_events(
    event: dict[str, Any], session_id: str
) -> tuple[AgentEvent, ...]:
    kind = event.get("event")
    if kind == "init":
        return (
            AgentEvent(
                session_id=session_id,
                kind="provider_started",
                data={},
                provider_event="init",
            ),
        )
    if kind == "result":
        result = _dict(event.get("result"))
        return (
            AgentEvent(
                session_id=session_id,
                kind="provider_result",
                data={
                    "status": result.get("status"),
                    "conversation_id": result.get("conversation_id"),
                    "duration_seconds": result.get("duration_seconds"),
                    "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
                },
                provider_event="result",
            ),
        )
    if kind != "step_update":
        return ()

    step = _dict(event.get("step_update"))
    step_type = step.get("step_type")
    step_index = _integer(step.get("step_index"))
    events: list[AgentEvent] = []

    if step_type == "agent_response":
        text = (
            step.get("text_delta")
            or step.get("delta")
            or step.get("text")
            or step.get("response")
        )
        if isinstance(text, str) and text:
            events.append(
                AgentEvent(
                    session_id=session_id,
                    kind="response_delta",
                    data={"text": text, "step_index": step_index},
                    provider_event="step_update",
                )
            )

    subagent_info = _dict(step.get("subagent_info"))
    raw_agents = subagent_info.get("subagents")
    if isinstance(raw_agents, list) and raw_agents:
        agents: list[dict[str, Any]] = []
        for agent in raw_agents:
            if not isinstance(agent, dict):
                continue
            agents.append(
                {
                    "provider_session_id": _text(
                        agent.get("conversation_id") or agent.get("session_id")
                    ),
                    "role": _text(agent.get("role")),
                    "type": _text(agent.get("type_name") or agent.get("type")),
                    "state": _text(agent.get("state") or agent.get("status")),
                    "log_uri": _text(agent.get("log_uri")),
                    "workspace_uris": (
                        agent.get("workspace_uris")
                        if isinstance(agent.get("workspace_uris"), list)
                        else []
                    ),
                }
            )
        if agents:
            events.append(
                AgentEvent(
                    session_id=session_id,
                    kind="child_snapshot",
                    data={"children": agents, "step_index": step_index},
                    provider_event="step_update",
                )
            )

    if step_type == "tool":
        info = _dict(step.get("tool_info"))
        params = _dict(info.get("parameters"))
        events.append(
            AgentEvent(
                session_id=session_id,
                kind="tool",
                data={
                    "name": step.get("tool_name") or info.get("name"),
                    "state": step.get("state"),
                    "parameters": params,
                    "output": _message(info.get("output")),
                    "error": _message(info.get("error")),
                    "step_index": step_index,
                },
                provider_event="step_update",
            )
        )
    return tuple(events)


def _codex_agent_events(
    event: dict[str, Any], session_id: str
) -> tuple[AgentEvent, ...]:
    kind = _text(event.get("type"))
    if kind == "thread.started":
        return (
            AgentEvent(
                session_id=session_id,
                kind="provider_session",
                data={"provider_session_id": _text(event.get("thread_id"))},
                provider_event=kind,
            ),
        )
    if kind in {"turn.started", "turn.completed", "turn.failed"}:
        return (
            AgentEvent(
                session_id=session_id,
                kind="turn",
                data={
                    "state": kind.split(".", 1)[1] if "." in kind else kind,
                    "usage": event.get("usage") if isinstance(event.get("usage"), dict) else {},
                },
                provider_event=kind,
            ),
        )
    if kind not in {"item.started", "item.updated", "item.completed"}:
        return ()

    item = _dict(event.get("item"))
    item_type = _text(item.get("type"))
    if item_type in {"agent_message", "agentMessage"}:
        text = item.get("text") or item.get("delta")
        if isinstance(text, str) and text:
            return (
                AgentEvent(
                    session_id=session_id,
                    kind="response_delta",
                    data={"text": text, "item_id": item.get("id")},
                    provider_event=kind,
                ),
            )

    if item_type == "collab_tool_call":
        states = _dict(item.get("agents_states"))
        children = [
            {
                "provider_session_id": _text(child_id),
                "state": _text(state),
                "role": None,
                "type": "codex-subagent",
            }
            for child_id, state in states.items()
            if _text(child_id)
        ]
        return (
            AgentEvent(
                session_id=session_id,
                kind="child_snapshot",
                data={"children": children},
                provider_event=kind,
            ),
        ) if children else ()

    if item_type in {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
    }:
        return (
            AgentEvent(
                session_id=session_id,
                kind="tool",
                data={
                    "type": item_type,
                    "status": item.get("status"),
                    "command": item.get("command"),
                    "changes": item.get("changes"),
                    "output": item.get("aggregated_output"),
                    "error": _dict(item.get("error")).get("message"),
                },
                provider_event=kind,
            ),
        )
    return ()


def _opencode_agent_events(
    event: dict[str, Any], session_id: str
) -> tuple[AgentEvent, ...]:
    kind = _text(event.get("type"))
    part = _dict(event.get("part"))

    provider_session_id = _text(
        event.get("sessionID") or part.get("sessionID") or part.get("sessionId")
    )
    events: list[AgentEvent] = []
    if provider_session_id:
        events.append(
            AgentEvent(
                session_id=session_id,
                kind="provider_session",
                data={"provider_session_id": provider_session_id},
                provider_event=kind,
            )
        )

    if kind == "text" or part.get("type") == "text":
        text = part.get("text") or event.get("text")
        if isinstance(text, str) and text:
            events.append(
                AgentEvent(
                    session_id=session_id,
                    kind="response_delta",
                    data={"text": text},
                    provider_event=kind,
                )
            )

    if part.get("type") == "tool":
        state = _dict(part.get("state"))
        tool = _text(part.get("tool"))
        metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
        child_id = _text(
            metadata.get("sessionID")
            or metadata.get("sessionId")
            or metadata.get("childSessionID")
            or metadata.get("childSessionId")
        )
        if tool in {"task", "Task"} and child_id:
            events.append(
                AgentEvent(
                    session_id=session_id,
                    kind="child_snapshot",
                    data={
                        "children": [
                            {
                                "provider_session_id": child_id,
                                "state": state.get("status"),
                                "role": _text(_dict(state.get("input")).get("subagent_type")),
                                "type": "opencode-subagent",
                            }
                        ]
                    },
                    provider_event=kind,
                )
            )
        events.append(
            AgentEvent(
                session_id=session_id,
                kind="tool",
                data={
                    "name": tool,
                    "status": state.get("status"),
                    "input": state.get("input"),
                    "output": state.get("output"),
                    "error": state.get("error"),
                    "metadata": metadata,
                },
                provider_event=kind,
            )
        )

    if kind in {"step_start", "step_finish"}:
        events.append(
            AgentEvent(
                session_id=session_id,
                kind="turn",
                data={"state": "started" if kind == "step_start" else "completed"},
                provider_event=kind,
            )
        )
    return tuple(events)


def normalize_progress(provider: str, line: str, task_id: str, stream: str = "stdout") -> ProgressEvent | None:
    if stream == "stderr":
        preview = _preview(line)
        if not preview:
            return None
        error = any(word in preview.lower() for word in ("error", "failed", "denied"))
        return ProgressEvent(task_id, "diagnostic", "error" if error else "warning", "Provider diagnostic", provider_event="stderr", error_preview=preview)
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
        return ProgressEvent(task_id, "startup", "active", "Provider started", provider_event="init")
    if kind == "result":
        result = _dict(event.get("result"))
        failed = str(result.get("status") or "").upper() not in {"", "SUCCESS"}
        return ProgressEvent(
            task_id, "result", "error" if failed else "done",
            "Provider failed" if failed else "Provider finished",
            provider_event="result",
            duration_seconds=_number(result.get("duration_seconds")),
            error_preview=_preview(_message(result.get("error"))),
            **_usage(result.get("usage")),
        )
    if kind != "step_update":
        return None
    step = _dict(event.get("step_update"))
    index = _integer(step.get("step_index"))
    if step.get("step_type") == "agent_response":
        delta = step.get("text_delta")
        if not isinstance(delta, str):
            delta = step.get("text")
        return ProgressEvent(
            task_id,
            "response",
            "active",
            "Agent response",
            provider_event="step_update",
            step_index=index,
            output_preview=_preview(delta),
        )
    agents = _subagents(step.get("subagent_info"))
    if agents and not step.get("tool_info"):
        state = "done" if step.get("state") == "DONE" else "active"
        return ProgressEvent(
            task_id, "subagent", state, f"{len(agents)} subagent(s) {state}",
            provider_event="step_update", step_index=index, subagents=agents,
            duration_seconds=_number(step.get("duration_seconds")),
            **_usage(step.get("usage")),
        )
    if step.get("step_type") != "tool":
        return None
    info = _dict(step.get("tool_info"))
    params = _dict(info.get("parameters"))
    tool = _preview(step.get("tool_name") or info.get("name"), 80)
    error = info.get("error")
    message = _dict(error).get("message") if isinstance(error, dict) else error
    state = "error" if message else ("done" if step.get("state") == "DONE" else "active")
    path = _preview(_param(params, "path", "file_path", "AbsolutePath", "FilePath", "TargetFile"), 180)
    return ProgressEvent(
        task_id, "tool", state, f"{tool or 'Tool'} {state}", tool=tool,
        command=_preview(_param(params, "CommandLine", "command", "cmd"), 180),
        path=path,
        provider_event="step_update", step_index=index,
        duration_seconds=_number(step.get("duration_seconds")),
        output_preview=_preview(_message(info.get("output"))), error_preview=_preview(_message(message)),
        files=(path,) if path and _changes_file(tool) and state == "done" else (),
        subagents=agents, **_usage(step.get("usage")),
    )


def _codex(event: dict[str, Any], task_id: str) -> ProgressEvent | None:
    method = event.get("method")
    if isinstance(method, str):
        params = _dict(event.get("params"))
        if method in {"turn/started", "turn/completed"}:
            turn = _dict(params.get("turn"))
            state = "active" if method == "turn/started" else "done"
            return ProgressEvent(
                task_id,
                "turn",
                state,
                method.replace("/", " "),
                provider_event=method,
                **_usage(turn.get("usage")),
            )
        if method == "item/agentMessage/delta":
            return ProgressEvent(
                task_id,
                "response",
                "active",
                "Agent response",
                provider_event=method,
                output_preview=_preview(params.get("delta")),
            )
        if method in {"item/started", "item/completed"}:
            item = _dict(params.get("item"))
            item_type = str(item.get("type") or "")
            if item_type == "agentMessage":
                return ProgressEvent(
                    task_id,
                    "response",
                    "done" if method == "item/completed" else "active",
                    "Agent message",
                    provider_event=method,
                    output_preview=_preview(item.get("text")),
                )
            state = "done" if method == "item/completed" else "active"
            files = ()
            path = None
            if item_type == "fileChange":
                changes = item.get("changes")
                files = tuple(
                    p for change in (changes if isinstance(changes, list) else [])[:12]
                    if isinstance(change, dict)
                    for p in [_preview(change.get("path"), 180)] if p
                )
                path = files[0] if files else None
            return ProgressEvent(
                task_id,
                "tool",
                state,
                f"{item_type or 'item'} {state}",
                tool=item_type or None,
                path=path,
                files=files,
                provider_event=method,
                subagents=_codex_agents(item),
            )
        if method == "error":
            return ProgressEvent(
                task_id,
                "diagnostic",
                "error",
                "Codex error",
                provider_event=method,
                error_preview=_preview(_message(params.get("error") or params)),
            )
        return None

    kind = event.get("type")
    if kind in {"turn.started", "turn.completed", "turn.failed"}:
        state = "active" if kind == "turn.started" else ("error" if kind == "turn.failed" else "done")
        return ProgressEvent(task_id, "turn", state, str(kind).replace(".", " "), provider_event=str(kind), **_usage(event.get("usage")))
    if kind not in {"item.started", "item.updated", "item.completed"}:
        return None
    item = _dict(event.get("item"))
    item_type = item.get("type")
    if item_type == "todo_list":
        entries = item.get("items")
        plan = tuple(
            {"text": _preview(entry.get("text"), 120), "completed": bool(entry.get("completed"))}
            for entry in (entries if isinstance(entries, list) else [])[:12] if isinstance(entry, dict)
        )
        done = sum(bool(entry["completed"]) for entry in plan)
        return ProgressEvent(task_id, "plan", "active", f"Plan updated ({done}/{len(plan)} done)", provider_event=str(kind), plan=plan)
    if item_type == "error":
        return ProgressEvent(task_id, "diagnostic", "error", "Codex error", provider_event=str(kind), error_preview=_preview(item.get("message")))
    if item_type not in {"command_execution", "file_change", "mcp_tool_call", "web_search", "collab_tool_call"}:
        return None
    state = "active" if kind != "item.completed" else "done"
    exit_code = _integer(item.get("exit_code"))
    if item.get("status") in {"failed", "error"} or (exit_code is not None and exit_code != 0):
        state = "error"
    changes = item.get("changes")
    files = tuple(
        path for change in (changes if isinstance(changes, list) else [])[:12]
        if isinstance(change, dict) for path in [_preview(change.get("path"), 180)] if path
    )
    tool = str(item_type)
    return ProgressEvent(
        task_id, "tool", state, f"{tool.replace('_', ' ')} {state}", tool=tool,
        command=_preview(item.get("command"), 180), path=files[0] if files else None,
        files=files, exit_code=exit_code, provider_event=str(kind),
        output_preview=_preview(item.get("aggregated_output")),
        error_preview=_preview(_dict(item.get("error")).get("message")),
        subagents=_codex_agents(item) if item_type == "collab_tool_call" else (),
    )


def _opencode(event: dict[str, Any], task_id: str) -> ProgressEvent | None:
    kind = event.get("type")
    part = _dict(event.get("part"))
    if kind in {"step_start", "step_finish"}:
        state = "active" if kind == "step_start" else "done"
        return ProgressEvent(task_id, "turn", state, "Step started" if state == "active" else "Step finished", provider_event=str(kind), **_usage(part.get("tokens")))
    if kind == "error":
        error = event.get("error")
        message = _dict(error).get("message") if isinstance(error, dict) else error
        return ProgressEvent(task_id, "diagnostic", "error", "OpenCode error", provider_event="error", error_preview=_preview(message))
    if kind == "text":
        return ProgressEvent(
            task_id,
            "response",
            "active",
            "Agent response",
            provider_event=str(kind),
            output_preview=_preview(part.get("text")),
        )
    if part.get("type") != "tool":
        return None
    tool = _preview(part.get("tool"), 80)
    state_data = _dict(part.get("state"))
    status = state_data.get("status")
    state = "error" if status == "error" else ("done" if status == "completed" else "active")
    params = _dict(state_data.get("input"))
    times = _dict(state_data.get("time"))
    start, end = _number(times.get("start")), _number(times.get("end"))
    duration = (end - start) / 1000 if start is not None and end is not None else None
    path = _preview(_param(params, "filePath", "path", "file_path"), 180)
    return ProgressEvent(
        task_id, "tool", state, f"{tool or 'Tool'} {state}", tool=tool,
        command=_preview(params.get("command"), 180),
        path=path,
        provider_event=str(kind), duration_seconds=duration,
        output_preview=_preview(_message(state_data.get("output"))),
        error_preview=_preview(_message(state_data.get("error"))),
        files=(path,) if path and _changes_file(tool) and state == "done" else (),
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("message") or value.get("text") or value.get("output")
    return value


def _changes_file(tool: str | None) -> bool:
    return bool(tool and any(word in tool.lower() for word in ("write", "edit", "patch", "replace")))


def _param(params: dict[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in params.items()}
    return next((lowered[name.lower()] for name in names if name.lower() in lowered), None)


def _preview(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(_ANSI.sub("", value).split())
    text = _SECRET.sub(r"\1\2[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    text = _LONG_TOKEN.sub("[redacted]", text)
    return text[:limit] if text else None


def _integer(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _usage(value: Any) -> dict[str, int | None]:
    data = _dict(value)
    return {
        "input_tokens": _integer(data.get("input_tokens") or data.get("input")),
        "output_tokens": _integer(data.get("output_tokens") or data.get("output")),
        "total_tokens": _integer(data.get("total_tokens")),
        "cache_tokens": _integer(data.get("cache_read_tokens") or data.get("cached_input_tokens")),
    }


def _subagents(value: Any) -> tuple[dict[str, Any], ...]:
    agents = _dict(value).get("subagents")
    if not isinstance(agents, list):
        return ()
    return tuple(
        {
            "role": _preview(agent.get("role"), 80),
            "type": _preview(agent.get("type_name"), 80),
            "state": _preview(agent.get("state") or agent.get("status"), 40),
            "conversation_id": _preview(agent.get("conversation_id"), 80),
            "log_uri": _preview(agent.get("log_uri"), 180),
            "workspace_uris": agent.get("workspace_uris"),
        }
        for agent in agents[:8] if isinstance(agent, dict)
    )


def _codex_agents(item: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    states = _dict(item.get("agents_states") or item.get("agentsStates"))
    if states:
        return tuple(
            {"conversation_id": _preview(key, 80), "state": _preview(str(value), 40)}
            for key, value in list(states.items())[:8]
        )
    item_type = str(item.get("type") or "")
    if item_type in {"collab_tool_call", "collabAgentToolCall"}:
        receivers = item.get("receiverThreadIds") or item.get("receiver_thread_ids")
        if isinstance(receivers, list):
            return tuple(
                {"conversation_id": _preview(value, 80), "state": "running"}
                for value in receivers[:8]
                if isinstance(value, str) and value.strip()
            )
    if item_type in {"subAgentActivity", "sub_agent_activity"}:
        agent_id = _preview(item.get("agentThreadId") or item.get("agent_thread_id"), 80)
        kind = str(item.get("kind") or "")
        if agent_id:
            return ({
                "conversation_id": agent_id,
                "state": "done" if kind == "completed" else (
                    "error" if kind == "interrupted" else "running"
                ),
            },)
    if item_type in {"create_subagent_call", "createSubagentCall"}:
        agent_id = _preview(item.get("agent_id") or item.get("agentId"), 80)
        if agent_id:
            return ({"conversation_id": agent_id, "state": "running"},)
    return ()


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
