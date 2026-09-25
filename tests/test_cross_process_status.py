import json
import os
import subprocess
import sys
import tempfile
import unittest

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cortex_relay.cli import main
from cortex_relay.runtime.agent_store import AgentStore


class CrossProcessStatusTests(unittest.TestCase):
    def _running_agent(self, state: Path, workspace: Path) -> str:
        store = AgentStore(state)
        session = store.create(
            provider="fake",
            workspace=workspace,
            task_id=None,
            model="model",
            reasoning="high",
            access="read_only",
            role="explorer",
            objective="cross-process status",
        )
        store.update(session.session_id, state="running")
        return session.session_id

    def test_fresh_cli_process_sees_direct_agent_from_canonical_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            workspace = root / "repo"
            workspace.mkdir()
            session_id = self._running_agent(state, workspace)

            env = dict(os.environ)
            env["CORTEX_RELAY_STATE_DIR"] = str(state)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cortex_relay.cli",
                    "status",
                    "--workspace",
                    str(workspace),
                    "--active-only",
                    "--json",
                ],
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                [item["session_id"] for item in payload["agents"]],
                [session_id],
            )
            self.assertEqual(payload["agent_summary"]["running"], 1)
            self.assertEqual(Path(payload["state_directory"]).parent, state.resolve())

    def test_status_watch_renders_direct_agent_from_canonical_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            workspace = root / "repo"
            workspace.mkdir()
            self._running_agent(state, workspace)

            output = StringIO()
            with patch.dict(
                os.environ,
                {"CORTEX_RELAY_STATE_DIR": str(state)},
                clear=False,
            ):
                with patch(
                    "cortex_relay.cli.time.sleep",
                    side_effect=KeyboardInterrupt,
                ):
                    with redirect_stdout(output):
                        code = main(
                            [
                                "status",
                                "--workspace",
                                str(workspace),
                                "--watch",
                                "--interval",
                                "0.2",
                            ]
                        )

            self.assertEqual(code, 0)
            rendered = output.getvalue()
            self.assertIn("CortexRelay live status", rendered)
            self.assertIn("Agents: running 1", rendered)
            self.assertIn("explorer", rendered)
            self.assertNotIn("No active CortexRelay requests", rendered)


if __name__ == "__main__":
    unittest.main()
