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
    session_protocol: str = "legacy_exec"

    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    @abstractmethod
    def execute(self, task: TaskSpec) -> TaskResult:
        raise NotImplementedError

    def continue_session(
        self,
        task: TaskSpec,
        provider_session_id: str,
        message: str,
    ) -> TaskResult:
        """Continue a provider-native session.

        Providers without a native continuation path intentionally remain
        closed-end fallbacks rather than pretending to be resumable.
        """
        raise NotImplementedError(
            f"{self.name} provider does not expose a resumable session protocol"
        )

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
