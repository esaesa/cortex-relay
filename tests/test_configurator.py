import unittest

from cortex_relay.configurator import ConfigValues, upsert_codex_config, upsert_managed_markdown


VALUES = ConfigValues(
    orchestrator_model="gpt-6-astra",
    orchestrator_effort="low",
    worker_model="gpt-5.6-luna",
    worker_effort="xhigh",
    max_threads=6,
)


class ConfiguratorTests(unittest.TestCase):
    def test_preserves_unrelated_config_and_updates_agents(self):
        existing = '''# existing\napproval_policy = "on-request"\n\n[agents]\nenabled = false\nmax_concurrent_threads_per_session = 2\n\n[mcp_servers.docs]\nurl = "https://example.invalid"\n'''
        result = upsert_codex_config(existing, VALUES)
        self.assertIn('approval_policy = "on-request"', result)
        self.assertIn('[mcp_servers.docs]', result)
        self.assertIn('model = "gpt-6-astra"', result)
        self.assertIn('model_reasoning_effort = "low"', result)
        self.assertIn('enabled = true', result)
        self.assertIn('max_concurrent_threads_per_session = 6', result)
        self.assertIn('default_subagent_model = "gpt-5.6-luna"', result)
        self.assertIn('default_subagent_reasoning_effort = "xhigh"', result)

    def test_adds_agents_section_when_missing(self):
        result = upsert_codex_config('model_verbosity = "medium"\n', VALUES)
        self.assertIn('[agents]', result)
        self.assertIn('model_verbosity = "medium"', result)

    def test_managed_markdown_is_idempotent(self):
        first = upsert_managed_markdown("# Project\n", "## CortexRelay\n- delegate")
        second = upsert_managed_markdown(first, "## CortexRelay\n- delegate")
        self.assertEqual(first, second)
        self.assertEqual(first.count("<!-- cortex-relay:start -->"), 1)


if __name__ == "__main__":
    unittest.main()
