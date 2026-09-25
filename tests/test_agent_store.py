import tempfile
import unittest
from pathlib import Path

from cortex_relay.core.agents import AgentEvent
from cortex_relay.runtime.agent_store import AgentStore


class AgentStoreTests(unittest.TestCase):
    def test_session_events_messages_and_children_are_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStore(Path(tmp))
            parent = store.create(
                provider="fake",
                workspace=Path(tmp),
                task_id="task-1",
                model="m1",
                reasoning="high",
                access="read_only",
                role="reviewer",
                objective="review",
            )
            store.bind_provider_session(parent.session_id, "provider-parent")
            store.record_event(
                AgentEvent(
                    session_id=parent.session_id,
                    kind="text_delta",
                    data={"text": "working"},
                    provider_event="delta",
                )
            )
            store.add_message(
                parent.session_id,
                direction="agent_to_host",
                content="done",
            )
            child = store.upsert_child(
                parent_session_id=parent.session_id,
                provider_session_id="provider-child",
                provider="fake",
                state="running",
                role="explorer",
                metadata={"log_uri": "file:///tmp/child.log"},
            )

            reloaded = AgentStore(Path(tmp))
            self.assertEqual(
                reloaded.get(parent.session_id).provider_session_id,
                "provider-parent",
            )
            events = reloaded.events(parent.session_id)
            self.assertEqual(events["events"][0]["kind"], "text_delta")
            self.assertEqual(
                reloaded.messages(parent.session_id)[0]["content"],
                "done",
            )
            children = reloaded.children(parent.session_id)
            self.assertEqual(children[0]["session_id"], child.session_id)
            self.assertEqual(children[0]["provider_session_id"], "provider-child")
            self.assertEqual(children[0]["parent_session_id"], parent.session_id)
            self.assertEqual(children[0]["root_session_id"], parent.session_id)


if __name__ == "__main__":
    unittest.main()
