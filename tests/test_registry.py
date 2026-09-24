import tempfile
import unittest
from pathlib import Path

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.registry import ProviderRegistry, default_registry
from cortex_relay.providers.base import ProviderAdapter, ProviderCapabilities


class FakeProvider(ProviderAdapter):
    name = "fake"

    def __init__(self, *, available: bool = True):
        self.available = available

    def capabilities(self):
        return ProviderCapabilities(
            name=self.name,
            binary="fake",
            available=self.available,
            structured_output=True,
            model_selection=False,
            reasoning_control=False,
            read_only_policy=True,
            workspace_write=False,
            detail="fake",
        )

    def execute(self, task):
        return TaskResult(status="success", provider=self.name, summary=task.objective)


class RegistryTests(unittest.TestCase):
    def test_explicit_provider_executes(self):
        registry = ProviderRegistry([FakeProvider()])
        with tempfile.TemporaryDirectory() as tmp:
            result = registry.execute(
                TaskSpec(objective="check", provider="fake", workspace=Path(tmp))
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "fake")

    def test_default_registry_contains_codex_and_antigravity(self):
        self.assertEqual(set(default_registry().names()), {"antigravity", "codex"})

    def test_unknown_provider_returns_unavailable(self):
        registry = ProviderRegistry([FakeProvider()])
        result = registry.execute(TaskSpec(objective="check", provider="missing"))
        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
