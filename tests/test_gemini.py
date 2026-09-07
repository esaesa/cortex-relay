import json
import tempfile
import unittest
from pathlib import Path

from cortex_relay.gemini import GeminiConfigValues, ORCHESTRATOR_ALIAS, render_gemini_agent_file, upsert_gemini_settings
from cortex_relay.installer import install
from cortex_relay.templates import AGENTS


VALUES = GeminiConfigValues(
    orchestrator_model="gemini-3.8-flash",
    orchestrator_thinking="high",
    worker_model="gemini-3.8-flash",
)


class GeminiConfiguratorTests(unittest.TestCase):
    def test_preserves_unrelated_settings_and_routes_thinking_by_role(self):
        existing = json.dumps({"general": {"vimMode": True}, "agents": {"browser": {"headless": True}}})
        result = json.loads(upsert_gemini_settings(existing, VALUES))

        self.assertTrue(result["general"]["vimMode"])
        self.assertTrue(result["agents"]["browser"]["headless"])
        self.assertEqual(result["model"]["name"], ORCHESTRATOR_ALIAS)

        alias = result["modelConfigs"]["customAliases"][ORCHESTRATOR_ALIAS]
        self.assertEqual(alias["modelConfig"]["model"], "gemini-3.8-flash")
        self.assertEqual(
            alias["modelConfig"]["generateContentConfig"]["thinkingConfig"]["thinkingLevel"],
            "HIGH",
        )

        overrides = result["agents"]["overrides"]
        expected = {
            "explorer": "MEDIUM",
            "architect": "HIGH",
            "implementer": "HIGH",
            "tester": "LOW",
            "reviewer": "HIGH",
        }
        for name, level in expected.items():
            self.assertEqual(overrides[name]["modelConfig"]["model"], "gemini-3.8-flash")
            self.assertEqual(
                overrides[name]["modelConfig"]["generateContentConfig"]["thinkingConfig"]["thinkingLevel"],
                level,
            )

    def test_rejects_non_object_settings(self):
        with self.assertRaises(ValueError):
            upsert_gemini_settings("[]", VALUES)

    def test_agent_definition_uses_supported_markdown_frontmatter(self):
        content = render_gemini_agent_file(AGENTS[0], model="gemini-3.8-flash")
        self.assertTrue(content.startswith("---\n"))
        self.assertIn("name: explorer", content)
        self.assertIn('model: "gemini-3.8-flash"', content)
        self.assertIn("max_turns: 30", content)


class GeminiInstallerTests(unittest.TestCase):
    def test_project_install_creates_gemini_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = install(provider="gemini", scope="project", project_dir=root, values=VALUES)
            self.assertEqual(result.provider, "gemini")
            self.assertEqual(result.config_path, root / ".gemini" / "settings.json")
            self.assertEqual(result.instructions_path, root / "GEMINI.md")
            self.assertTrue(result.config_path.exists())
            self.assertTrue(result.instructions_path.exists())
            self.assertEqual(len(result.agent_paths), 5)
            self.assertTrue(all(path.suffix == ".md" and path.exists() for path in result.agent_paths))

    def test_reinstall_backs_up_gemini_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            install(provider="gemini", scope="project", project_dir=root, values=VALUES)
            result = install(provider="gemini", scope="project", project_dir=root, values=VALUES)
            self.assertGreaterEqual(len(result.backups), 7)


if __name__ == "__main__":
    unittest.main()
