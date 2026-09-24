import tempfile
import tomllib
import unittest

from pathlib import Path
from unittest.mock import patch

from cortex_relay.wizard import (
    ProviderCatalog,
    WizardIO,
    render_toml,
    run_config_editor,
    run_setup,
)


class ScriptedIO(WizardIO):
    def __init__(self, answers):
        self.answers = iter(answers)
        self.output = []
        super().__init__(input_fn=self._input, output_fn=self.output.append)

    def _input(self, _prompt):
        try:
            return next(self.answers)
        except StopIteration as exc:
            raise AssertionError("wizard requested more input than expected") from exc


class WizardTests(unittest.TestCase):
    def test_render_toml_round_trips_nested_runtime_config(self):
        data = {
            "active_preset": "default",
            "profiles": {
                "worker": {
                    "provider": "opencode",
                    "model": "opencode/gpt-6-luna",
                    "reasoning": "max",
                    "access": "workspace_write",
                    "isolate_write": True,
                    "fallbacks": ["backup"],
                    "options": {"validate_variant": True},
                },
                "backup": {
                    "provider": "codex",
                    "model": "gpt-6-luna",
                    "reasoning": "max",
                    "access": "workspace_write",
                },
            },
            "roles": {"implementer": "worker"},
            "presets": {"default": {"roles": {"implementer": "worker"}}},
        }

        rendered = render_toml(data)
        parsed = tomllib.loads(rendered)
        self.assertEqual(parsed, data)

    def test_setup_creates_easy_orchestrator_worker_config(self):
        catalogs = {
            "opencode": ProviderCatalog(
                name="opencode",
                models={
                    "opencode/muse": {"variants": {"xhigh": {}}},
                    "opencode/luna": {"variants": {"max": {}}},
                },
                reasoning_levels=(),
            )
        }
        answers = [
            "",                    # orchestrator provider
            "opencode/muse",       # orchestrator model
            "",                    # xhigh
            "",                    # read_only
            "",                    # worker provider
            "opencode/luna",       # worker model
            "",                    # max
            "",                    # workspace_write
            "", "", "", "", "", "", # role defaults
            "",                    # no fallback
        ]
        io = ScriptedIO(answers)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch(
                "cortex_relay.wizard.discover_catalogs",
                return_value=catalogs,
            ):
                path = run_setup(
                    workspace=workspace,
                    scope="project",
                    io=io,
                )

            with path.open("rb") as handle:
                parsed = tomllib.load(handle)

        self.assertEqual(parsed["profiles"]["orchestrator"]["model"], "opencode/muse")
        self.assertEqual(parsed["profiles"]["orchestrator"]["reasoning"], "xhigh")
        self.assertEqual(parsed["profiles"]["worker"]["model"], "opencode/luna")
        self.assertEqual(parsed["profiles"]["worker"]["reasoning"], "max")
        self.assertEqual(parsed["roles"]["orchestrator"], "orchestrator")
        self.assertEqual(parsed["roles"]["architect"], "orchestrator")
        self.assertEqual(parsed["roles"]["implementer"], "worker")
        self.assertEqual(parsed["roles"]["tester"], "worker")
        self.assertEqual(parsed["roles"]["reviewer"], "worker")
        self.assertEqual(parsed["active_preset"], "default")

    def test_setup_keeps_backup_when_overwriting(self):
        catalogs = {
            "opencode": ProviderCatalog(
                name="opencode",
                models={"opencode/model": {"variants": {"high": {}}}},
                reasoning_levels=(),
            )
        }
        answers = [
            "", "opencode/model", "", "",
            "", "opencode/model", "", "",
            "", "", "", "", "", "",
            "",
        ]
        io = ScriptedIO(answers)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / ".cortex-relay" / "config.toml"
            target.parent.mkdir(parents=True)
            target.write_text(
                '[profiles.old]\nprovider = "codex"\n\n[roles]\nreviewer = "old"\n',
                encoding="utf-8",
            )

            with patch(
                "cortex_relay.wizard.discover_catalogs",
                return_value=catalogs,
            ):
                run_setup(workspace=workspace, io=io)

            backup = target.with_suffix(".toml.bak")
            self.assertTrue(backup.exists())
            self.assertIn("[profiles.old]", backup.read_text(encoding="utf-8"))

    def test_config_editor_reassigns_role_and_saves_backup(self):
        initial = """
[profiles.orchestrator]
provider = "opencode"
model = "opencode/muse"
reasoning = "xhigh"
access = "read_only"

[profiles.worker]
provider = "opencode"
model = "opencode/luna"
reasoning = "max"
access = "workspace_write"

[roles]
orchestrator = "orchestrator"
reviewer = "worker"
""".strip()
        io = ScriptedIO(
            [
                "assign role",
                "reviewer",
                "orchestrator",
                "save and exit",
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            target = workspace / ".cortex-relay" / "config.toml"
            target.parent.mkdir(parents=True)
            target.write_text(initial, encoding="utf-8")

            with patch(
                "cortex_relay.wizard.discover_catalogs",
                return_value={},
            ):
                run_config_editor(
                    workspace=workspace,
                    io=io,
                )

            with target.open("rb") as handle:
                parsed = tomllib.load(handle)

            self.assertEqual(parsed["roles"]["reviewer"], "orchestrator")
            self.assertTrue(target.with_suffix(".toml.bak").exists())

    def test_config_without_file_starts_setup(self):
        io = ScriptedIO([])
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with patch("cortex_relay.wizard.run_setup", return_value=workspace / "x") as setup:
                result = run_config_editor(workspace=workspace, io=io)

        self.assertEqual(result, workspace / "x")
        setup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
