import json
import unittest

from cortex_relay.runtime.agent_events import normalize_agent_events


class AgentEventNormalizationTests(unittest.TestCase):
    def test_antigravity_response_delta_and_child_are_preserved(self):
        event = {
            "event": "step_update",
            "step_update": {
                "step_index": 3,
                "step_type": "agent_response",
                "text_delta": "partial answer",
                "subagent_info": {
                    "subagents": [
                        {
                            "role": "researcher",
                            "type_name": "worker",
                            "state": "RUNNING",
                            "conversation_id": "child-123",
                            "log_uri": "file:///tmp/child.log",
                            "workspace_uris": ["file:///tmp/work"],
                        }
                    ]
                },
            },
        }
        normalized = normalize_agent_events(
            "antigravity",
            json.dumps(event),
            "agent-root",
        )
        self.assertEqual(normalized[0].kind, "text_delta")
        self.assertEqual(normalized[0].data["text"], "partial answer")
        child = next(item for item in normalized if item.kind == "child_update")
        self.assertEqual(child.data["provider_session_id"], "child-123")
        self.assertEqual(child.data["log_uri"], "file:///tmp/child.log")

    def test_codex_app_server_delta_is_preserved(self):
        event = {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "delta": "hello",
            },
        }
        normalized = normalize_agent_events(
            "codex",
            json.dumps(event),
            "agent-root",
        )
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0].kind, "text_delta")
        self.assertEqual(normalized[0].data["text"], "hello")

    def test_opencode_task_tool_child_is_preserved(self):
        event = {
            "type": "tool",
            "sessionID": "parent-session",
            "part": {
                "type": "tool",
                "tool": "task",
                "state": {
                    "status": "completed",
                    "input": {"subagent_type": "explore"},
                    "metadata": {"sessionID": "child-session"},
                    "output": "done",
                },
            },
        }
        normalized = normalize_agent_events(
            "opencode",
            json.dumps(event),
            "agent-root",
        )
        child = next(item for item in normalized if item.kind == "child_update")
        self.assertEqual(child.data["provider_session_id"], "child-session")
        self.assertEqual(child.data["role"], "explore")


if __name__ == "__main__":
    unittest.main()
