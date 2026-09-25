from __future__ import annotations

import json
import re
from typing import Any

from cortex_relay.core.agents import AgentEvent


_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|access[_-]?token|authorization|password|secret)")
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_LONG_TOKEN = re.compile(r"(?<![\w])[A-Za-z0-9_+/=-]{48,}(?![\w])")


def normalize_agent_events(
    provider: str,
    line: str,
    session_id: str,
    stream: str = "stdout",
) -> tuple[AgentEvent, ...]:
    """Translate provider-native streaming records into durable agent semantics."""

    if stream == "stderr":
        text = line.strip()
        if not text:
            return ()
        return (
            AgentEvent(
                session_id=session_id,
                kind="diagnostic",
                data={"stream": "stderr", "text": _sanitize(text[:4000])},
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
        return _antigravity(event, session_id)
    if provider == "codex":
        return _codex(event, session_id)
    if provider == "opencode":
        return _opencode(event, session_id)
    return (
        AgentEvent(
            session_id=session_id,
            kind="provider_event",
            data={"event": event},
            provider_event=str(event.get("type") or event.get("event") or "unknown"),
        ),
    )


def _antigravity(event: dict[str, Any], session_id: str) -> tuple[AgentEvent, ...]:
    kind = str(event.get("event") or "")
    if kind == "init":
        return (_event(session_id, "lifecycle", {"state": "running"}, kind),)
    if kind == "result":
        result = _dict(event.get("result"))
        return (
            _event(
                session_id,
                "lifecycle",
                {
                    "state": "idle"
                    if str(result.get("status") or "").upper() in {"", "SUCCESS"}
                    else "failed",
                    "status": result.get("status"),
                    "usage": result.get("usage"),
                    "duration_seconds": result.get("duration_seconds"),
                    "error": result.get("error"),
                },
                kind,
            ),
        )
    if kind != "step_update":
        return ()

    step = _dict(event.get("step_update"))
    out: list[AgentEvent] = []
    step_type = str(step.get("step_type") or "")
    if step_type == "agent_response":
        delta = step.get("text_delta")
        if not isinstance(delta, str):
            delta = step.get("text")
        if isinstance(delta, str) and delta:
            out.append(
                _event(
                    session_id,
                    "text_delta",
                    {
                        "text": delta,
                        "step_index": step.get("step_index"),
                        "usage": step.get("usage"),
                        "duration_seconds": step.get("duration_seconds"),
                    },
                    "step_update",
                )
            )

    for child in _agy_children(step.get("subagent_info")):
        out.append(
            _event(
                session_id,
                "child_update",
                {
                    **child,
                    "step_index": step.get("step_index"),
                    "usage": step.get("usage"),
                    "duration_seconds": step.get("duration_seconds"),
                },
                "step_update",
            )
        )

    if step_type == "tool":
        info = _dict(step.get("tool_info"))
        tool_name = _text(step.get("tool_name") or info.get("name"))
        parameters = info.get("parameters")
        out.append(
            _event(
                session_id,
                "tool",
                {
                    "name": tool_name,
                    "state": step.get("state"),
                    "parameters": parameters,
                    "output": info.get("output"),
                    "error": info.get("error"),
                    "step_index": step.get("step_index"),
                    "usage": step.get("usage"),
                    "duration_seconds": step.get("duration_seconds"),
                },
                "step_update",
            )
        )
        if (
            _looks_like_child_tool(tool_name, parameters)
            and not any(item.kind == "child_update" for item in out)
        ):
            out.append(
                _event(
                    session_id,
                    "untracked_child",
                    {
                        "tool": tool_name,
                        "parameters": parameters,
                        "reason": "provider emitted child-management activity without a child session identifier",
                    },
                    "step_update",
                )
            )
    return tuple(out)


def _codex(event: dict[str, Any], session_id: str) -> tuple[AgentEvent, ...]:
    # app-server JSON-RPC notifications
    method = event.get("method")
    if isinstance(method, str):
        params = _dict(event.get("params"))
        if method == "thread/started":
            thread = _dict(params.get("thread"))
            provider_id = _text(thread.get("id"))
            return (
                _event(
                    session_id,
                    "provider_session",
                    {"provider_session_id": provider_id},
                    method,
                ),
            ) if provider_id else ()

        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            return (
                _event(
                    session_id,
                    "text_delta",
                    {
                        "text": delta,
                        "turn_id": params.get("turnId"),
                        "item_id": params.get("itemId"),
                    },
                    method,
                ),
            ) if isinstance(delta, str) and delta else ()

        if method in {"turn/started", "turn/completed"}:
            turn = _dict(params.get("turn"))
            return (
                _event(
                    session_id,
                    "lifecycle",
                    {
                        "state": "running" if method == "turn/started" else "idle",
                        "turn_id": turn.get("id"),
                        "status": turn.get("status"),
                        "usage": turn.get("usage"),
                    },
                    method,
                ),
            )

        if method in {"item/started", "item/completed"}:
            item = _dict(params.get("item"))
            item_type = str(item.get("type") or "")
            if item_type in {"todoList", "todo_list"}:
                return (
                    _event(
                        session_id,
                        "plan",
                        {
                            "items": item.get("items"),
                            "state": "done" if method == "item/completed" else "running",
                            "turn_id": params.get("turnId"),
                        },
                        method,
                    ),
                )
            if item_type == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text:
                    return (
                        _event(
                            session_id,
                            "message",
                            {
                                "text": text,
                                "phase": item.get("phase"),
                                "turn_id": params.get("turnId"),
                                "item_id": item.get("id"),
                            },
                            method,
                        ),
                    )
            child = _codex_child(item)
            if child is not None:
                return (_event(session_id, "child_update", child, method),)
            return (
                _event(
                    session_id,
                    "tool",
                    {
                        "item_type": item_type,
                        "state": "done" if method == "item/completed" else "running",
                        "item": item,
                        "turn_id": params.get("turnId"),
                    },
                    method,
                ),
            )

        if method == "error":
            return (
                _event(session_id, "diagnostic", {"error": params}, method),
            )
        return ()

    # codex exec JSONL fallback
    kind = str(event.get("type") or "")
    if kind == "thread.started":
        provider_id = _text(event.get("thread_id"))
        return (
            _event(
                session_id,
                "provider_session",
                {"provider_session_id": provider_id},
                kind,
            ),
        ) if provider_id else ()
    if kind in {"turn.started", "turn.completed", "turn.failed"}:
        return (
            _event(
                session_id,
                "lifecycle",
                {
                    "state": "running"
                    if kind == "turn.started"
                    else ("failed" if kind == "turn.failed" else "idle"),
                    "usage": event.get("usage"),
                },
                kind,
            ),
        )
    if kind in {"item.started", "item.updated", "item.completed"}:
        item = _dict(event.get("item"))
        if item.get("type") == "todo_list":
            return (
                _event(
                    session_id,
                    "plan",
                    {
                        "items": item.get("items"),
                        "state": "done" if kind == "item.completed" else "running",
                    },
                    kind,
                ),
            )
        if item.get("type") == "agent_message":
            text = item.get("text")
            return (
                _event(
                    session_id,
                    "message",
                    {"text": text, "phase": item.get("phase"), "item_id": item.get("id")},
                    kind,
                ),
            ) if isinstance(text, str) and text else ()
        child = _codex_child(item)
        if child is not None:
            return (_event(session_id, "child_update", child, kind),)
        return (
            _event(
                session_id,
                "tool",
                {
                    "item_type": item.get("type"),
                    "state": "done" if kind == "item.completed" else "running",
                    "item": item,
                },
                kind,
            ),
        )
    return ()


def _opencode(event: dict[str, Any], session_id: str) -> tuple[AgentEvent, ...]:
    kind = str(event.get("type") or "")
    provider_session = _text(event.get("sessionID") or event.get("sessionId"))
    out: list[AgentEvent] = []
    if provider_session:
        out.append(
            _event(
                session_id,
                "provider_session",
                {"provider_session_id": provider_session},
                kind or "session",
            )
        )

    part = _dict(event.get("part"))
    if kind == "text":
        text = part.get("text")
        if isinstance(text, str) and text:
            out.append(
                _event(
                    session_id,
                    "message",
                    {
                        "text": text,
                        "message_id": part.get("messageID") or part.get("messageId"),
                        "part_id": part.get("id"),
                    },
                    kind,
                )
            )
    elif kind in {"step_start", "step_finish"}:
        out.append(
            _event(
                session_id,
                "lifecycle",
                {
                    "state": "running" if kind == "step_start" else "idle",
                    "usage": part.get("tokens"),
                },
                kind,
            )
        )
    elif kind == "error":
        out.append(_event(session_id, "diagnostic", {"error": event.get("error")}, kind))

    if part.get("type") == "tool":
        state = _dict(part.get("state"))
        tool = _text(part.get("tool"))
        out.append(
            _event(
                session_id,
                "tool",
                {
                    "name": tool,
                    "status": state.get("status"),
                    "input": state.get("input"),
                    "output": state.get("output"),
                    "error": state.get("error"),
                    "metadata": state.get("metadata"),
                    "time": state.get("time"),
                },
                kind,
            )
        )
        if tool == "task":
            child_id = _find_session_id(
                state.get("metadata"),
                state.get("output"),
                state.get("input"),
            )
            if child_id:
                out.append(
                    _event(
                        session_id,
                        "child_update",
                        {
                            "provider_session_id": child_id,
                            "role": _nested_text(state.get("input"), "subagent_type") or "subagent",
                            "state": "idle"
                            if state.get("status") == "completed"
                            else "running",
                            "metadata": state.get("metadata"),
                        },
                        kind,
                    )
                )
            else:
                out.append(
                    _event(
                        session_id,
                        "untracked_child",
                        {
                            "tool": tool,
                            "parameters": state.get("input"),
                            "reason": "OpenCode task tool did not expose a child session identifier",
                        },
                        kind,
                    )
                )
    return tuple(out)


def _looks_like_child_tool(name: str | None, parameters: Any) -> bool:
    if not name:
        return False
    normalized = name.strip().lower().replace("-", "_")
    if normalized not in {
        "manage_task",
        "task",
        "spawn_agent",
        "create_subagent",
        "create_sub_agent",
    }:
        return False
    if normalized != "manage_task":
        return True
    params = _dict(parameters)
    action = _text(
        params.get("action")
        or params.get("operation")
        or params.get("command")
        or params.get("mode")
    )
    if action is None:
        return True
    return action.strip().lower() in {
        "create",
        "spawn",
        "start",
        "run",
        "delegate",
        "launch",
    }


def _agy_children(value: Any) -> tuple[dict[str, Any], ...]:
    raw = _dict(value).get("subagents")
    if not isinstance(raw, list):
        return ()
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        provider_id = _text(item.get("conversation_id"))
        if not provider_id:
            continue
        result.append(
            {
                "provider_session_id": provider_id,
                "role": _text(item.get("role")) or "subagent",
                "type": _text(item.get("type_name")),
                "state": _text(item.get("state") or item.get("status")) or "running",
                "log_uri": _text(item.get("log_uri")),
                "workspace_uris": item.get("workspace_uris"),
            }
        )
    return tuple(result)


def _codex_child(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = str(item.get("type") or "")
    if item_type in {"subAgentActivity", "sub_agent_activity"}:
        provider_id = _text(item.get("agentThreadId") or item.get("agent_thread_id"))
        if provider_id:
            kind = _text(item.get("kind")) or "interacted"
            return {
                "provider_session_id": provider_id,
                "role": "subagent",
                "state": "idle" if kind == "completed" else (
                    "failed" if kind == "interrupted" else "running"
                ),
                "metadata": {
                    "activity_kind": kind,
                    "agent_path": item.get("agentPath") or item.get("agent_path"),
                },
            }

    if item_type in {"collab_tool_call", "collabAgentToolCall"}:
        states = _dict(item.get("agents_states") or item.get("agentsStates"))
        receivers = item.get("receiverThreadIds") or item.get("receiver_thread_ids")
        receiver_ids = [
            str(value)
            for value in (receivers if isinstance(receivers, list) else [])
            if isinstance(value, str) and value.strip()
        ]
        provider_id: str | None = None
        state: Any = "running"
        if states:
            provider_id, state = next(iter(states.items()))
        elif receiver_ids:
            provider_id = receiver_ids[0]
        if provider_id:
            tool = _text(item.get("tool")) or "collab"
            return {
                "provider_session_id": str(provider_id),
                "role": "subagent",
                "state": str(state),
                "metadata": {
                    "tool": tool,
                    "sender_thread_id": item.get("senderThreadId") or item.get("sender_thread_id"),
                    "receiver_thread_ids": receiver_ids,
                    "model": item.get("model"),
                    "reasoning_effort": item.get("reasoningEffort") or item.get("reasoning_effort"),
                    "agents_states": states,
                },
            }
    if item_type in {"create_subagent_call", "createSubagentCall"}:
        provider_id = _text(item.get("agent_id") or item.get("agentId"))
        if provider_id:
            return {
                "provider_session_id": provider_id,
                "role": _text(item.get("agent_type") or item.get("agentType")) or "subagent",
                "state": "running",
            }
    return None


def _find_session_id(*values: Any) -> str | None:
    keys = {"sessionid", "session_id", "childsessionid", "child_session_id"}
    stack = list(values)
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in keys:
                    found = _text(item)
                    if found:
                        return found
                stack.append(item)
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, str):
            # Task outputs often include a compact JSON object with the child
            # session identifier.
            text = value.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    stack.append(json.loads(text))
                except json.JSONDecodeError:
                    pass
    return None


def _nested_text(value: Any, key: str) -> str | None:
    if not isinstance(value, dict):
        return None
    return _text(value.get(key))


def _event(
    session_id: str,
    kind: str,
    data: dict[str, Any],
    provider_event: str,
) -> AgentEvent:
    return AgentEvent(
        session_id=session_id,
        kind=kind,
        data=_sanitize(data),
        provider_event=provider_event,
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        text = _BEARER.sub("Bearer [redacted]", value)
        text = _LONG_TOKEN.sub("[redacted]", text)
        return text[:16000]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            name = str(key)
            if _SECRET_KEY.search(name):
                result[name] = "[redacted]"
            else:
                result[name] = _sanitize(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:1000]
