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

    def run(self, argv, *, cwd, timeout_seconds, env=None):
        self.calls.append((list(argv), cwd, timeout_seconds))
        return self.result


class AntigravityTests(unittest.TestCase):
    def test_command_uses_structured_output_and_sandbox(self):
        adapter = AntigravityAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(
                objective="Review auth",
                workspace=Path(tmp),
                reasoning="medium",
                model="gemini-3.8-flash-high",
            )
            command = adapter.command_for(task)
        self.assertIn("--json-schema", command)
        self.assertIn("--output-format", command)
        self.assertIn("--sandbox", command)
        self.assertIn("--effort", command)
        self.assertIn("--model", command)

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
        self.assertEqual(result.evidence[0].path, "auth.py")

    @patch("cortex_relay.providers.antigravity.shutil.which", return_value=None)
    def test_missing_binary_returns_unavailable(self, _which):
        adapter = AntigravityAdapter()
        result = adapter.execute(TaskSpec(objective="Review auth"))
        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
