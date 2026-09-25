import json
import unittest

from cortex_relay.runtime.progress import normalize_progress


class ProgressProjectionTests(unittest.TestCase):
    def test_stderr_projects_canonical_diagnostic(self):
        warning = normalize_progress(
            "opencode",
            "provider warming up",
            "task-1",
            stream="stderr",
        )
        self.assertIsNotNone(warning)
        assert warning is not None
        self.assertEqual(warning.phase, "diagnostic")
        self.assertEqual(warning.state, "warning")
        self.assertEqual(warning.error_preview, "provider warming up")

        error = normalize_progress(
            "opencode",
            "ERROR: provider failed",
            "task-1",
            stream="stderr",
        )
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.state, "error")

    def test_antigravity_tool_progress_preserves_metrics_and_file(self):
        event = {
            "event": "step_update",
            "step_update": {
                "step_index": 7,
                "step_type": "tool",
                "tool_name": "write_file",
                "state": "DONE",
                "duration_seconds": 1.25,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "total_tokens": 125,
                    "cache_read_tokens": 10,
                },
                "tool_info": {
                    "parameters": {
                        "path": "src/app.py",
                        "command": "write src/app.py",
                    },
                    "output": "updated",
                },
            },
        }
        progress = normalize_progress(
            "antigravity",
            json.dumps(event),
            "task-1",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "tool")
        self.assertEqual(progress.state, "done")
        self.assertEqual(progress.tool, "write_file")
        self.assertEqual(progress.path, "src/app.py")
        self.assertEqual(progress.files, ("src/app.py",))
        self.assertEqual(progress.step_index, 7)
        self.assertEqual(progress.duration_seconds, 1.25)
        self.assertEqual(progress.input_tokens, 100)
        self.assertEqual(progress.output_tokens, 25)
        self.assertEqual(progress.total_tokens, 125)
        self.assertEqual(progress.cache_tokens, 10)

    def test_antigravity_child_only_progress_projects_subagent(self):
        event = {
            "event": "step_update",
            "step_update": {
                "step_index": 3,
                "step_type": "other",
                "duration_seconds": 0.5,
                "subagent_info": {
                    "subagents": [
                        {
                            "role": "explorer",
                            "state": "RUNNING",
                            "conversation_id": "child-1",
                        }
                    ]
                },
            },
        }
        progress = normalize_progress(
            "antigravity",
            json.dumps(event),
            "task-1",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "subagent")
        self.assertEqual(progress.state, "active")
        self.assertEqual(progress.step_index, 3)
        self.assertEqual(progress.subagents[0]["conversation_id"], "child-1")
        self.assertEqual(progress.subagents[0]["role"], "explorer")

    def test_codex_app_server_file_change_projects_files(self):
        event = {
            "method": "item/completed",
            "params": {
                "turnId": "turn-1",
                "item": {
                    "type": "fileChange",
                    "id": "item-1",
                    "changes": [
                        {"path": "src/a.py"},
                        {"path": "src/b.py"},
                    ],
                },
            },
        }
        progress = normalize_progress(
            "codex",
            json.dumps(event),
            "task-1",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "tool")
        self.assertEqual(progress.state, "done")
        self.assertEqual(progress.tool, "fileChange")
        self.assertEqual(progress.path, "src/a.py")
        self.assertEqual(progress.files, ("src/a.py", "src/b.py"))

    def test_codex_exec_todo_list_projects_plan(self):
        event = {
            "type": "item.updated",
            "item": {
                "type": "todo_list",
                "items": [
                    {"text": "Inspect", "completed": True},
                    {"text": "Patch", "completed": False},
                ],
            },
        }
        progress = normalize_progress(
            "codex",
            json.dumps(event),
            "task-1",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "plan")
        self.assertEqual(progress.activity, "Plan updated (1/2 done)")
        self.assertEqual(len(progress.plan), 2)
        self.assertTrue(progress.plan[0]["completed"])

    def test_codex_child_event_projects_original_tool_and_subagent(self):
        event = {
            "method": "item/started",
            "params": {
                "turnId": "turn-1",
                "item": {
                    "type": "collabAgentToolCall",
                    "id": "call-1",
                    "tool": "spawnAgent",
                    "senderThreadId": "parent",
                    "receiverThreadIds": ["child-thread"],
                    "agentsStates": {},
                },
            },
        }
        progress = normalize_progress(
            "codex",
            json.dumps(event),
            "task-1",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "tool")
        self.assertEqual(progress.tool, "collabAgentToolCall")
        self.assertEqual(progress.state, "active")
        self.assertEqual(
            progress.subagents[0]["conversation_id"],
            "child-thread",
        )

    def test_opencode_step_finish_projects_token_usage(self):
        event = {
            "type": "step_finish",
            "sessionID": "session-1",
            "part": {
                "tokens": {
                    "input": 30,
                    "output": 5,
                    "total": 35,
                    "cache": {"read": 12},
                }
            },
        }
        progress = normalize_progress(
            "opencode",
            json.dumps(event),
            "task-1",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "turn")
        self.assertEqual(progress.state, "done")
        self.assertEqual(progress.input_tokens, 30)
        self.assertEqual(progress.output_tokens, 5)
        self.assertEqual(progress.total_tokens, 35)
        self.assertEqual(progress.cache_tokens, 12)

    def test_opencode_tool_projects_duration_file_and_child(self):
        event = {
            "type": "tool",
            "sessionID": "parent-session",
            "part": {
                "type": "tool",
                "tool": "write",
                "state": {
                    "status": "completed",
                    "input": {
                        "filePath": "src/main.py",
                        "command": "write src/main.py",
                        "subagent_type": "explorer",
                    },
                    "metadata": {"sessionID": "child-session"},
                    "output": "done",
                    "time": {"start": 1000, "end": 2500},
                },
            },
        }
        progress = normalize_progress(
            "opencode",
            json.dumps(event),
            "task-1",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "tool")
        self.assertEqual(progress.state, "done")
        self.assertEqual(progress.path, "src/main.py")
        self.assertEqual(progress.files, ("src/main.py",))
        self.assertEqual(progress.duration_seconds, 1.5)

    def test_opencode_task_projects_child_from_same_canonical_event_batch(self):
        event = {
            "type": "tool",
            "sessionID": "parent-session",
            "part": {
                "type": "tool",
                "tool": "task",
                "state": {
                    "status": "completed",
                    "input": {"subagent_type": "explorer"},
                    "metadata": {"sessionID": "child-session"},
                    "output": "done",
                },
            },
        }
        progress = normalize_progress(
            "opencode",
            json.dumps(event),
            "task-1",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.phase, "tool")
        self.assertEqual(progress.tool, "task")
        self.assertEqual(progress.subagents[0]["conversation_id"], "child-session")
        self.assertEqual(progress.subagents[0]["role"], "explorer")


if __name__ == "__main__":
    unittest.main()
