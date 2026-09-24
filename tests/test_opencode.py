import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from cortex_relay.core.models import TaskSpec
from cortex_relay.providers.opencode import OpenCodeAdapter
from cortex_relay.runtime.process import ProcessResult


class FakeRunner:
    def __init__(self, *, model_listing: str = ""):
        self.model_listing = model_listing
        self.calls = []

    def run(
        self,
        argv,
        *,
        cwd,
        timeout_seconds,
        env=None,
        cancel_event=None,
    ):
        self.calls.append(
            {
                "argv": list(argv),
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "env": env,
                "cancel_event": cancel_event,
            }
        )
        if len(argv) >= 2 and argv[1] == "models":
            return ProcessResult(
                argv=tuple(argv),
                returncode=0,
                stdout=self.model_listing,
                stderr="",
            )

        payload = {
            "summary": "Implemented.",
            "evidence": [{"finding": "Done", "path": "app.py"}],
            "changed_files": ["app.py"],
            "commands": ["python -m unittest"],
            "tests": ["tests passed"],
            "risks": [],
        }
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "session-123",
                        "part": {"text": json.dumps(payload)},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "sessionID": "session-123",
                        "part": {
                            "tokens": {"input": 20, "output": 10},
                            "cost": 0.01,
                            "modelID": "gpt-6-luna",
                            "providerID": "opencode",
                            "reason": "stop",
                        },
                    }
                ),
            ]
        )
        return ProcessResult(
            argv=tuple(argv),
            returncode=0,
            stdout=stdout,
            stderr="",
        )


class OpenCodeAdapterTests(unittest.TestCase):
    @patch("cortex_relay.providers.opencode.shutil.which", return_value="/usr/bin/opencode")
    def test_successful_execution_is_normalized_and_safe(self, _which):
        runner = FakeRunner()
        adapter = OpenCodeAdapter(runner=runner)

        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(
                objective="Implement the bounded change",
                provider="opencode",
                model="opencode/gpt-6-luna",
                reasoning="max",
                workspace=Path(tmp),
                access="workspace_write",
                metadata={"profile_options": {"validate_variant": False}},
            )
            result = adapter.execute(task)

        self.assertTrue(result.ok)
        self.assertEqual(result.provider, "opencode")
        self.assertEqual(result.conversation_id, "session-123")
        self.assertEqual(result.usage["cost"], 0.01)
        self.assertEqual(result.evidence[0].path, "app.py")

        call = runner.calls[-1]
        argv = call["argv"]
        self.assertEqual(argv[:3], ["opencode", "--pure", "run"])
        self.assertIn("--format", argv)
        self.assertIn("json", argv)
        self.assertIn("--model", argv)
        self.assertIn("opencode/gpt-6-luna", argv)
        self.assertIn("--variant", argv)
        self.assertIn("max", argv)
        self.assertNotIn("--auto", argv)

        self.assertEqual(call["env"]["OPENCODE_CLIENT"], "cortex-relay")
        config = json.loads(call["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"]["edit"], "allow")
        self.assertEqual(config["permission"]["external_directory"], "deny")
        self.assertEqual(config["permission"]["task"], "deny")
        self.assertEqual(config["permission"]["bash"]["*"], "ask")

    def test_permission_policy_is_read_only_when_requested(self):
        adapter = OpenCodeAdapter()
        config = adapter._runtime_config(TaskSpec(objective="Review"))
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertEqual(config["permission"]["webfetch"], "deny")
        self.assertEqual(config["permission"]["websearch"], "deny")

    @patch("cortex_relay.providers.opencode.shutil.which", return_value="/usr/bin/opencode")
    def test_verbose_model_listing_supports_variant_validation(self, _which):
        listing = """
opencode/muse
{
  "variants": {
    "high": {},
    "xhigh": {}
  }
}
opencode/gpt-6-luna
{
  "variants": [
    {"id": "high"},
    {"id": "max"}
  ]
}
""".strip()
        runner = FakeRunner(model_listing=listing)
        adapter = OpenCodeAdapter(runner=runner)
        models = adapter.discover_models(verbose=True)

        self.assertIn("opencode/muse", models)
        self.assertIn("opencode/gpt-6-luna", models)

        accepted = adapter._variant_error(
            TaskSpec(
                objective="Plan",
                provider="opencode",
                model="opencode/muse",
                reasoning="xhigh",
            )
        )
        self.assertIsNone(accepted)

        rejected = adapter._variant_error(
            TaskSpec(
                objective="Plan",
                provider="opencode",
                model="opencode/muse",
                reasoning="max",
            )
        )
        self.assertIn("does not advertise variant", rejected)

    @patch("cortex_relay.providers.opencode.shutil.which", return_value="/usr/bin/opencode")
    def test_default_variant_skips_validation_and_flag(self, _which):
        adapter = OpenCodeAdapter(runner=FakeRunner())
        task = TaskSpec(
            objective="Work",
            provider="opencode",
            model="opencode/model",
            reasoning="default",
            access="workspace_write",
        )
        command = adapter.command_for(task)
        self.assertNotIn("--variant", command)
        self.assertIsNone(adapter._variant_error(task))

    @patch("cortex_relay.providers.opencode.shutil.which", return_value=None)
    def test_missing_binary_returns_unavailable(self, _which):
        result = OpenCodeAdapter().execute(
            TaskSpec(objective="Review", provider="opencode")
        )
        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
