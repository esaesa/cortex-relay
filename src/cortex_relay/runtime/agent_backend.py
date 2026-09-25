from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cortex_relay.core.agents import AgentEvent, AgentSession


@runtime_checkable
class AgentStoreBackend(Protocol):
    """Storage contract for durable direct-agent control state.

    Implementations may use local SQLite, PostgreSQL, or another transactional
    backend, but must preserve the same session identity, cursor ordering,
    topology, result, lease, health, and retention semantics.
    """

    def health(self) -> dict[str, Any]: ...

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
    ) -> AgentSession: ...

    def get(self, session_id: str) -> AgentSession: ...

    def find_by_provider_session(
        self,
        provider: str,
        provider_session_id: str,
    ) -> AgentSession | None: ...

    def update(self, session_id: str, **updates: Any) -> AgentSession: ...

    def merge_metadata(
        self,
        session_id: str,
        updates: dict[str, Any],
    ) -> AgentSession: ...

    def bind_provider_session(
        self,
        session_id: str,
        provider_session_id: str | None,
    ) -> AgentSession: ...

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
    ) -> AgentSession: ...

    def list(
        self,
        *,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
        workspace: Path | None = None,
        active_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def children(self, session_id: str) -> list[dict[str, Any]]: ...

    def record_event(self, event: AgentEvent) -> dict[str, Any]: ...

    def events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]: ...

    def recent_events(
        self,
        session_id: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...

    def add_message(
        self,
        session_id: str,
        *,
        direction: str,
        content: str,
        sender_session_id: str | None = None,
        recipient_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def message_page(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]: ...

    def messages(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def save_result(
        self,
        session_id: str,
        result: dict[str, Any],
    ) -> None: ...

    def result(self, session_id: str) -> dict[str, Any] | None: ...

    def acquire_lease(
        self,
        session_id: str,
        owner_id: str,
        *,
        ttl_seconds: float = 30.0,
        owner_pid: int | None = None,
    ) -> bool: ...

    def heartbeat(
        self,
        session_id: str,
        owner_id: str,
        *,
        ttl_seconds: float = 30.0,
    ) -> bool: ...

    def release_lease(
        self,
        session_id: str,
        owner_id: str,
    ) -> bool: ...

    def lease(self, session_id: str) -> dict[str, Any] | None: ...

    def reconcile_expired(
        self,
        *,
        workspace: Path | None = None,
        now: float | None = None,
    ) -> list[str]: ...

    def gc(
        self,
        *,
        workspace: Path | None = None,
        retention_days: int = 30,
        max_completed_roots: int = 1000,
        dry_run: bool = False,
    ) -> dict[str, Any]: ...

    def close(self, session_id: str) -> AgentSession: ...
