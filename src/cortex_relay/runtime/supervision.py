from __future__ import annotations

from dataclasses import dataclass
from cortex_relay.core.models import TaskBudget
from cortex_relay.runtime.progress import ProgressEvent


@dataclass(frozen=True)
class SupervisionViolation:
    reason: str
    activity: str
    termination_reason: str


class SupervisionTracker:
    """Provider-neutral semantic budget enforcement for one worker turn."""

    def __init__(self, budget: TaskBudget) -> None:
        self.budget = budget
        self.tool_calls = 0
        self.last_tool_fingerprint: tuple[str, str, str] | None = None
        self.repeated_calls = 0
        self.child_ids: set[str] = set()

    def observe(self, event: ProgressEvent) -> SupervisionViolation | None:
        violation = self._observe_tool(event)
        if violation is not None:
            return violation

        violation = self._observe_children(event)
        if violation is not None:
            return violation

        return self._observe_tokens(event)

    def _observe_tool(self, event: ProgressEvent) -> SupervisionViolation | None:
        if event.phase != "tool" or event.state != "active":
            return None

        self.tool_calls += 1
        fingerprint = (
            str(event.tool or ""),
            str(event.command or ""),
            str(event.path or ""),
        )
        if fingerprint == self.last_tool_fingerprint:
            self.repeated_calls += 1
        else:
            self.last_tool_fingerprint = fingerprint
            self.repeated_calls = 1

        if (
            self.budget.max_tool_calls is not None
            and self.tool_calls > self.budget.max_tool_calls
        ):
            return SupervisionViolation(
                reason=(
                    f"tool-call budget exceeded: "
                    f"{self.tool_calls} > {self.budget.max_tool_calls}"
                ),
                activity="Tool-call budget exceeded; cancellation requested",
                termination_reason="tool_call_budget",
            )

        if (
            self.budget.max_repeated_calls is not None
            and self.repeated_calls > self.budget.max_repeated_calls
        ):
            return SupervisionViolation(
                reason=(
                    "repeated tool-call budget exceeded: "
                    f"{self.repeated_calls} > {self.budget.max_repeated_calls}; "
                    f"fingerprint={fingerprint!r}"
                ),
                activity="Repeated tool-call stall detected; cancellation requested",
                termination_reason="repeated_tool_stall",
            )
        return None

    def _observe_children(self, event: ProgressEvent) -> SupervisionViolation | None:
        for child in event.subagents:
            if not isinstance(child, dict):
                continue
            child_id = (
                child.get("provider_session_id")
                or child.get("session_id")
                or child.get("id")
            )
            if child_id:
                self.child_ids.add(str(child_id))

        if (
            self.budget.max_child_agents is not None
            and len(self.child_ids) > self.budget.max_child_agents
        ):
            return SupervisionViolation(
                reason=(
                    f"child-agent budget exceeded: "
                    f"{len(self.child_ids)} > {self.budget.max_child_agents}"
                ),
                activity="Child-agent budget exceeded; cancellation requested",
                termination_reason="child_agent_budget",
            )
        return None

    def _observe_tokens(self, event: ProgressEvent) -> SupervisionViolation | None:
        limit = self.budget.max_tokens
        if limit is None:
            return None
        observed = event.total_tokens
        if observed is None and (
            event.input_tokens is not None or event.output_tokens is not None
        ):
            observed = (event.input_tokens or 0) + (event.output_tokens or 0)
        if observed is not None and observed > limit:
            return SupervisionViolation(
                reason=f"token budget exceeded: {observed} > {limit}",
                activity="Token budget exceeded; cancellation requested",
                termination_reason="token_budget",
            )
        return None
