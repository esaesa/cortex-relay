import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from cortex_relay.cli import main
from cortex_relay.core.models import TaskResult


class FakeRegistry:
    def capabilities(self):
        return [
            {
                "name": "fake",
                "binary": "fake",
                "available": True,
                "structured_output": True,
                "model_selection": True,
                "reasoning_control": True,
                "read_only_policy": True,
                "workspace_write": True,
                "detail": "/bin/fake",
            }
        ]

    def execute(self, task):
        return TaskResult(status="success", provider="fake", summary=task.objective)


class CLIRuntimeTests(unittest.TestCase):
    @patch("cortex_relay.cli.default_registry", return_value=FakeRegistry())
    def test_providers_command(self, _registry):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["providers"])
        self.assertEqual(code, 0)
        self.assertIn("fake: available", output.getvalue())

    @patch("cortex_relay.cli.default_registry", return_value=FakeRegistry())
    def test_delegate_json(self, _registry):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["delegate", "Review auth", "--json"])
        self.assertEqual(code, 0)
        self.assertIn('"status": "success"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
