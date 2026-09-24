import json
import os
import subprocess
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from cortex_relay.core.models import TaskSpec
from cortex_relay.providers.opencode import OpenCodeAdapter


class OpenCodeHostLaunchTests(unittest.TestCase):
    def test_launch_uses_isolated_variant_without_changing_user_state(self):
        with tempfile.TemporaryDirectory() as user_state_home:
            user_state = Path(user_state_home) / "opencode" / "model.json"
            user_state.parent.mkdir()
            user_state.write_text(
                json.dumps({"variant": {"opencode/muse": "xhigh"}}),
                encoding="utf-8",
            )
            task = TaskSpec(
                objective="Orchestrate",
                provider="opencode",
                model="opencode/muse",
                reasoning="high",
                workspace=Path(user_state_home),
                metadata={"profile_options": {"validate_variant": False}},
            )
            observed = {}

            def capture_child(argv, *, cwd, env, check):
                observed["argv"] = argv
                observed["state_home"] = env["XDG_STATE_HOME"]
                model_state = Path(env["XDG_STATE_HOME"]) / "opencode" / "model.json"
                observed["variant"] = json.loads(model_state.read_text(encoding="utf-8"))[
                    "variant"
                ][task.model]
                self.assertEqual(cwd, task.workspace)
                self.assertFalse(check)
                return subprocess.CompletedProcess(argv, 0)

            with patch.dict(os.environ, {"XDG_STATE_HOME": user_state_home}):
                with patch(
                    "cortex_relay.providers.opencode.shutil.which",
                    return_value="opencode",
                ):
                    with patch(
                        "cortex_relay.providers.opencode.subprocess.run",
                        side_effect=capture_child,
                    ):
                        result = OpenCodeAdapter().launch_host(task)

            self.assertEqual(result, 0)
            model_index = observed["argv"].index("--model") + 1
            self.assertEqual(observed["argv"][model_index], task.model)
            self.assertEqual(observed["variant"], "high")
            self.assertNotEqual(observed["state_home"], user_state_home)
            self.assertFalse(Path(observed["state_home"]).exists())
            self.assertEqual(
                json.loads(user_state.read_text(encoding="utf-8"))["variant"][task.model],
                "xhigh",
            )


if __name__ == "__main__":
    unittest.main()
