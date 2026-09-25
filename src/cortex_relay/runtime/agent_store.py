from __future__ import annotations

import json
import os
import sqlite3
import time

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar
from uuid import uuid4

from cortex_relay.core.agents import AgentEvent, AgentMessage, AgentSession
from cortex_relay.runtime.state_retry import (
    DEFAULT_STATE_RETRY_POLICY,
    StateRetryPolicy,
    retry_state_operation,
)


_ACTIVE_STATES = {"starting", "running"}
_TERMINAL_STATES = {"idle", "interrupted", "failed", "closed"}
_SCHEMA_VERSION = 1

# SQLite already waits out short locks through ``busy_timeout``; this budget
# only covers the residual cases (connection churn, WAL checkpoint contention)
# and stays deliberately small so a stuck writer never stalls a worker.
AGENT_STORE_RETRY_POLICY = StateRetryPolicy(
    max_attempts=3,
    initial_delay_seconds=0.05,
    max_delay_seconds=0.25,
    multiplier=2.0,
)

T = TypeVar("T")


def _retryable_transaction(operation_name: str | None = None):
    """Re-run an idempotent store operation on a fresh connection.

    Every wrapped operation either commits atomically or rolls back, and all
    identifiers are generated inside the operation, so replaying it after a
    transient SQLite error cannot duplicate work.
    """

    def decorate(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapped(self: Any, *args: Any, **kwargs: Any) -> T:
            return retry_state_operation(
                lambda: fn(self, *args, **kwargs),
                policy=getattr(self, "_retry_policy", DEFAULT_STATE_RETRY_POLICY),
                operation_name=operation_name or f"agent-store:{fn.__name__}",
                sleep=getattr(self, "_sleeper", time.sleep),
            )

        return wrapped

    return decorate


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentStore:
    """Transactional provider-neutral agent/session state.

    The public surface intentionally matches the original file-backed store, but
    persistence is now SQLite/WAL-backed so session lookup, event/message cursors,
    child discovery, and result publication are indexed and atomic across threads
    and processes.
    """

    def __init__(
        self,
        root: Path,
        *,
        retry_policy: StateRetryPolicy = AGENT_STORE_RETRY_POLICY,
        sleeper: Callable[[float], None] = time.sleep,
        fault_injector: Callable[[str, Any], None] | None = None,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "agent-state.sqlite3"
        self._retry_policy = retry_policy
        self._sleeper = sleeper
        self._fault_injector = fault_injector
        self._setup()

    def _inject_fault(self, point: str, payload: Any) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point, payload)

    def _open_connection(self) -> sqlite3.Connection:
        def open_connection() -> sqlite3.Connection:
            conn = sqlite3.connect(
                self.db_path,
                timeout=10.0,
                isolation_level=None,
                check_same_thread=False,
            )
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=10000")
            except Exception:
                conn.close()
                raise
            return conn

        return retry_state_operation(
            open_connection,
            policy=self._retry_policy,
            operation_name="agent-store:connect",
            sleep=self._sleeper,
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._open_connection()
        try:
            yield conn
        finally:
            conn.close()

    def _begin(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        self._inject_fault("after_begin", conn)

    def _setup(self) -> None:
        retry_state_operation(
            self._apply_schema,
            policy=self._retry_policy,
            operation_name="agent-store:setup",
            sleep=self._sleeper,
        )

    def _apply_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    "agent state schema is newer than this CortexRelay build: "
                    f"{version} > {_SCHEMA_VERSION}"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    state TEXT NOT NULL,
                    provider_session_id TEXT,
                    task_id TEXT,
                    model TEXT,
                    reasoning TEXT NOT NULL,
                    access TEXT NOT NULL,
                    parent_session_id TEXT,
                    root_session_id TEXT,
                    role TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    created_at TEXT,
                    updated_at TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_provider_session
                ON agent_sessions(provider, provider_session_id)
                WHERE provider_session_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_agent_workspace_state_updated
                ON agent_sessions(workspace, state, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_agent_parent
                ON agent_sessions(parent_session_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_agent_root
                ON agent_sessions(root_session_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_agent_task
                ON agent_sessions(task_id);

                CREATE TABLE IF NOT EXISTS agent_events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    data TEXT NOT NULL,
                    provider_event TEXT,
                    at TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence),
                    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agent_events_cursor
                ON agent_events(session_id, sequence);

                CREATE TABLE IF NOT EXISTS agent_messages (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    direction TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sender_session_id TEXT,
                    recipient_session_id TEXT,
                    created_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (session_id, sequence),
                    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agent_messages_cursor
                ON agent_messages(session_id, sequence);

                CREATE TABLE IF NOT EXISTS agent_results (
                    session_id TEXT PRIMARY KEY,
                    result TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_leases (
                    session_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    owner_pid INTEGER,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agent_leases_expiry
                ON agent_leases(expires_at);
                """
            )
            # Version 0 is the unversioned schema shipped before migrations were
            # introduced. The CREATE IF NOT EXISTS statements above make that
            # schema compatible with v1, so the first open performs an in-place
            # migration by stamping the durable SQLite user_version.
            if version == 0:
                conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def schema_version(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def integrity_check(self) -> tuple[str, ...]:
        """Return SQLite quick-check diagnostics; ("ok",) means healthy."""
        with self._connect() as conn:
            rows = conn.execute("PRAGMA quick_check").fetchall()
        return tuple(str(row[0]) for row in rows)

    def health(self) -> dict[str, Any]:
        integrity = self.integrity_check()
        return {
            "db_path": str(self.db_path),
            "schema_version": self.schema_version(),
            "expected_schema_version": _SCHEMA_VERSION,
            "integrity": list(integrity),
            "ok": integrity == ("ok",) and self.schema_version() == _SCHEMA_VERSION,
        }

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
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO agent_sessions (
                        session_id, provider, workspace, state,
                        provider_session_id, task_id, model, reasoning, access,
                        parent_session_id, root_session_id, role, objective,
                        created_at, updated_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._session_values(session),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"could not create agent session {session_id}: {exc}") from exc
        return session

    def get(self, session_id: str) -> AgentSession:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown agent session: {session_id}")
        return self._from_row(row)

    def find_by_provider_session(
        self, provider: str, provider_session_id: str
    ) -> AgentSession | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_sessions
                WHERE provider = ? AND provider_session_id = ?
                LIMIT 1
                """,
                (provider, provider_session_id),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    @_retryable_transaction()
    def update(self, session_id: str, **updates: Any) -> AgentSession:
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
        if "state" in updates:
            updates["state"] = _normalize_state(str(updates["state"]))
        if "metadata" in updates:
            updates["metadata"] = json.dumps(
                updates["metadata"] if isinstance(updates["metadata"], dict) else {},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        updates["updated_at"] = _utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        values = [*updates.values(), session_id]
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    f"UPDATE agent_sessions SET {assignments} WHERE session_id = ?",
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"could not update agent session {session_id}: {exc}") from exc
            if cur.rowcount != 1:
                raise ValueError(f"unknown agent session: {session_id}")
        return self.get(session_id)

    @_retryable_transaction()
    def merge_metadata(
        self,
        session_id: str,
        updates: dict[str, Any],
    ) -> AgentSession:
        """Atomically merge metadata without clobbering concurrent fields."""
        with self._connect() as conn:
            self._begin(conn)
            try:
                row = conn.execute(
                    "SELECT metadata FROM agent_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown agent session: {session_id}")
                metadata = _loads(row["metadata"], {})
                if not isinstance(metadata, dict):
                    metadata = {}
                merged = {**metadata, **updates}
                conn.execute(
                    """
                    UPDATE agent_sessions
                    SET metadata = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (
                        json.dumps(
                            merged,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        _utc_now(),
                        session_id,
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return self.get(session_id)

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
        parent = self.get(parent_session_id)
        existing = self.find_by_provider_session(provider, provider_session_id)
        if existing is not None:
            merged = {**existing.metadata, **(metadata or {})}
            return self.update(
                existing.session_id,
                state=_normalize_state(state),
                parent_session_id=parent_session_id,
                root_session_id=parent.root_session_id or parent.session_id,
                role=role or existing.role,
                model=model or existing.model,
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
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO agent_sessions (
                        session_id, provider, workspace, state,
                        provider_session_id, task_id, model, reasoning, access,
                        parent_session_id, root_session_id, role, objective,
                        created_at, updated_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._session_values(session),
                )
            except sqlite3.IntegrityError:
                raced = self.find_by_provider_session(provider, provider_session_id)
                if raced is not None:
                    return self.update(
                        raced.session_id,
                        state=_normalize_state(state),
                        parent_session_id=parent_session_id,
                        root_session_id=parent.root_session_id or parent.session_id,
                        role=role or raced.role,
                        model=model or raced.model,
                        metadata={**raced.metadata, **(metadata or {})},
                    )
                raise

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
        workspace: Path | None = None,
        active_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if parent_session_id is not None:
            clauses.append("parent_session_id = ?")
            params.append(parent_session_id)
        if root_session_id is not None:
            clauses.append("root_session_id = ?")
            params.append(root_session_id)
        if workspace is not None:
            clauses.append("workspace = ?")
            params.append(str(workspace.expanduser().resolve()))
        if active_only:
            clauses.append("state IN ('starting', 'running')")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM agent_sessions{where} ORDER BY updated_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._from_row(row).to_dict() for row in rows]

    def children(self, session_id: str) -> list[dict[str, Any]]:
        self.get(session_id)
        return self.list(parent_session_id=session_id)

    @_retryable_transaction()
    def record_event(self, event: AgentEvent) -> dict[str, Any]:
        at = event.at or _utc_now()
        with self._connect() as conn:
            self._begin(conn)
            try:
                exists = conn.execute(
                    "SELECT state FROM agent_sessions WHERE session_id = ?",
                    (event.session_id,),
                ).fetchone()
                if exists is None:
                    raise ValueError(f"unknown agent session: {event.session_id}")
                row = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                    "FROM agent_events WHERE session_id = ?",
                    (event.session_id,),
                ).fetchone()
                sequence = int(row["next_sequence"])
                conn.execute(
                    """
                    INSERT INTO agent_events (
                        session_id, sequence, kind, data, provider_event, at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.session_id,
                        sequence,
                        event.kind,
                        json.dumps(
                            event.data,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        event.provider_event,
                        at,
                    ),
                )
                conn.execute(
                    """
                    UPDATE agent_sessions
                    SET state = CASE WHEN state = 'starting' THEN 'running' ELSE state END,
                        updated_at = ?
                    WHERE session_id = ?
                    """,
                    (at, event.session_id),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return {
            **event.to_dict(),
            "sequence": sequence,
            "at": at,
        }

    def events(
        self, session_id: str, *, after_sequence: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        self.get(session_id)
        if after_sequence < 0 or not 1 <= limit <= 200:
            raise ValueError("after_sequence must be non-negative and limit must be 1..200")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sequence, kind, data, provider_event, at
                FROM agent_events
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (session_id, after_sequence, limit),
            ).fetchall()
        events = [self._event_row(session_id, row) for row in rows]
        return {
            "session_id": session_id,
            "events": events,
            "next_sequence": int(events[-1]["sequence"]) if events else after_sequence,
        }

    def recent_events(
        self,
        session_id: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        self.get(session_id)
        if not 1 <= limit <= 50:
            raise ValueError("limit must be 1..50")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sequence, kind, data, provider_event, at
                FROM agent_events
                WHERE session_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            self._event_row(session_id, row)
            for row in reversed(rows)
        ]

    @_retryable_transaction()
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
        created_at = _utc_now()
        message_id = f"msg-{uuid4().hex}"
        with self._connect() as conn:
            self._begin(conn)
            try:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                    "FROM agent_messages WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                sequence = int(row["next_sequence"])
                conn.execute(
                    """
                    INSERT INTO agent_messages (
                        session_id, sequence, message_id, direction, content,
                        sender_session_id, recipient_session_id, created_at, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        sequence,
                        message_id,
                        direction,
                        content,
                        sender_session_id,
                        recipient_session_id,
                        created_at,
                        json.dumps(
                            metadata or {},
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        return {
            **AgentMessage(
                message_id=message_id,
                session_id=session_id,
                direction=direction,  # type: ignore[arg-type]
                content=content,
                sender_session_id=sender_session_id,
                recipient_session_id=recipient_session_id,
                created_at=created_at,
                metadata=dict(metadata or {}),
            ).to_dict(),
            "sequence": sequence,
        }

    def message_page(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        self.get(session_id)
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("after_sequence must be non-negative and limit must be 1..500")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sequence, message_id, direction, content,
                       sender_session_id, recipient_session_id, created_at, metadata
                FROM agent_messages
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (session_id, after_sequence, limit),
            ).fetchall()
        messages = [self._message_row(session_id, row) for row in rows]
        return {
            "session_id": session_id,
            "messages": messages,
            "next_sequence": int(messages[-1]["sequence"]) if messages else after_sequence,
        }

    def messages(self, session_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        self.get(session_id)
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1..500")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sequence, message_id, direction, content,
                       sender_session_id, recipient_session_id, created_at, metadata
                FROM agent_messages
                WHERE session_id = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [
            self._message_row(session_id, row)
            for row in reversed(rows)
        ]

    @_retryable_transaction()
    def save_result(self, session_id: str, result: dict[str, Any]) -> None:
        self.get(session_id)
        payload = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_results(session_id, result, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    result = excluded.result,
                    updated_at = excluded.updated_at
                """,
                (session_id, payload, _utc_now()),
            )

    def result(self, session_id: str) -> dict[str, Any] | None:
        self.get(session_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result FROM agent_results WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        value = _loads(row["result"], {})
        return value if isinstance(value, dict) else None

    @_retryable_transaction()
    def acquire_lease(
        self,
        session_id: str,
        owner_id: str,
        *,
        ttl_seconds: float = 30.0,
        owner_pid: int | None = None,
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.get(session_id)
        now = time.time()
        expires_at = now + ttl_seconds
        with self._connect() as conn:
            self._begin(conn)
            try:
                row = conn.execute(
                    "SELECT owner_id, expires_at FROM agent_leases WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if (
                    row is not None
                    and float(row["expires_at"]) > now
                    and str(row["owner_id"]) != owner_id
                ):
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    """
                    INSERT INTO agent_leases(
                        session_id, owner_id, owner_pid, heartbeat_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        owner_pid = excluded.owner_pid,
                        heartbeat_at = excluded.heartbeat_at,
                        expires_at = excluded.expires_at
                    """,
                    (
                        session_id,
                        owner_id,
                        owner_pid if owner_pid is not None else os.getpid(),
                        now,
                        expires_at,
                    ),
                )
                conn.execute("COMMIT")
                return True
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @_retryable_transaction()
    def heartbeat(
        self,
        session_id: str,
        owner_id: str,
        *,
        ttl_seconds: float = 30.0,
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE agent_leases
                SET heartbeat_at = ?, expires_at = ?
                WHERE session_id = ? AND owner_id = ?
                """,
                (now, now + ttl_seconds, session_id, owner_id),
            )
        return cur.rowcount == 1

    def release_lease(self, session_id: str, owner_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM agent_leases WHERE session_id = ? AND owner_id = ?",
                (session_id, owner_id),
            )
        return cur.rowcount == 1

    def lease(self, session_id: str) -> dict[str, Any] | None:
        self.get(session_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT owner_id, owner_pid, heartbeat_at, expires_at
                FROM agent_leases WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    @_retryable_transaction()
    def reconcile_expired(
        self,
        *,
        workspace: Path | None = None,
        now: float | None = None,
    ) -> list[str]:
        current = time.time() if now is None else now
        clauses = [
            "s.state IN ('starting', 'running')",
            "l.expires_at <= ?",
        ]
        params: list[Any] = [current]
        if workspace is not None:
            clauses.append("s.workspace = ?")
            params.append(str(workspace.expanduser().resolve()))
        where = " AND ".join(clauses)

        with self._connect() as conn:
            self._begin(conn)
            try:
                rows = conn.execute(
                    f"""
                    SELECT s.session_id, s.metadata
                    FROM agent_sessions s
                    JOIN agent_leases l ON l.session_id = s.session_id
                    WHERE {where}
                    """,
                    params,
                ).fetchall()
                ids: list[str] = []
                stamp = _utc_now()
                for row in rows:
                    session_id = str(row["session_id"])
                    metadata = _loads(row["metadata"], {})
                    if not isinstance(metadata, dict):
                        metadata = {}
                    metadata = {
                        **metadata,
                        "interrupted_reason": "agent lease expired",
                        "termination_reason": "lease_expired",
                        "interrupted_at": stamp,
                    }
                    conn.execute(
                        """
                        UPDATE agent_sessions
                        SET state = 'interrupted', updated_at = ?, metadata = ?
                        WHERE session_id = ?
                        """,
                        (
                            stamp,
                            json.dumps(
                                metadata,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            session_id,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM agent_leases WHERE session_id = ?",
                        (session_id,),
                    )
                    ids.append(session_id)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

        for session_id in ids:
            try:
                self.record_event(
                    AgentEvent(
                        session_id=session_id,
                        kind="diagnostic",
                        data={"error": "Agent owner lease expired; session interrupted."},
                        provider_event="lease_expired",
                    )
                )
            except (sqlite3.Error, ValueError):
                pass
        return ids

    def gc(
        self,
        *,
        workspace: Path | None = None,
        retention_days: int = 30,
        max_completed_roots: int = 1000,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Prune terminal agent trees while preserving active lineage.

        Sessions are retained/deleted by root tree rather than individually so
        parent/child topology never becomes partially orphaned.
        """
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        if max_completed_roots < 0:
            raise ValueError("max_completed_roots must be non-negative")

        clauses: list[str] = []
        params: list[Any] = []
        if workspace is not None:
            clauses.append("workspace = ?")
            params.append(str(workspace.expanduser().resolve()))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, root_session_id, workspace, state, "
                f"created_at, updated_at FROM agent_sessions{where}",
                params,
            ).fetchall()

        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            root_id = str(row["root_session_id"] or row["session_id"])
            groups.setdefault(root_id, []).append(row)

        candidates: list[tuple[float, str, list[sqlite3.Row]]] = []
        for root_id, members in groups.items():
            if any(str(member["state"]) not in _TERMINAL_STATES for member in members):
                continue
            stamp = max(
                _iso_timestamp(member["updated_at"] or member["created_at"])
                for member in members
            )
            candidates.append((stamp, root_id, members))

        candidates.sort(key=lambda item: item[0], reverse=True)
        keep_roots = {
            root_id
            for _, root_id, _ in candidates[:max_completed_roots]
        }
        cutoff = time.time() - retention_days * 86400

        planned: list[dict[str, Any]] = []
        for stamp, root_id, members in candidates:
            expired = stamp < cutoff
            over_count = root_id not in keep_roots
            if not (expired or over_count):
                continue
            session_ids = [str(member["session_id"]) for member in members]
            entry = {
                "root_session_id": root_id,
                "workspace": str(members[0]["workspace"]),
                "sessions": session_ids,
                "expired": expired,
                "over_count": over_count,
            }
            planned.append(entry)
            if dry_run:
                continue

            placeholders = ",".join("?" for _ in session_ids)
            with self._connect() as conn:
                self._begin(conn)
                try:
                    conn.execute(
                        f"DELETE FROM agent_sessions WHERE session_id IN ({placeholders})",
                        session_ids,
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise

        return {
            "dry_run": dry_run,
            "workspace": (
                str(workspace.expanduser().resolve())
                if workspace is not None else None
            ),
            "retention_days": retention_days,
            "max_completed_roots": max_completed_roots,
            "count": len(planned),
            "agents": planned,
        }

    def close(self, session_id: str) -> AgentSession:
        session = self.get(session_id)
        if session.state in _ACTIVE_STATES:
            raise ValueError(
                f"agent session {session_id} is {session.state}; wait for it to become idle "
                "or cancel the active turn before closing"
            )
        return self.update(session_id, state="closed")

    @staticmethod
    def _session_values(session: AgentSession) -> tuple[Any, ...]:
        return (
            session.session_id,
            session.provider,
            str(session.workspace.expanduser().resolve()),
            session.state,
            session.provider_session_id,
            session.task_id,
            session.model,
            session.reasoning,
            session.access,
            session.parent_session_id,
            session.root_session_id,
            session.role,
            session.objective,
            session.created_at,
            session.updated_at,
            json.dumps(
                session.metadata,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AgentSession:
        metadata = _loads(row["metadata"], {})
        return AgentSession(
            session_id=str(row["session_id"]),
            provider=str(row["provider"]),
            workspace=Path(str(row["workspace"])),
            state=_normalize_state(str(row["state"])),
            provider_session_id=_optional(row["provider_session_id"]),
            task_id=_optional(row["task_id"]),
            model=_optional(row["model"]),
            reasoning=str(row["reasoning"] or "high"),
            access=str(row["access"] or "read_only"),
            parent_session_id=_optional(row["parent_session_id"]),
            root_session_id=_optional(row["root_session_id"]),
            role=str(row["role"] or "worker"),
            objective=str(row["objective"] or ""),
            created_at=_optional(row["created_at"]),
            updated_at=_optional(row["updated_at"]),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    @staticmethod
    def _event_row(session_id: str, row: sqlite3.Row) -> dict[str, Any]:
        data = _loads(row["data"], {})
        return {
            "session_id": session_id,
            "kind": str(row["kind"]),
            "data": data if isinstance(data, dict) else {},
            "provider_event": _optional(row["provider_event"]),
            "sequence": int(row["sequence"]),
            "at": str(row["at"]),
        }

    @staticmethod
    def _message_row(session_id: str, row: sqlite3.Row) -> dict[str, Any]:
        metadata = _loads(row["metadata"], {})
        return {
            "message_id": str(row["message_id"]),
            "session_id": session_id,
            "direction": str(row["direction"]),
            "content": str(row["content"]),
            "sender_session_id": _optional(row["sender_session_id"]),
            "recipient_session_id": _optional(row["recipient_session_id"]),
            "created_at": str(row["created_at"]),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "sequence": int(row["sequence"]),
        }


def _iso_timestamp(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _loads(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


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
    if lowered in {"cancelled", "canceled"}:
        return "interrupted"
    if lowered == "error":
        return "failed"
    return "running"
