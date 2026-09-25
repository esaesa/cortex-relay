from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


AgentState = Literal[
    "starting",
    "running",
    "idle",
    "interrupted",
    "failed",
    "closed",
]


@dataclass(frozen=True)
class AgentSession:
    """Provider-neutral handle for a live or resumable agent conversation."""

    session_id: str
    provider: str
    workspace: Path
    state: AgentState = "starting"
    provider_session_id: str | None = None
    task_id: str | None = None
    model: str | None = None
    reasoning: str = "high"
    access: str = "read_only"
    parent_session_id: str | None = None
    root_session_id: str | None = None
    role: str = "worker"
    objective: str = ""
    created_at: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workspace"] = str(self.workspace)
        return data


@dataclass(frozen=True)
class AgentEvent:
    """Durable semantic event emitted by a provider agent or one of its children."""

    session_id: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    provider_event: str | None = None
    sequence: int | None = None
    at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentMessage:
    """Message delivered between the host and a provider-backed agent session."""

    message_id: str
    session_id: str
    direction: Literal["host_to_agent", "agent_to_host", "agent_to_agent"]
    content: str
    sender_session_id: str | None = None
    recipient_session_id: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
