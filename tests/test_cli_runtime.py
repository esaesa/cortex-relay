import io
import unittest

from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from cortex_relay.cli import main
from cortex_relay.core.models import TaskResult


class FakeProfileResolver:
    def load(self, _workspace):
        from cortex_relay.core.profiles import runtime_config_from_mapping

        return runtime_config_from_mapping(
            {
                "profiles": {
                    "muse": {
                        "provider": "opencode",
                        "model": "opencode/muse",
                        "reasoning": "xhigh",
                        "access": "read_only",
                    }
                },
                "roles": {"orchestrator": "muse"},
            }
        )


class FakeRegistry:
    def __init__(self):
        self.last_task = None
        self.profiles = FakeProfileResolver()

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

    def profile_config(self, _workspace, *, preset=None):
        return {
            "active_preset": preset,
            "sources": ["/repo/.cortex-relay/config.toml"],
            "roles": {"reviewer": "zen-luna"},
            "profiles": {
                "zen-luna": {
                    "provider": "opencode",
                    "model": "opencode/gpt-6-luna",
                    "reasoning": "max",
                    "access": "workspace_write",
                    "isolate_write": True,
                    "fallbacks": [],
                    "fallback_on": ["unavailable"],
                    "billing_class": "zen-cheap",
                    "options": {},
                }
            },
            "presets": {},
        }

    def execute(self, task):
        self.last_task = task
        return TaskResult(status="success", provider="fake", summary=task.objective)


class FakeOpenCodeAdapter:
    def capabilities(self):
        class Capabilities:
            available = True
            detail = "fake"

        return Capabilities()

    def discover_models(self, *, refresh=False, verbose=False):
        return {
            "opencode/muse": {"variants": {"xhigh": {}}},
            "opencode/gpt-6-luna": {"variants": {"max": {}}},
        }


class CLIRuntimeTests(unittest.TestCase):
    def test_providers_command(self):
        registry = FakeRegistry()
        output = io.StringIO()
        with patch("cortex_relay.cli.default_registry", return_value=registry):
            with redirect_stdout(output):
                code = main(["providers"])
        self.assertEqual(code, 0)
        self.assertIn("fake: available", output.getvalue())

    def test_delegate_accepts_new_codex_efforts(self):
        registry = FakeRegistry()
        output = io.StringIO()
        with patch("cortex_relay.cli.default_registry", return_value=registry):
            with redirect_stdout(output):
                code = main(
                    [
                        "delegate",
                        "Review auth",
                        "--provider",
                        "codex",
                        "--model",
                        "gpt-6-luna",
                        "--reasoning",
                        "max",
                    ]
                )
        self.assertEqual(code, 0)

    def test_delegate_carries_profile_and_preset(self):
        registry = FakeRegistry()
        output = io.StringIO()
        with patch("cortex_relay.cli.default_registry", return_value=registry):
            with redirect_stdout(output):
                code = main(
                    [
                        "delegate",
                        "Review auth",
                        "--profile",
                        "zen-luna",
                        "--preset",
                        "cheap",
                    ]
                )
        self.assertEqual(code, 0)
        self.assertEqual(registry.last_task.profile, "zen-luna")
        self.assertEqual(registry.last_task.preset, "cheap")

    def test_profiles_command(self):
        registry = FakeRegistry()
        output = io.StringIO()
        with patch("cortex_relay.cli.default_registry", return_value=registry):
            with redirect_stdout(output):
                code = main(["profiles", "--preset", "cheap"])
        self.assertEqual(code, 0)
        rendered = output.getvalue()
        self.assertIn("reviewer: zen-luna", rendered)
        self.assertIn("zen-luna: opencode / opencode/gpt-6-luna / max", rendered)

    def test_models_command_uses_opencode_discovery(self):
        output = io.StringIO()
        with patch(
            "cortex_relay.providers.opencode.OpenCodeAdapter",
            return_value=FakeOpenCodeAdapter(),
        ):
            with redirect_stdout(output):
                code = main(["models", "--provider", "opencode", "--refresh"])
        self.assertEqual(code, 0)
        self.assertIn("opencode/muse", output.getvalue())
        self.assertIn("opencode/gpt-6-luna", output.getvalue())

    @patch("cortex_relay.transports.a2a.run_a2a")
    def test_serve_a2a_builds_server_profile_policy(self, run_a2a):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "serve",
                    "--transport",
                    "a2a",
                    "--a2a-profile",
                    "zen-luna",
                    "--a2a-preset",
                    "cheap",
                    "--a2a-provider",
                    "codex",
                    "--a2a-model",
                    "gpt-6-luna",
                    "--a2a-reasoning",
                    "max",
                ]
            )
        self.assertEqual(code, 0)
        policy = run_a2a.call_args.kwargs["policy"]
        self.assertEqual(policy.profile, "zen-luna")
        self.assertEqual(policy.preset, "cheap")
        self.assertEqual(policy.provider, "codex")
        self.assertEqual(policy.model, "gpt-6-luna")
        self.assertEqual(policy.reasoning, "max")
        self.assertIn("/.well-known/agent-card.json", output.getvalue())

    def test_launch_uses_configured_opencode_orchestrator(self):
        registry = FakeRegistry()

        class FakeHostAdapter:
            last_task = None

            def launch_host(self, task):
                self.last_task = task
                FakeHostAdapter.last_task = task
                return 0

        with patch("cortex_relay.cli.default_registry", return_value=registry):
            with patch("cortex_relay.cli.importlib.util.find_spec", return_value=object()):
                with patch(
                    "cortex_relay.providers.opencode.OpenCodeAdapter",
                    return_value=FakeHostAdapter(),
                ):
                    code = main(["launch", "--role", "orchestrator"])

        self.assertEqual(code, 0)
        self.assertEqual(FakeHostAdapter.last_task.profile, "muse")
        self.assertEqual(FakeHostAdapter.last_task.model, "opencode/muse")
        self.assertEqual(FakeHostAdapter.last_task.reasoning, "xhigh")

    def test_delegate_json(self):
        registry = FakeRegistry()
        output = io.StringIO()
        with patch("cortex_relay.cli.default_registry", return_value=registry):
            with redirect_stdout(output):
                code = main(["delegate", "Review auth", "--json"])
        self.assertEqual(code, 0)
        self.assertIn('"status": "success"', output.getvalue())

    def test_models_unavailable_returns_nonzero(self):
        class MissingOpenCode:
            def capabilities(self):
                class Capabilities:
                    available = False
                    detail = "opencode was not found on PATH"

                return Capabilities()

        stderr = io.StringIO()
        with patch(
            "cortex_relay.providers.opencode.OpenCodeAdapter",
            return_value=MissingOpenCode(),
        ):
            with redirect_stderr(stderr):
                code = main(["models"])
        self.assertEqual(code, 1)
        self.assertIn("not found", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
