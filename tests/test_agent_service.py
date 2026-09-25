import tempfile
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


class AgentServiceTests(unittest.TestCase):
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
