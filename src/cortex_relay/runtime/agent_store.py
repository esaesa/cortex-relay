from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex_relay.core.agents import AgentEvent, AgentMessage, AgentSession


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentStore:
    """Durable provider-neutral agent/session state.

    Task records describe orchestration. Agent records describe the live or
    resumable provider conversations that actually perform the work.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def create(
        self,
        *,
        provider: str,
        workspace: Path,
        task_id: str | None,
        model: str | None,
        reasoning: str,
        access: str,
        role: str,
        objective: str,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentSession:
        session_id = f"agent-{uuid4().hex}"
        now = _utc_now()
        root = root_session_id or session_id
        session = AgentSession(
            session_id=session_id,
            provider=provider,
            workspace=workspace.expanduser().resolve(),
            state="starting",
            task_id=task_id,
            model=model,
            reasoning=reasoning,
            access=access,
            parent_session_id=parent_session_id,
            root_session_id=root,
            role=role,
            objective=objective,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        self._write_session(session)
        return session

    def get(self, session_id: str) -> AgentSession:
        data = self._read_json(self._session_path(session_id))
        if not isinstance(data, dict):
            raise ValueError(f"unknown agent session: {session_id}")
        return self._from_dict(data)

    def find_by_provider_session(
        self, provider: str, provider_session_id: str
    ) -> AgentSession | None:
        directory = self.root / "agent-sessions"
        if not directory.exists():
            return None
        for path in directory.glob("*.json"):
            data = self._read_json(path)
            if not isinstance(data, dict):
                continue
            if (
                data.get("provider") == provider
                and data.get("provider_session_id") == provider_session_id
            ):
                return self._from_dict(data)
        return None

    def update(self, session_id: str, **updates: Any) -> AgentSession:
        session = self.get(session_id)
        allowed = {
            "state",
            "provider_session_id",
            "task_id",
            "model",
            "reasoning",
            "access",
            "parent_session_id",
            "root_session_id",
            "role",
            "objective",
            "metadata",
        }
        invalid = set(updates) - allowed
        if invalid:
            raise ValueError(f"unsupported agent session fields: {sorted(invalid)}")
        updates["updated_at"] = _utc_now()
        session = replace(session, **updates)
        self._write_session(session)
        return session

    def bind_provider_session(
        self, session_id: str, provider_session_id: str | None
    ) -> AgentSession:
        if not provider_session_id:
            return self.get(session_id)
        return self.update(session_id, provider_session_id=provider_session_id)

    def upsert_child(
        self,
        *,
        parent_session_id: str,
        provider_session_id: str,
        provider: str,
        state: str = "running",
        role: str = "subagent",
        model: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentSession:
        existing = self.find_by_provider_session(provider, provider_session_id)
        parent = self.get(parent_session_id)
        if existing is not None:
            merged = {**existing.metadata, **(metadata or {})}
            return self.update(
                existing.session_id,
                state=_normalize_state(state),
                parent_session_id=parent_session_id,
                root_session_id=parent.root_session_id or parent.session_id,
                metadata=merged,
            )
        now = _utc_now()
        session = AgentSession(
            session_id=f"agent-{uuid4().hex}",
            provider=provider,
            provider_session_id=provider_session_id,
            workspace=parent.workspace,
            state=_normalize_state(state),
            model=model or parent.model,
            reasoning=parent.reasoning,
            access=parent.access,
            parent_session_id=parent_session_id,
            root_session_id=parent.root_session_id or parent.session_id,
            role=role,
            objective="Provider-native child agent",
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        self._write_session(session)
        self.record_event(
            AgentEvent(
                session_id=parent_session_id,
                kind="child_spawned",
                data={
                    "child_session_id": session.session_id,
                    "provider_session_id": provider_session_id,
                    "role": role,
                },
                provider_event="child",
            )
        )
        return session

    def list(
        self,
        *,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        directory = self.root / "agent-sessions"
        rows: list[AgentSession] = []
        if directory.exists():
            for path in directory.glob("*.json"):
                data = self._read_json(path)
                if not isinstance(data, dict):
                    continue
                session = self._from_dict(data)
                if parent_session_id is not None and session.parent_session_id != parent_session_id:
                    continue
                if root_session_id is not None and session.root_session_id != root_session_id:
                    continue
                rows.append(session)
        rows.sort(key=lambda item: item.updated_at or "", reverse=True)
        return [item.to_dict() for item in rows]

    def children(self, session_id: str) -> list[dict[str, Any]]:
        self.get(session_id)
        return self.list(parent_session_id=session_id)

    def record_event(self, event: AgentEvent) -> dict[str, Any]:
        session = self.get(event.session_id)
        sequence = self._next_sequence(event.session_id)
        at = event.at or _utc_now()
        payload = {
            **event.to_dict(),
            "sequence": sequence,
            "at": at,
        }
        self._append_jsonl(self._events_path(event.session_id), payload)
        self._write_sequence(event.session_id, sequence)
        if session.state == "starting":
            self.update(event.session_id, state="running")
        return payload

    def events(
        self, session_id: str, *, after_sequence: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        self.get(session_id)
        if after_sequence < 0 or not 1 <= limit <= 200:
            raise ValueError("after_sequence must be non-negative and limit must be 1..200")
        events: list[dict[str, Any]] = []
        path = self._events_path(session_id)
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    if int(item.get("sequence") or 0) <= after_sequence:
                        continue
                    events.append(item)
                    if len(events) >= limit:
                        break
        return {
            "session_id": session_id,
            "events": events,
            "next_sequence": int(events[-1]["sequence"]) if events else after_sequence,
        }

    def add_message(
        self,
        session_id: str,
        *,
        direction: str,
        content: str,
        sender_session_id: str | None = None,
        recipient_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.get(session_id)
        message = AgentMessage(
            message_id=f"msg-{uuid4().hex}",
            session_id=session_id,
            direction=direction,  # type: ignore[arg-type]
            content=content,
            sender_session_id=sender_session_id,
            recipient_session_id=recipient_session_id,
            created_at=_utc_now(),
            metadata=dict(metadata or {}),
        ).to_dict()
        self._append_jsonl(self._messages_path(session_id), message)
        return message

    def messages(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self.get(session_id)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        rows: list[dict[str, Any]] = []
        path = self._messages_path(session_id)
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        rows.append(item)
        return rows[-limit:]

    def close(self, session_id: str) -> AgentSession:
        return self.update(session_id, state="closed")

    def _write_session(self, session: AgentSession) -> None:
        path = self._session_path(session.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temp.replace(path)

    @staticmethod
    def _from_dict(data: dict[str, Any]) -> AgentSession:
        return AgentSession(
            session_id=str(data["session_id"]),
            provider=str(data["provider"]),
            workspace=Path(str(data["workspace"])),
            state=_normalize_state(str(data.get("state") or "idle")),
            provider_session_id=_optional(data.get("provider_session_id")),
            task_id=_optional(data.get("task_id")),
            model=_optional(data.get("model")),
            reasoning=str(data.get("reasoning") or "high"),
            access=str(data.get("access") or "read_only"),
            parent_session_id=_optional(data.get("parent_session_id")),
            root_session_id=_optional(data.get("root_session_id")),
            role=str(data.get("role") or "worker"),
            objective=str(data.get("objective") or ""),
            created_at=_optional(data.get("created_at")),
            updated_at=_optional(data.get("updated_at")),
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        )

    def _session_path(self, session_id: str) -> Path:
        return self.root / "agent-sessions" / f"{session_id}.json"

    def _events_path(self, session_id: str) -> Path:
        return self.root / "agent-events" / f"{session_id}.jsonl"

    def _messages_path(self, session_id: str) -> Path:
        return self.root / "agent-messages" / f"{session_id}.jsonl"

    def _sequence_path(self, session_id: str) -> Path:
        return self.root / "agent-events" / f"{session_id}.seq"

    def _next_sequence(self, session_id: str) -> int:
        path = self._sequence_path(session_id)
        try:
            return int(path.read_text(encoding="utf-8")) + 1
        except (OSError, ValueError):
            return 1

    def _write_sequence(self, session_id: str, sequence: int) -> None:
        path = self._sequence_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(sequence), encoding="utf-8")

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


def _optional(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _normalize_state(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"starting", "running", "idle", "interrupted", "failed", "closed"}:
        return lowered
    if lowered in {"done", "success", "completed"}:
        return "idle"
    if lowered in {"error", "cancelled", "canceled"}:
        return "failed"
    return "running"
