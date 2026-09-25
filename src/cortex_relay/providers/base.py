from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

from cortex_relay.core.models import TaskResult, TaskSpec


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    binary: str
    available: bool
    structured_output: bool
    model_selection: bool
    reasoning_control: bool
    read_only_policy: bool
    workspace_write: bool
    detail: str = ""
    known_models: tuple[str, ...] = ()
    reasoning_levels: tuple[str, ...] = ()
    session_mode: str = "closed_end"
    persistent_sessions: bool = False
    streaming_events: bool = False
    native_subagents: bool = False
    child_messaging: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def execute(self, task: TaskSpec) -> TaskResult:
        raise NotImplementedError

    def execute_session(self, task: TaskSpec) -> TaskResult:
        """Execute one turn using the provider's preferred session transport."""
        return self.execute(task)

    def continue_session(self, task: TaskSpec, provider_session_id: str) -> TaskResult:
        """Continue a provider-backed session.

        Closed-end providers intentionally do not implement this. Session-capable
        providers should reuse the provider's native thread/conversation/session
        handle instead of creating a fresh logical worker.
        """
        raise NotImplementedError(
            f"{self.name} does not support resumable provider sessions"
        )


def task_execution_constraints(task: TaskSpec) -> str:
    """Render machine-known execution limits into every delegated worker prompt."""
    budget = task.budget
    constraints = [
        f"- Workspace root: {task.workspace}",
        f"- Access mode: {task.access}",
        f"- Absolute turn timeout: {task.timeout_seconds}s",
    ]
    optional = (
        ("Maximum tool calls", budget.max_tool_calls),
        ("Maximum consecutive repeated tool calls", budget.max_repeated_calls),
        ("Maximum idle seconds without provider output", budget.max_idle_seconds),
        ("Maximum runtime seconds", budget.max_runtime_seconds),
        ("Maximum child agents", budget.max_child_agents),
        ("Maximum tokens", budget.max_tokens),
        ("Maximum cost", budget.max_cost),
    )
    constraints.extend(
        f"- {label}: {value}"
        for label, value in optional
        if value is not None
    )
    constraints.extend(
        [
            "- Use workspace-qualified paths; do not guess alternate repository roots.",
            "- Avoid broad recursive repository scans unless the objective requires them.",
            "- Stop once the acceptance criteria are satisfied; do not repeat equivalent reads or tool calls.",
        ]
    )
    return "Execution constraints:\n" + "\n".join(constraints)
