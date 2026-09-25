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


class CancellableSessionProvider(SessionProvider):
    def execute_session(self, task: TaskSpec) -> TaskResult:
        cancel_event = task.metadata.get("_cancel_event")
        progress = task.metadata.get("_progress_line")
        if callable(progress):
            progress(
                self.name,
                json.dumps({"type": "provider.ping", "detail": "cancellable"}),
                "stdout",
            )
        for _ in range(200):
            if cancel_event is not None and cancel_event.is_set():
                return TaskResult(
                    status="cancelled",
                    provider=self.name,
                    model=task.model,
                    summary="cancelled",
                    final_text="",
                    conversation_id="provider-session-cancelled",
                )
            threading.Event().wait(0.01)
        return TaskResult(
            status="success",
            provider=self.name,
            model=task.model,
            summary="completed",
            final_text="completed",
            conversation_id="provider-session-cancelled",
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


class FailingSessionProvider(SessionProvider):
    def execute_session(self, task: TaskSpec) -> TaskResult:
        return TaskResult(
            status="error",
            provider=self.name,
            model=task.model,
            summary="failed after session start",
            error="boom",
            conversation_id="provider-session-failed",
        )


class AlternateSessionProvider(SessionProvider):
    name = "alt"

    def __init__(self) -> None:
        super().__init__()
        self.executed = False

    def execute_session(self, task: TaskSpec) -> TaskResult:
        self.executed = True
        return TaskResult(
            status="success",
            provider=self.name,
            summary="alternate",
            final_text="alternate answer",
            conversation_id="provider-session-alt",
        )


class FallbackProfileResolver:
    def load(self, _workspace):
        from cortex_relay.core.profiles import runtime_config_from_mapping

        return runtime_config_from_mapping(
            {
                "profiles": {
                    "primary": {
                        "provider": "fake",
                        "access": "read_only",
                        "fallbacks": ["alternate"],
                        "fallback_on": ["error", "unavailable"],
                    },
                    "alternate": {
                        "provider": "alt",
                        "access": "read_only",
                    },
                },
                "roles": {"reviewer": "primary"},
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
    def test_direct_session_never_switches_provider_after_identity_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            failing = FailingSessionProvider()
            alternate = AlternateSessionProvider()
            registry = ProviderRegistry(
                [failing, alternate],
                run_store=RunStore(root / "state"),
                profiles=FallbackProfileResolver(),
            )
            service = AgentService(registry)
            self.addCleanup(service.shutdown)

            result = service.start(
                objective="inspect",
                role="reviewer",
                workspace=workspace,
            )

            self.assertEqual(result["status"], "error")
            self.assertEqual(result["provider"], "fake")
            self.assertIn("agent_session_id", result)
            self.assertFalse(alternate.executed)

    def test_watch_returns_visible_updates_and_authoritative_running_state(self):
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
                objective="inspect with updates",
                provider="fake",
                workspace=workspace,
                timeout_seconds=60,
            )
            session_id = started["agent_session_id"]

            watched = service.watch(
                session_id,
                after_sequence=0,
                timeout_seconds=1,
            )
            self.assertFalse(watched["complete"])
            self.assertTrue(watched["authoritative_session"])
            self.assertFalse(watched["replacement_recommended"])
            self.assertTrue(watched["updates"])
            self.assertGreaterEqual(watched["next_sequence"], 1)

            provider.release.set()
            completed = service.wait(session_id, timeout_seconds=5)
            self.assertTrue(completed["complete"])

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
            self.assertEqual(messages["messages"][0]["direction"], "host_to_agent")
            self.assertEqual(messages["messages"][0]["content"], "inspect")
            self.assertEqual(messages["messages"][-1]["direction"], "agent_to_host")
            cursor = messages["messages"][0]["sequence"]
            incremental = service.messages(session_id, after_sequence=cursor)
            self.assertEqual(len(incremental["messages"]), 1)
            self.assertEqual(incremental["messages"][0]["direction"], "agent_to_host")

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

    def test_cancel_interrupts_active_provider_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = CancellableSessionProvider()
            registry = ProviderRegistry(
                [provider],
                run_store=RunStore(root / "state"),
            )
            service = AgentService(
                registry,
                lease_ttl_seconds=2,
                lease_heartbeat_seconds=0.2,
            )
            self.addCleanup(service.shutdown)

            started = service.start_async(
                objective="long turn",
                provider="fake",
                workspace=workspace,
                timeout_seconds=30,
            )
            session_id = started["agent_session_id"]
            cancelled = service.cancel(session_id)
            self.assertTrue(cancelled["cancel_requested"])
            self.assertTrue(cancelled["delivered"])

            completed = service.wait(session_id, timeout_seconds=5)
            self.assertTrue(completed["complete"])
            self.assertEqual(completed["status"], "interrupted")
            self.assertEqual(completed["result"]["status"], "cancelled")

    def test_close_rejects_active_turn_and_send_rejects_closed_session(self):
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
            self.addCleanup(service.shutdown)

            started = service.start_async(
                objective="hold open",
                provider="fake",
                workspace=workspace,
                timeout_seconds=30,
            )
            session_id = started["agent_session_id"]
            with self.assertRaisesRegex(ValueError, "active turn"):
                service.close(session_id)

            provider.release.set()
            self.assertTrue(service.wait(session_id, timeout_seconds=5)["complete"])
            closed = service.close(session_id)
            self.assertEqual(closed["state"], "closed")
            with self.assertRaisesRegex(ValueError, "follow-up messages require an idle session"):
                service.send(session_id, "should fail")

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
            self.assertEqual(messages["messages"][-2]["direction"], "host_to_agent")
            self.assertEqual(messages["messages"][-1]["direction"], "agent_to_host")


if __name__ == "__main__":
    unittest.main()
