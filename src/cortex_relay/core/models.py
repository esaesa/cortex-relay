from __future__ import annotations

import re

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


TaskAccess = Literal["read_only", "workspace_write"]
TaskStatus = Literal[
    "success", "error", "timeout", "unavailable", "cancelled",
    "blocked", "interrupted", "failed_gate", "budget_exceeded",
]
ReasoningLevel = str

_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

@dataclass(frozen=True)
class DelegationContext:
    trace_id: str
    parent_task_id: str | None = None
    root_task_id: str | None = None
    depth: int = 0
    max_depth: int = 8
    ancestry: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _CONTEXT_ID_RE.fullmatch(self.trace_id):
            raise ValueError("trace_id must be a bounded token")
        for value, label in (
            (self.parent_task_id, "parent_task_id"),
            (self.root_task_id, "root_task_id"),
        ):
            if value is not None and not _CONTEXT_ID_RE.fullmatch(value):
                raise ValueError(f"{label} must be a bounded token")
        if self.depth < 0 or self.max_depth < 1 or self.depth > self.max_depth:
            raise ValueError("delegation depth exceeds max_depth")
        if len(self.ancestry) != len(set(self.ancestry)):
            raise ValueError("delegation ancestry contains a loop")
        if self.parent_task_id and self.parent_task_id in self.ancestry[:-1]:
            raise ValueError("parent_task_id already appears earlier in ancestry")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "parent_task_id": self.parent_task_id,
            "root_task_id": self.root_task_id,
            "depth": self.depth,
            "max_depth": self.max_depth,
            "ancestry": list(self.ancestry),
        }


@dataclass(frozen=True)
class TaskBudget:
    max_tokens: int | None = None
    max_cost: float | None = None

    def __post_init__(self) -> None:
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if self.max_cost is not None and self.max_cost < 0:
            raise ValueError("max_cost must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"max_tokens": self.max_tokens, "max_cost": self.max_cost}


@dataclass(frozen=True)
class QualityGates:
    require_changed_files: bool = False
    require_tests: bool = False
    require_review: bool = False
    allowed_paths: tuple[str, ...] = ()
    max_failed_tests: int | None = None

    def __post_init__(self) -> None:
        if self.max_failed_tests is not None and self.max_failed_tests < 0:
            raise ValueError("max_failed_tests must be non-negative")
        object.__setattr__(
            self,
            "allowed_paths",
            tuple(path.strip().replace("\\", "/") for path in self.allowed_paths if path.strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_changed_files": self.require_changed_files,
            "require_tests": self.require_tests,
            "require_review": self.require_review,
            "allowed_paths": list(self.allowed_paths),
            "max_failed_tests": self.max_failed_tests,
        }

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
    context: DelegationContext | None = None
    budget: TaskBudget = field(default_factory=TaskBudget)
    quality_gates: QualityGates = field(default_factory=QualityGates)
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
            "context": self.context.to_dict() if self.context else None,
            "budget": self.budget.to_dict(),
            "quality_gates": self.quality_gates.to_dict(),
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
