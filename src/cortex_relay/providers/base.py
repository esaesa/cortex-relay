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
