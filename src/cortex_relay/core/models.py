from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


TaskAccess = Literal["read_only", "workspace_write"]
TaskStatus = Literal["success", "error", "timeout", "unavailable"]
ReasoningLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class TaskSpec:
    """Provider-neutral description of one delegated task."""

    objective: str
    role: str = "reviewer"
    provider: str = "auto"
    workspace: Path = field(default_factory=Path.cwd)
    access: TaskAccess = "read_only"
    reasoning: ReasoningLevel = "high"
    model: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    timeout_seconds: int = 300
    isolate_write: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("objective must not be empty")
        if self.access not in {"read_only", "workspace_write"}:
            raise ValueError(f"unsupported access mode: {self.access}")
        if self.reasoning not in {"low", "medium", "high"}:
            raise ValueError(f"unsupported reasoning level: {self.reasoning}")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")

        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        object.__setattr__(
            self,
            "acceptance_criteria",
            tuple(item.strip() for item in self.acceptance_criteria if item.strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workspace"] = str(self.workspace)
        return data


@dataclass(frozen=True)
class Evidence:
    finding: str
    path: str | None = None
    symbol: str | None = None
    severity: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(
            finding=str(data.get("finding", "")),
            path=_optional_string(data.get("path")),
            symbol=_optional_string(data.get("symbol")),
            severity=_optional_string(data.get("severity")),
        )


@dataclass(frozen=True)
class TaskResult:
    """Normalized result returned by every runtime provider."""

    status: TaskStatus
    provider: str
    summary: str
    evidence: tuple[Evidence, ...] = ()
    changed_files: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    model: str | None = None
    conversation_id: str | None = None
    error: str | None = None
    duration_seconds: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "summary": self.summary,
            "evidence": [asdict(item) for item in self.evidence],
            "changed_files": list(self.changed_files),
            "commands": list(self.commands),
            "tests": list(self.tests),
            "risks": list(self.risks),
            "model": self.model,
            "conversation_id": self.conversation_id,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
            "usage": self.usage,
            "metadata": self.metadata,
        }


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
