import json
import unittest

from cortex_relay.runtime.agent_events import normalize_agent_events
from cortex_relay.runtime.progress import (
    normalize_progress,
    normalize_progress_events,
    project_agent_event,
)


class ProgressProjectionTests(unittest.TestCase):
    def test_antigravity_tool_progress_comes_from_same_semantic_event(self):
        raw = {
            "event": "step_update",
            "step_update": {
                "step_index": 7,
                "step_type": "tool",
                "tool_name": "write_file",
                "state": "DONE",
                "duration_seconds": 1.5,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
                "tool_info": {
                    "parameters": {
                        "file_path": "src/example.py",
                        "command": "write",
                    },
                    "output": "ok",
                },
            },
        }
        line = json.dumps(raw)
        semantic = normalize_agent_events(
            "antigravity", line, "agent-test"
        )
        tool_event = next(item for item in semantic if item.kind == "tool")
        projected = project_agent_event(tool_event, "task-test")
        direct = normalize_progress_events(
            "antigravity", line, "task-test"
        )

        self.assertIsNotNone(projected)
        assert projected is not None
        self.assertEqual(projected.phase, "tool")
        self.assertEqual(projected.state, "done")
        self.assertEqual(projected.path, "src/example.py")
        self.assertEqual(projected.files, ("src/example.py",))
        self.assertEqual(projected.total_tokens, 120)
        self.assertTrue(any(item == projected for item in direct))

    def test_antigravity_untracked_child_is_visible_in_workflow_progress(self):
        raw = {
            "event": "step_update",
            "step_update": {
                "step_index": 4,
                "step_type": "tool",
                "tool_name": "manage_task",
                "state": "RUNNING",
                "tool_info": {
                    "parameters": {"action": "spawn", "role": "explorer"}
                },
            },
        }
        events = normalize_progress_events(
            "antigravity",
            json.dumps(raw),
            "task-test",
        )
        warning = next(
            item
            for item in events
            if item.activity.startswith("Untracked native child")
        )
        self.assertEqual(warning.phase, "subagent")
        self.assertEqual(warning.state, "warning")

    def test_codex_plan_is_canonical_semantic_event_and_progress(self):
        raw = {
            "type": "item.updated",
            "item": {
                "type": "todo_list",
                "items": [
                    {"text": "inspect", "completed": True},
                    {"text": "test", "completed": False},
                ],
            },
        }
        line = json.dumps(raw)
        semantic = normalize_agent_events(
            "codex", line, "agent-test"
        )
        self.assertEqual(semantic[0].kind, "plan")
        progress = normalize_progress(
            "codex", line, "task-test"
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "plan")
        self.assertEqual(len(progress.plan), 2)
        self.assertTrue(progress.plan[0]["completed"])

    def test_opencode_one_line_can_project_session_and_response(self):
        raw = {
            "type": "text",
            "sessionID": "provider-session",
            "part": {
                "type": "text",
                "text": "hello",
                "id": "part-1",
            },
        }
        events = normalize_progress_events(
            "opencode", json.dumps(raw), "task-test"
        )
        self.assertEqual(
            [event.phase for event in events],
            ["startup", "response"],
        )
        best = normalize_progress(
            "opencode", json.dumps(raw), "task-test"
        )
        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best.phase, "response")
        self.assertEqual(best.output_preview, "hello")

    def test_stderr_secret_is_redacted_before_semantic_persistence(self):
        line = "authorization=secret-value password=hunter2"
        semantic = normalize_agent_events(
            "antigravity",
            line,
            "agent-test",
            "stderr",
        )
        stored = semantic[0].data["text"]
        self.assertNotIn("secret-value", stored)
        self.assertNotIn("hunter2", stored)
        progress = normalize_progress_events(
            "antigravity",
            line,
            "task-test",
            "stderr",
        )
        self.assertEqual(progress[0].phase, "diagnostic")
        self.assertNotIn("secret-value", progress[0].error_preview or "")


if __name__ == "__main__":
    unittest.main()
