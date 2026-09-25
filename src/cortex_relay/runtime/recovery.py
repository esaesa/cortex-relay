"""Bounded, observable recovery from provider print timeouts.

When a provider's print/idle timeout cuts a turn off mid-work, CortexRelay may
resume the *same* provider session once, twice, ... up to a policy limit while
runtime budget remains. Recovery is never allowed to change provider, never
applies to terminal outcomes (cancelled, budget, owner loss), and every decision
is explicit so a resumed turn can never masquerade as the original one.
"""

from __future__ import annotations

import time

from dataclasses import dataclass, field
from typing import Any

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.providers.base import ProviderCapabilities

RECOVERY_CONTINUATION_PROMPT = (
    "Your previous turn was interrupted by the provider print timeout before it "
    "finished. Continue the original objective from where you stopped: do not "
    "restart completed work, do not repeat finished analysis, and do not ask for "
    "instructions. If the work is already complete, report the final result now."
)

# Outcomes that must never be resumed, whatever the provider claims.
NON_RECOVERABLE_TERMINATION_REASONS = frozenset({
    "cancelled",
    "budget_exceeded",
    "supervisor_budget",
    "token_budget",
    "cost_budget",
    "owner_lost",
    "lease_expired",
    "worker_lost",
    "execution_timeout",
    "idle_timeout",
    "provider_exception",
})

DEFAULT_RECOVERABLE_TERMINATION_REASONS = frozenset({"provider_print_timeout"})

# Recovery attempts keep almost all of the remaining runtime for actual work.
RECOVERY_PRINT_TIMEOUT_RESERVE_SECONDS = 5.0
MIN_PRINT_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class RecoveryPolicy:
    """Limits on how often one interrupted session may be resumed."""

    max_attempts: int = 3
    min_remaining_seconds: float = 15.0
    recoverable_termination_reasons: frozenset[str] = field(
        default_factory=lambda: DEFAULT_RECOVERABLE_TERMINATION_REASONS
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 0:
            raise ValueError("max_attempts must be non-negative")
        if self.min_remaining_seconds < 0:
            raise ValueError("min_remaining_seconds must be non-negative")


DEFAULT_RECOVERY_POLICY = RecoveryPolicy()


@dataclass(frozen=True)
class RecoveryDecision:
    """One explicit, loggable answer to "should this session be resumed?"."""

    resume: bool
    reason: str
    attempt: int
    next_attempt: int
    prompt: str | None
    remaining_runtime_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "resume": self.resume,
            "reason": self.reason,
            "attempt": self.attempt,
            "next_attempt": self.next_attempt,
            "remaining_runtime_seconds": round(self.remaining_runtime_seconds, 3),
        }


def recovery_attempt(task: TaskSpec) -> int:
    """Return the completed recovery-attempt count carried by ``task``."""
    value = task.metadata.get("_recovery_attempt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def stamp_logical_deadline(
    metadata: dict[str, Any],
    timeout_seconds: float,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Stamp a monotonic cumulative deadline once, preserving an outer one.

    The deadline is the single authority for how much runtime a task may still
    consume across retries and recovery attempts; it is ``_``-prefixed so it
    never leaks into serialized task metadata.
    """
    existing = metadata.get("_logical_deadline")
    if isinstance(existing, bool) or not isinstance(existing, (int, float)):
        current = time.monotonic() if now is None else now
        metadata["_logical_deadline"] = current + max(0.0, float(timeout_seconds))
    return metadata


def build_recovery_prompt(task: TaskSpec) -> str:
    """Continuation prompt that keeps the original objective authoritative."""
    objective = str(task.objective or "").strip()
    if not objective:
        return RECOVERY_CONTINUATION_PROMPT
    return f"{RECOVERY_CONTINUATION_PROMPT}\n\nOriginal objective (authoritative):\n{objective}"


def remaining_runtime_seconds(task: TaskSpec, *, now: float | None = None) -> float:
    """Runtime left under the cumulative logical deadline, else the turn timeout."""
    deadline = task.metadata.get("_logical_deadline")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return float(task.timeout_seconds)
    current = time.monotonic() if now is None else now
    return max(0.0, float(deadline) - current)


def print_timeout_reserve_seconds(
    timeout_seconds: float,
    *,
    attempt: int = 0,
    policy: RecoveryPolicy = DEFAULT_RECOVERY_POLICY,
) -> float:
    """Reserve left unused by the provider's own print timeout.

    Normal turns keep 20% of the timeout (clamped to 30..90s) so recovery has
    runtime to resume into. Recovery turns keep almost everything because they
    are already inside that budget.
    """
    del policy
    timeout_seconds = max(0.0, float(timeout_seconds))
    if attempt > 0:
        return min(RECOVERY_PRINT_TIMEOUT_RESERVE_SECONDS, max(0.0, timeout_seconds - 1.0))
    reserve = min(max(timeout_seconds * 0.20, 30.0), 90.0)
    return min(reserve, max(0.0, timeout_seconds - 1.0))


def recovery_decision(
    task: TaskSpec,
    result: TaskResult,
    *,
    provider_capabilities: ProviderCapabilities,
    remaining_runtime_seconds: float,
    policy: RecoveryPolicy = DEFAULT_RECOVERY_POLICY,
    attempt: int | None = None,
) -> RecoveryDecision:
    """Decide whether an interrupted session may be resumed, and say why."""
    completed = recovery_attempt(task) if attempt is None else max(0, int(attempt))
    remaining = max(0.0, float(remaining_runtime_seconds))

    def refuse(reason: str) -> RecoveryDecision:
        return RecoveryDecision(
            resume=False,
            reason=reason,
            attempt=completed,
            next_attempt=completed,
            prompt=None,
            remaining_runtime_seconds=remaining,
        )

    if result.status in {"cancelled", "budget_exceeded"}:
        return refuse(f"terminal_status:{result.status}")
    termination = str(result.termination_reason or "")
    if termination in NON_RECOVERABLE_TERMINATION_REASONS:
        return refuse(f"non_recoverable_termination:{termination}")
    if termination not in policy.recoverable_termination_reasons:
        return refuse("not_a_recoverable_interruption")
    if result.status != "timeout":
        return refuse("not_a_timeout_result")
    if not provider_capabilities.persistent_sessions:
        return refuse("provider_has_no_resumable_session")
    if provider_capabilities.session_mode not in {"resumable", "native"}:
        return refuse(f"session_mode:{provider_capabilities.session_mode}")
    if completed >= policy.max_attempts:
        return refuse("attempt_budget_exhausted")
    if remaining < policy.min_remaining_seconds:
        return refuse("insufficient_remaining_runtime")

    return RecoveryDecision(
        resume=True,
        reason="resumable_print_timeout",
        attempt=completed,
        next_attempt=completed + 1,
        prompt=build_recovery_prompt(task),
        remaining_runtime_seconds=remaining,
    )
