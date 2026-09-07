import tempfile
import unittest
from pathlib import Path

from cortex_relay.configurator import ConfigValues
from cortex_relay.installer import install


VALUES = ConfigValues(
    orchestrator_model="gpt-6-astra",
    orchestrator_effort="low",
    worker_model="gpt-5.6-luna",
    worker_effort="xhigh",
    max_threads=4,
)


class InstallerTests(unittest.TestCase):
    def test_project_install_creates_expected_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = install(scope="project", project_dir=root, values=VALUES)
            self.assertTrue(result.config_path.exists())
            self.assertTrue(result.instructions_path.exists())
            self.assertEqual(len(result.agent_paths), 5)
            self.assertTrue(all(path.exists() for path in result.agent_paths))

    def test_reinstall_backs_up_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install(scope="project", project_dir=root, values=VALUES)
            result = install(scope="project", project_dir=root, values=VALUES)
            self.assertGreaterEqual(len(result.backups), 7)


if __name__ == "__main__":
    unittest.main()
