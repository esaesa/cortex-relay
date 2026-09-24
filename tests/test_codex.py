import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cortex_relay.core.models import TaskSpec
from cortex_relay.providers.codex import CodexAdapter
from cortex_relay.providers.codex_models import compatibility_error, known_model_ids
from cortex_relay.runtime.process import ProcessResult


class FakeRunner:
    def __init__(self, *, payload=None, returncode=0, stderr=""):
        self.payload = payload or {
            "summary": "Reviewed.",
            "evidence": [{"finding": "No material defect", "path": "auth.py"}],
            "changed_files": [],
            "commands": ["python -m unittest"],
            "tests": ["unit tests passed"],
            "risks": [],
        }
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def run(self, argv, *, cwd, timeout_seconds, env=None):
        self.calls.append((list(argv), cwd, timeout_seconds))
        output_index = list(argv).index("--output-last-message") + 1
        Path(argv[output_index]).write_text(json.dumps(self.payload), encoding="utf-8")
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 12,
                            "cached_input_tokens": 3,
                            "output_tokens": 4,
                        },
                    }
                ),
            ]
        )
        return ProcessResult(
            argv=tuple(argv),
            returncode=self.returncode,
            stdout=stdout,
            stderr=self.stderr,
        )


class CodexModelTests(unittest.TestCase):
    def test_current_gpt6_models_are_exposed(self):
        self.assertEqual(
            known_model_ids(),
            ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna"),
        )

    def test_known_model_effort_compatibility(self):
        self.assertIsNotNone(compatibility_error("gpt-6-astra", "none"))
        self.assertIsNone(compatibility_error("gpt-6-astra", "max"))
        self.assertIsNone(compatibility_error("gpt-6-sol", "none"))
        self.assertIsNone(compatibility_error("gpt-6-luna", "max"))

    def test_unknown_future_model_and_effort_are_not_blocked(self):
        self.assertIsNone(
            compatibility_error("gpt-future-codex", "future-effort")
        )


class CodexAdapterTests(unittest.TestCase):
    @patch("cortex_relay.providers.codex.shutil.which", return_value="/usr/bin/codex")
    def test_successful_execution_is_normalized(self, _which):
        runner = FakeRunner()
        adapter = CodexAdapter(runner=runner)

        with tempfile.TemporaryDirectory() as tmp:
            result = adapter.execute(
                TaskSpec(
                    objective="Review auth",
                    provider="codex",
                    model="gpt-6-sol",
                    reasoning="high",
                    workspace=Path(tmp),
                    access="workspace_write",
                )
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "codex")
        self.assertEqual(result.conversation_id, "thread-123")
        self.assertEqual(result.usage["input_tokens"], 12)
        self.assertEqual(result.evidence[0].path, "auth.py")

        command = runner.calls[0][0]
        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--json", command)
        self.assertIn("--output-schema", command)
        self.assertIn("--output-last-message", command)
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)
        self.assertIn("--model", command)
        self.assertIn("gpt-6-sol", command)
        self.assertIn('model_reasoning_effort="high"', command)

    @patch("cortex_relay.providers.codex.shutil.which", return_value="/usr/bin/codex")
    def test_read_only_uses_read_only_sandbox(self, _which):
        adapter = CodexAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            schema = root / "schema.json"
            output = root / "output.json"
            command = adapter.command_for(
                TaskSpec(objective="Review", provider="codex", workspace=root),
                schema_path=schema,
                last_message_path=output,
            )
        sandbox_index = command.index("--sandbox") + 1
        self.assertEqual(command[sandbox_index], "read-only")

    @patch("cortex_relay.providers.codex.shutil.which", return_value="/usr/bin/codex")
    def test_astra_none_is_rejected_before_execution(self, _which):
        runner = FakeRunner()
        adapter = CodexAdapter(runner=runner)
        result = adapter.execute(
            TaskSpec(
                objective="Review",
                provider="codex",
                model="gpt-6-astra",
                reasoning="none",
            )
        )
        self.assertEqual(result.status, "error")
        self.assertIn("does not support", result.error)
        self.assertEqual(runner.calls, [])

    @patch("cortex_relay.providers.codex.shutil.which", return_value="/usr/bin/codex")
    def test_future_model_and_effort_pass_through(self, _which):
        runner = FakeRunner()
        adapter = CodexAdapter(runner=runner)

        with tempfile.TemporaryDirectory() as tmp:
            result = adapter.execute(
                TaskSpec(
                    objective="Review",
                    provider="codex",
                    model="gpt-future-codex",
                    reasoning="future-effort",
                    workspace=Path(tmp),
                    access="workspace_write",
                )
            )

        self.assertTrue(result.ok)
        command = runner.calls[0][0]
        self.assertIn("gpt-future-codex", command)
        self.assertIn('model_reasoning_effort="future-effort"', command)

    @patch("cortex_relay.providers.codex.shutil.which", return_value=None)
    def test_missing_binary_returns_unavailable(self, _which):
        result = CodexAdapter().execute(TaskSpec(objective="Review", provider="codex"))
        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
