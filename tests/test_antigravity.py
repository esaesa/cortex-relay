import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cortex_relay.core.models import TaskSpec
from cortex_relay.providers.antigravity import AntigravityAdapter
from cortex_relay.runtime.process import ProcessResult


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, argv, *, cwd, timeout_seconds, env=None, cancel_event=None):
        self.calls.append((list(argv), cwd, timeout_seconds))
        return self.result


class AntigravityTests(unittest.TestCase):
    def test_command_uses_structured_output_and_sandbox(self):
        adapter = AntigravityAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            task = TaskSpec(
                objective="Review auth",
                workspace=workspace,
                reasoning="medium",
                model="gemini-3.8-flash-high",
            )
            command = adapter.command_for(task)
            prompt = adapter._prompt(task)
        self.assertIn("--json-schema", command)
        self.assertIn("--output-format", command)
        self.assertIn("--sandbox", command)
        self.assertIn("--disable-slash-commands", command)
        self.assertIn("--add-dir", command)
        add_dir_index = command.index("--add-dir") + 1
        self.assertEqual(Path(command[add_dir_index]).resolve(), task.workspace)
        self.assertIn("--effort", command)
        self.assertIn("--model", command)
        self.assertIn(f"Workspace: {task.workspace}", prompt)

    def test_claude_model_omits_unsupported_effort_flag(self):
        adapter = AntigravityAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(
                objective="Review auth",
                workspace=Path(tmp),
                reasoning="high",
                model="claude-sonnet-4-6",
            )
            command = adapter.command_for(task)
        self.assertNotIn("--effort", command)
        self.assertIn("--model", command)
        self.assertIn("claude-sonnet-4-6", command)


    def test_continue_session_adds_conversation_handle(self):
        adapter = AntigravityAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(
                objective="Continue review",
                workspace=Path(tmp),
                reasoning="medium",
                model="gemini-3.8-flash-high",
                metadata={"_resume_provider_session_id": "conv-existing"},
            )
            command = adapter.command_for(task)
        self.assertIn("--conversation", command)
        self.assertEqual(
            command[command.index("--conversation") + 1],
            "conv-existing",
        )

    @patch("cortex_relay.providers.antigravity.shutil.which", return_value="/usr/bin/agy")
    def test_successful_envelope_is_normalized(self, _which):
        envelope = {
            "status": "SUCCESS",
            "conversation_id": "abc",
            "duration_seconds": 2.5,
            "num_turns": 1,
            "usage": {"total_tokens": 10},
            "structured_output": {
                "summary": "One issue.",
                "final_text": "The complete review found one missing authorization check.",
                "evidence": [{"finding": "Missing check", "path": "auth.py"}],
                "changed_files": [],
                "commands": [],
                "tests": ["unit tests not run"],
                "risks": [],
            },
        }
        runner = FakeRunner(
            ProcessResult(
                argv=("agy",),
                returncode=0,
                stdout=json.dumps(envelope),
                stderr="",
            )
        )
        adapter = AntigravityAdapter(runner=runner)

        with tempfile.TemporaryDirectory() as tmp:
            result = adapter.execute(
                TaskSpec(
                    objective="Review auth",
                    workspace=Path(tmp),
                    access="workspace_write",
                )
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.conversation_id, "abc")
        self.assertEqual(
            result.final_text,
            "The complete review found one missing authorization check.",
        )
        self.assertEqual(result.evidence[0].path, "auth.py")

    @patch("cortex_relay.providers.antigravity.shutil.which", return_value="/usr/bin/agy")
    def test_print_timeout_is_reported_as_timeout(self, _which):
        runner = FakeRunner(
            ProcessResult(
                argv=("agy",),
                returncode=0,
                stdout=json.dumps({"status": "SUCCESS", "response": ""}),
                stderr="[agy] print timeout after 1m0s with turn in progress; returning partial output",
            )
        )
        adapter = AntigravityAdapter(runner=runner)

        with tempfile.TemporaryDirectory() as tmp:
            result = adapter.execute(
                TaskSpec(
                    objective="List files",
                    workspace=Path(tmp),
                    access="read_only",
                )
            )

        self.assertEqual(result.status, "timeout")
        self.assertIn("print timeout", result.error)

    @patch("cortex_relay.providers.antigravity.shutil.which", return_value=None)
    def test_missing_binary_returns_unavailable(self, _which):
        adapter = AntigravityAdapter()
        result = adapter.execute(TaskSpec(objective="Review auth"))
        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
