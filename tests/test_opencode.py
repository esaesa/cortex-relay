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
            "final_text": "Implemented the bounded change with complete details.",
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
        self.assertEqual(
            result.final_text,
            "Implemented the bounded change with complete details.",
        )
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

    def test_worker_disables_inherited_cortex_relay_mcp(self):
        inherited = json.dumps(
            {
                "mcp": {
                    "servers": {
                        "cortex-relay": {
                            "type": "local",
                            "command": ["cortex-relay", "serve", "--transport", "mcp"],
                        },
                        "other": {
                            "type": "remote",
                            "url": "https://example.invalid/mcp",
                        },
                    }
                }
            }
        )
        with patch.dict(
            "os.environ",
            {"OPENCODE_CONFIG_CONTENT": inherited},
            clear=False,
        ):
            config = OpenCodeAdapter()._runtime_config(TaskSpec(objective="Review"))

        self.assertTrue(
            config["mcp"]["servers"]["cortex-relay"]["disabled"]
        )
        self.assertNotIn(
            "disabled",
            config["mcp"]["servers"]["other"],
        )

    def test_permission_policy_is_read_only_when_requested(self):
        adapter = OpenCodeAdapter()
        config = adapter._runtime_config(TaskSpec(objective="Review"))
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertEqual(config["permission"]["webfetch"], "deny")
        self.assertEqual(config["permission"]["websearch"], "deny")
        self.assertEqual(config["permission"]["todowrite"], "deny")
        self.assertEqual(config["permission"]["todoread"], "deny")

    def test_worker_disables_external_mcp_servers_from_user_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "opencode"
            config_dir.mkdir(parents=True)
            (config_dir / "opencode.json").write_text(
                json.dumps(
                    {
                        "mcp": {
                            "chrome-devtools": {"type": "local", "command": ["node", "server.js"]},
                            "playwright": {"type": "local", "command": ["node", "pw.js"]},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": tmp}, clear=False):
                config = OpenCodeAdapter()._runtime_config(TaskSpec(objective="Review"))

        self.assertEqual(config["mcp"]["chrome-devtools"], {"enabled": False})
        self.assertEqual(config["mcp"]["playwright"], {"enabled": False})

    def test_usage_aggregates_tokens_and_cost_across_all_steps(self):
        events = [
            {
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "input": 5500,
                        "output": 80,
                        "reasoning": 36,
                        "cache": {"read": 113, "write": 0},
                        "total": 5729,
                    },
                    "cost": 0.002,
                    "modelID": "muse-spark",
                    "providerID": "opencode",
                    "reason": "tool-calls",
                },
            },
            {
                "type": "step_finish",
                "part": {
                    "tokens": {
                        "input": 815,
                        "output": 130,
                        "reasoning": 88,
                        "cache": {"read": 5489, "write": 0},
                        "total": 6522,
                    },
                    "cost": 0.003,
                    "modelID": "muse-spark",
                    "providerID": "opencode",
                    "reason": "stop",
                },
            },
        ]
        usage = OpenCodeAdapter._usage(events)
        self.assertEqual(usage["tokens"]["input"], 6315)
        self.assertEqual(usage["tokens"]["output"], 210)
        self.assertEqual(usage["tokens"]["reasoning"], 124)
        self.assertEqual(usage["tokens"]["cache"]["read"], 5602)
        self.assertEqual(usage["tokens"]["total"], 12251)
        self.assertAlmostEqual(usage["cost"], 0.005)
        self.assertEqual(usage["reason"], "stop")


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

    def test_host_command_uses_variant_selector_and_injects_cortex_mcp(self):
        adapter = OpenCodeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(
                objective="Orchestrate",
                role="orchestrator",
                provider="opencode",
                model="opencode/muse",
                reasoning="xhigh",
                workspace=Path(tmp),
                access="read_only",
                profile="muse",
                metadata={"session_id": "session-test"},
            )
            command = adapter.host_command(task)
            env = adapter.host_environment(task)

        self.assertEqual(command[0], "opencode")
        self.assertIn("--model", command)
        self.assertIn("opencode/muse", command)
        model_index = command.index("--model") + 1
        self.assertNotIn("#", command[model_index])
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        server = config["mcp"]["servers"]["cortex-relay"]
        self.assertEqual(server["type"], "local")
        self.assertIn("cortex_relay.cli", server["command"])
        self.assertEqual(config["permission"]["task"], "deny")
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertEqual(env["OPENCODE_CLIENT"], "cortex-relay-orchestrator")
        self.assertEqual(env["CORTEX_RELAY_SESSION_ID"], "session-test")
        self.assertEqual(env["CORTEX_RELAY_HOST_PROFILE"], "muse")
        self.assertEqual(env["CORTEX_RELAY_HOST_MODEL"], "opencode/muse")
        self.assertEqual(env["CORTEX_RELAY_HOST_REASONING"], "xhigh")

    @patch("cortex_relay.providers.opencode.shutil.which", return_value=None)
    def test_missing_binary_returns_unavailable(self, _which):
        result = OpenCodeAdapter().execute(
            TaskSpec(objective="Review", provider="opencode")
        )
        self.assertEqual(result.status, "unavailable")


if __name__ == "__main__":
    unittest.main()
