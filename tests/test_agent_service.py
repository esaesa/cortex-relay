import json
import tempfile
import threading
import unittest
from pathlib import Path

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.observability import RunStore
from cortex_relay.providers.base import ProviderAdapter, ProviderCapabilities
from cortex_relay.runtime.agent_service import AgentService


class SessionProvider(ProviderAdapter):
    name = "fake"

    def __init__(self) -> None:
        self.continued = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            binary="fake",
            available=True,
            structured_output=True,
            model_selection=True,
            reasoning_control=True,
            read_only_policy=True,
            workspace_write=True,
            session_mode="resumable",
            persistent_sessions=True,
            streaming_events=True,
            native_subagents=True,
            child_messaging=True,
        )

    def execute(self, task: TaskSpec) -> TaskResult:
        return self.execute_session(task)

    def execute_session(self, task: TaskSpec) -> TaskResult:
        progress = task.metadata.get("_progress_line")
        if callable(progress):
            progress(
                self.name,
                json.dumps({"type": "provider.ping", "detail": "initial turn"}),
                "stdout",
            )
        return TaskResult(
            status="success",
            provider=self.name,
            model=task.model,
            summary="initial",
            final_text="initial full answer",
            conversation_id="provider-session-1",
        )

    def continue_session(
        self,
        task: TaskSpec,
        provider_session_id: str,
    ) -> TaskResult:
        self.continued.append((provider_session_id, task.objective))
        return TaskResult(
            status="success",
            provider=self.name,
            model=task.model,
            summary="continued",
            final_text=f"continued: {task.objective}",
            conversation_id=provider_session_id,
        )


class SlowSessionProvider(SessionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def execute_session(self, task: TaskSpec) -> TaskResult:
        progress = task.metadata.get("_progress_line")
        if callable(progress):
            progress(
                self.name,
                json.dumps({"type": "provider.ping", "detail": "running"}),
                "stdout",
            )
        self.release.wait(5)
        return TaskResult(
            status="success",
            provider=self.name,
            model=task.model,
            summary="slow initial",
            final_text="slow full answer",
            conversation_id="provider-session-slow",
        )


class FakeProfileResolver:
    def load(self, _workspace):
        from cortex_relay.core.profiles import runtime_config_from_mapping

        return runtime_config_from_mapping(
            {
                "profiles": {
                    "writer": {
                        "provider": "fake",
                        "reasoning": "high",
                        "access": "workspace_write",
                    }
                },
                "roles": {"implementer": "writer"},
            }
        )


class ClosedEndProvider(ProviderAdapter):
    name = "closed"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name=self.name,
            binary="closed",
            available=True,
            structured_output=True,
            model_selection=True,
            reasoning_control=True,
            read_only_policy=True,
            workspace_write=True,
            session_mode="closed_end",
            persistent_sessions=False,
        )

    def execute(self, task: TaskSpec) -> TaskResult:
        return TaskResult(
            status="success",
            provider=self.name,
            summary="closed",
            final_text="closed answer",
        )


class AgentServiceTests(unittest.TestCase):
    def test_start_async_returns_session_before_turn_finishes_and_wait_is_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = SlowSessionProvider()
            registry = ProviderRegistry(
                [provider],
                run_store=RunStore(root / "state"),
            )
            service = AgentService(registry)

            started = service.start_async(
                objective="inspect concurrently",
                provider="fake",
                workspace=workspace,
                timeout_seconds=60,
            )
            session_id = started["agent_session_id"]

            self.assertIn(started["status"], {"starting", "running"})
            self.assertEqual(registry.run_store.indexed_task_ids(), [])
            pending = service.wait(session_id, timeout_seconds=0)
            self.assertFalse(pending["complete"])
            self.assertIsNone(pending["result"])

            provider.release.set()
            completed = service.wait(session_id, timeout_seconds=5)
            self.assertTrue(completed["complete"])
            self.assertEqual(completed["status"], "idle")
            self.assertEqual(
                completed["result"]["final_text"],
                "slow full answer",
            )
            self.assertEqual(
                service.result(session_id)["conversation_id"],
                "provider-session-slow",
            )

    def test_access_auto_inherits_profile_access_for_direct_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = SessionProvider()
            registry = ProviderRegistry(
                [provider],
                run_store=RunStore(root / "state"),
                profiles=FakeProfileResolver(),
            )
            service = AgentService(registry)

            started = service.start(
                objective="write directly",
                role="implementer",
                provider="auto",
                workspace=workspace,
                access="auto",
            )
            session = service.get(started["agent_session_id"])
            self.assertEqual(session["access"], "workspace_write")
            self.assertEqual(session["role"], "implementer")

    def test_start_creates_direct_persistent_session_without_task_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = SessionProvider()
            registry = ProviderRegistry(
                [provider],
                run_store=RunStore(root / "state"),
            )
            service = AgentService(registry)

            started = service.start(
                objective="inspect",
                provider="fake",
                workspace=workspace,
                timeout_seconds=60,
            )

            self.assertEqual(started["status"], "success")
            self.assertEqual(started["final_text"], "initial full answer")
            self.assertIn("agent_session_id", started)
            session_id = started["agent_session_id"]
            session = service.get(session_id)
            self.assertIsNone(session["task_id"])
            self.assertEqual(session["provider_session_id"], "provider-session-1")
            self.assertEqual(session["state"], "idle")
            self.assertEqual(registry.run_store.indexed_task_ids(), [])

            messages = service.messages(session_id)
            self.assertEqual(messages[0]["direction"], "host_to_agent")
            self.assertEqual(messages[0]["content"], "inspect")
            self.assertEqual(messages[-1]["direction"], "agent_to_host")

            events = service.events(session_id)["events"]
            self.assertTrue(events)
            self.assertEqual(events[0]["kind"], "provider_event")

    def test_start_rejects_closed_end_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            registry = ProviderRegistry(
                [ClosedEndProvider()],
                run_store=RunStore(root / "state"),
            )
            service = AgentService(registry)

            started = service.start(
                objective="inspect",
                provider="closed",
                workspace=workspace,
            )

            self.assertEqual(started["status"], "unavailable")
            self.assertIn("persistent agent sessions", started["summary"])
            self.assertIn("delegate/delegate_async", started["error"])
            self.assertNotIn("agent_session_id", started)
            self.assertEqual(registry.run_store.indexed_task_ids(), [])

    def test_followup_reuses_provider_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = SessionProvider()
            registry = ProviderRegistry(
                [provider],
                run_store=RunStore(root / "state"),
            )

            initial = registry.execute(
                TaskSpec(
                    objective="inspect",
                    provider="fake",
                    workspace=workspace,
                )
            )
            session_id = initial.metadata["agent_session_id"]
            service = AgentService(registry)

            session = service.get(session_id)
            self.assertEqual(
                session["provider_session_id"],
                "provider-session-1",
            )

            followup = service.send(
                session_id,
                "go deeper",
                timeout_seconds=60,
            )
            self.assertEqual(followup["status"], "success")
            self.assertEqual(
                followup["final_text"],
                "continued: go deeper",
            )
            self.assertEqual(
                provider.continued,
                [("provider-session-1", "go deeper")],
            )
            messages = service.messages(session_id)
            self.assertEqual(messages[-2]["direction"], "host_to_agent")
            self.assertEqual(messages[-1]["direction"], "agent_to_host")


if __name__ == "__main__":
    unittest.main()
