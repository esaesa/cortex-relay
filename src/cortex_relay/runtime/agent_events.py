from __future__ import annotations

import json
from typing import Any

from cortex_relay.core.agents import AgentEvent


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
                data={"stream": "stderr", "text": text[:4000]},
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
                    "usage": result.get("usage"),
                    "duration_seconds": result.get("duration_seconds"),
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
                    },
                    "step_update",
                )
            )

    for child in _agy_children(step.get("subagent_info")):
        out.append(
            _event(
                session_id,
                "child_update",
                child,
                "step_update",
            )
        )

    if step_type == "tool":
        info = _dict(step.get("tool_info"))
        out.append(
            _event(
                session_id,
                "tool",
                {
                    "name": step.get("tool_name") or info.get("name"),
                    "state": step.get("state"),
                    "parameters": info.get("parameters"),
                    "output": info.get("output"),
                    "error": info.get("error"),
                    "step_index": step.get("step_index"),
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
                {"state": "running" if kind == "step_start" else "idle"},
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
    return tuple(out)


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
    if item_type in {"collab_tool_call", "collabAgentToolCall"}:
        states = _dict(item.get("agents_states") or item.get("agentsStates"))
        if states:
            provider_id, state = next(iter(states.items()))
            return {
                "provider_session_id": str(provider_id),
                "role": "subagent",
                "state": str(state),
                "metadata": {"agents_states": states},
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
        data=data,
        provider_event=provider_event,
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
