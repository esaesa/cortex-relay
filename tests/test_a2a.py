import tempfile
import threading
import unittest
from pathlib import Path

from cortex_relay.core.models import TaskBudget, TaskResult
from cortex_relay.transports.a2a import (
    A2AServerPolicy,
    a2a_available,
    agent_card_url,
    create_a2a_app,
    gemini_remote_agent_markdown,
    resolve_public_url,
    validate_bind_host,
)


class FakeRegistry:
    def capabilities(self):
        return []

    def execute(self, task):
        return TaskResult(
            status="success",
            provider=task.provider,
            model=task.model,
            summary=task.objective,
        )


class A2APolicyTests(unittest.TestCase):
    def test_policy_locks_provider_workspace_and_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            cancel_event = threading.Event()
            policy = A2AServerPolicy(
                profile="zen-luna",
                preset="cheap",
                provider="codex",
                model="gpt-6-luna",
                reasoning="max",
                role="reviewer",
                workspace=Path(tmp),
                access="read_only",
            )
            task = policy.task_spec_for(
                "Review authentication",
                cancel_event=cancel_event,
            )

        self.assertEqual(task.profile, "zen-luna")
        self.assertEqual(task.preset, "cheap")
        self.assertEqual(task.provider, "codex")
        self.assertEqual(task.model, "gpt-6-luna")
        self.assertEqual(task.reasoning, "max")
        self.assertEqual(task.access, "read_only")
        self.assertIs(task.metadata["_cancel_event"], cancel_event)
        self.assertNotIn("_cancel_event", task.to_dict()["metadata"])

    def test_policy_carries_supervision_budget_into_task(self):
        policy = A2AServerPolicy(
            provider="codex",
            budget=TaskBudget(
                max_tool_calls=8,
                max_repeated_calls=2,
                max_idle_seconds=30,
                max_runtime_seconds=120,
                max_child_agents=3,
            ),
        )
        task = policy.task_spec_for("Review")
        self.assertEqual(task.budget.max_tool_calls, 8)
        self.assertEqual(task.budget.max_repeated_calls, 2)
        self.assertEqual(task.budget.max_idle_seconds, 30)
        self.assertEqual(task.budget.max_runtime_seconds, 120)
        self.assertEqual(task.budget.max_child_agents, 3)

    def test_policy_carries_transport_progress_callback(self):
        policy = A2AServerPolicy(provider="codex")
        callback = lambda event: None
        task = policy.task_spec_for("Review", progress_callback=callback)
        self.assertIs(task.metadata["_external_progress"], callback)
        self.assertNotIn("_external_progress", task.to_dict()["metadata"])

    def test_workspace_write_isolation_defaults_on(self):
        policy = A2AServerPolicy(access="workspace_write")
        self.assertTrue(policy.isolate_write)

    def test_public_url_and_card_url(self):
        public = resolve_public_url(
            host="0.0.0.0",
            port=8765,
            public_url=None,
        )
        self.assertEqual(public, "http://127.0.0.1:8765")
        self.assertEqual(
            agent_card_url(public),
            "http://127.0.0.1:8765/.well-known/agent-card.json",
        )

    def test_remote_bind_requires_explicit_opt_in(self):
        with self.assertRaises(RuntimeError):
            validate_bind_host("0.0.0.0", allow_remote=False)
        validate_bind_host("0.0.0.0", allow_remote=True)
        validate_bind_host("127.0.0.1", allow_remote=False)

    def test_gemini_remote_agent_definition(self):
        rendered = gemini_remote_agent_markdown(
            name="cortex-codex",
            agent_card_url="http://127.0.0.1:8765/.well-known/agent-card.json",
        )
        self.assertIn("kind: remote", rendered)
        self.assertIn("name: cortex-codex", rendered)
        self.assertIn("agent_card_url:", rendered)

        with self.assertRaises(ValueError):
            gemini_remote_agent_markdown(
                name="Not Valid",
                agent_card_url="http://127.0.0.1:8765/.well-known/agent-card.json",
            )


@unittest.skipUnless(a2a_available(), "A2A optional dependency is not installed")
class A2AInstalledRuntimeTests(unittest.TestCase):
    def test_app_exposes_health_and_agent_card(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            app = create_a2a_app(
                policy=A2AServerPolicy(
                    provider="codex",
                    workspace=Path(tmp),
                ),
                registry=FakeRegistry(),
                public_url="http://127.0.0.1:8765",
                name="cortex-relay",
            )

            client = TestClient(app)
            health = client.get("/healthz")
            card = client.get("/.well-known/agent-card.json")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["provider"], "codex")
        self.assertIsNone(health.json()["profile"])
        self.assertIsNone(health.json()["preset"])
        self.assertEqual(card.status_code, 200)
        payload = card.json()
        self.assertEqual(payload["name"], "cortex-relay")
        interfaces = payload.get("supportedInterfaces", [])
        versions = {
            (item.get("protocolBinding"), item.get("protocolVersion"))
            for item in interfaces
        }
        self.assertIn(("JSONRPC", "1.0"), versions)
        self.assertIn(("JSONRPC", "0.3"), versions)
        self.assertIn(("HTTP+JSON", "1.0"), versions)
        self.assertIn(("HTTP+JSON", "0.3"), versions)


if __name__ == "__main__":
    unittest.main()
