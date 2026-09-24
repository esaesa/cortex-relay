from __future__ import annotations

import re

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


TaskAccess = Literal["read_only", "workspace_write"]
TaskStatus = Literal["success", "error", "timeout", "unavailable", "cancelled"]
ReasoningLevel = str

_REASONING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class TaskSpec:
    """Provider-neutral description of one delegated task."""

    objective: str
    role: str = "reviewer"
    profile: str | None = None
    preset: str | None = None
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
        if not self.reasoning or not _REASONING_RE.fullmatch(self.reasoning):
            raise ValueError(
                "reasoning must be a non-empty token containing only letters, numbers, '.', '_' or '-'"
            )
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")

        object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        object.__setattr__(self, "reasoning", self.reasoning.lower())
        object.__setattr__(
            self,
            "profile",
            self.profile.strip()
            if isinstance(self.profile, str) and self.profile.strip()
            else None,
        )
        object.__setattr__(
            self,
            "preset",
            self.preset.strip()
            if isinstance(self.preset, str) and self.preset.strip()
            else None,
        )
        object.__setattr__(
            self,
            "acceptance_criteria",
            tuple(item.strip() for item in self.acceptance_criteria if item.strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "role": self.role,
            "profile": self.profile,
            "preset": self.preset,
            "provider": self.provider,
            "workspace": str(self.workspace),
            "access": self.access,
            "reasoning": self.reasoning,
            "model": self.model,
            "acceptance_criteria": list(self.acceptance_criteria),
            "timeout_seconds": self.timeout_seconds,
            "isolate_write": self.isolate_write,
            "metadata": {
                key: value
                for key, value in self.metadata.items()
                if not key.startswith("_")
            },
        }


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
