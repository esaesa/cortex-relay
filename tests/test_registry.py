import tempfile
import unittest

from pathlib import Path

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.profiles import runtime_config_from_mapping
from cortex_relay.core.registry import ProviderRegistry, default_registry
from cortex_relay.providers.base import ProviderAdapter, ProviderCapabilities
from cortex_relay.observability import RunStore
from cortex_relay.runtime.agent_store import AgentStore


class FakeProvider(ProviderAdapter):
    def __init__(self, name="fake", *, available=True, status="success"):
        self.name = name
        self.available = available
        self.status = status
        self.last_task = None

    def capabilities(self):
        return ProviderCapabilities(
            name=self.name,
            binary=self.name,
            available=self.available,
            structured_output=True,
            model_selection=True,
            reasoning_control=True,
            read_only_policy=True,
            workspace_write=True,
            detail=self.name,
        )

    def execute(self, task):
        self.last_task = task
        if not self.available:
            return TaskResult(
                status="unavailable",
                provider=self.name,
                model=task.model,
                summary="Unavailable",
            )
        return TaskResult(
            status=self.status,
            provider=self.name,
            model=task.model,
            summary=task.objective,
        )


class StaticResolver:
    def __init__(self, mapping):
        self.config = runtime_config_from_mapping(mapping)

    def load(self, _workspace):
        return self.config


class RegistryTests(unittest.TestCase):
    def test_registry_and_runstore_share_injected_agent_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_store = RunStore(root / "state")
            agent_store = AgentStore(root / "custom-agent-state")
            registry = ProviderRegistry(
                [FakeProvider()],
                profiles=StaticResolver({}),
                run_store=run_store,
                agent_store=agent_store,
            )
            self.assertIs(registry.agent_store, agent_store)
            self.assertIs(registry.run_store.agent_store, agent_store)

    def test_explicit_provider_executes(self):
        provider = FakeProvider()
        registry = ProviderRegistry([provider], profiles=StaticResolver({}))
        with tempfile.TemporaryDirectory() as tmp:
            result = registry.execute(
                TaskSpec(objective="check", provider="fake", workspace=Path(tmp))
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "fake")

    def test_default_registry_contains_all_runtime_providers(self):
        self.assertEqual(
            set(default_registry().names()),
            {"antigravity", "codex", "opencode"},
        )

    def test_unknown_provider_returns_unavailable(self):
        registry = ProviderRegistry(
            [FakeProvider()],
            profiles=StaticResolver({}),
        )
        result = registry.execute(TaskSpec(objective="check", provider="missing"))
        self.assertEqual(result.status, "unavailable")

    def test_role_mapping_resolves_execution_profile(self):
        zen = FakeProvider("opencode")
        registry = ProviderRegistry(
            [zen],
            profiles=StaticResolver(
                {
                    "profiles": {
                        "zen-luna": {
                            "provider": "opencode",
                            "model": "opencode/gpt-6-luna",
                            "reasoning": "max",
                            "billing_class": "zen-cheap",
                        }
                    },
                    "roles": {"reviewer": "zen-luna"},
                }
            ),
        )

        result = registry.execute(TaskSpec(objective="Review", role="reviewer"))

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "opencode")
        self.assertEqual(result.model, "opencode/gpt-6-luna")
        self.assertEqual(zen.last_task.reasoning, "max")
        self.assertEqual(result.metadata["profile"], "zen-luna")
        self.assertEqual(result.metadata["profile_source"], "role:reviewer")
        self.assertEqual(result.metadata["billing_class"], "zen-cheap")

    def test_explicit_profile_overrides_role(self):
        opencode = FakeProvider("opencode")
        codex = FakeProvider("codex")
        registry = ProviderRegistry(
            [opencode, codex],
            profiles=StaticResolver(
                {
                    "profiles": {
                        "muse": {"provider": "opencode", "model": "opencode/muse"},
                        "premium": {"provider": "codex", "model": "gpt-6-sol"},
                    },
                    "roles": {"reviewer": "muse"},
                }
            ),
        )

        result = registry.execute(
            TaskSpec(objective="Review", role="reviewer", profile="premium")
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "codex")
        self.assertEqual(result.model, "gpt-6-sol")
        self.assertEqual(result.metadata["profile_source"], "explicit")

    def test_profile_fallback_runs_on_unavailable(self):
        backup = FakeProvider("fake")
        registry = ProviderRegistry(
            [backup],
            profiles=StaticResolver(
                {
                    "profiles": {
                        "primary": {
                            "provider": "missing",
                            "fallbacks": ["backup"],
                        },
                        "backup": {"provider": "fake"},
                    },
                    "roles": {"reviewer": "primary"},
                }
            ),
        )

        result = registry.execute(TaskSpec(objective="Review", role="reviewer"))

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "fake")
        self.assertEqual(result.metadata["profile"], "backup")
        self.assertEqual(
            [item["status"] for item in result.metadata["routing_attempts"]],
            ["unavailable", "success"],
        )

    def test_profile_does_not_fallback_on_error_by_default(self):
        failing = FakeProvider("opencode", status="error")
        backup = FakeProvider("codex")
        registry = ProviderRegistry(
            [failing, backup],
            profiles=StaticResolver(
                {
                    "profiles": {
                        "primary": {
                            "provider": "opencode",
                            "fallbacks": ["backup"],
                        },
                        "backup": {"provider": "codex"},
                    },
                    "roles": {"reviewer": "primary"},
                }
            ),
        )

        result = registry.execute(TaskSpec(objective="Review", role="reviewer"))

        self.assertEqual(result.status, "error")
        self.assertIsNone(backup.last_task)
        self.assertEqual(len(result.metadata["routing_attempts"]), 1)

    def test_read_only_profile_cannot_satisfy_write_task(self):
        registry = ProviderRegistry(
            [FakeProvider("opencode")],
            profiles=StaticResolver(
                {
                    "profiles": {
                        "read": {
                            "provider": "opencode",
                            "access": "read_only",
                        }
                    },
                    "roles": {"implementer": "read"},
                }
            ),
        )

        result = registry.execute(
            TaskSpec(
                objective="Change code",
                role="implementer",
                access="workspace_write",
            )
        )

        self.assertEqual(result.status, "error")
        self.assertIn("does not permit workspace writes", result.summary)

    def test_execute_records_observability_and_returns_task_id(self):
        provider = FakeProvider()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            state = Path(tmp) / "state"
            workspace.mkdir()
            registry = ProviderRegistry(
                [provider],
                profiles=StaticResolver({}),
                run_store=RunStore(state),
            )
            result = registry.execute(
                TaskSpec(
                    objective="Inspect repository",
                    provider="fake",
                    workspace=workspace,
                )
            )
            snapshot = registry.status_snapshot(workspace)

        self.assertTrue(result.ok)
        self.assertIn("task_id", result.metadata)
        self.assertIn("observability", result.metadata)
        self.assertEqual(len(snapshot["tasks"]), 1)
        self.assertEqual(snapshot["tasks"][0]["status"], "success")
        self.assertEqual(
            snapshot["tasks"][0]["task_id"],
            result.metadata["task_id"],
        )

    def test_explicit_provider_bypasses_role_profile(self):
        opencode = FakeProvider("opencode")
        codex = FakeProvider("codex")
        registry = ProviderRegistry(
            [opencode, codex],
            profiles=StaticResolver(
                {
                    "profiles": {"muse": {"provider": "opencode"}},
                    "roles": {"reviewer": "muse"},
                }
            ),
        )

        result = registry.execute(
            TaskSpec(objective="Review", role="reviewer", provider="codex")
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "codex")
        self.assertIsNone(opencode.last_task)


if __name__ == "__main__":
    unittest.main()
