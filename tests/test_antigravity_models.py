import unittest

from cortex_relay.providers.antigravity import AntigravityAdapter


class AntigravityModelDiscoveryTests(unittest.TestCase):
    def test_parse_model_listing_from_official_human_readable_shape(self):
        stdout = """
gemini-3.8-flash-high     Gemini 3.8 Flash (High)
gemini-3.8-flash-medium   Gemini 3.8 Flash (Medium)
gemini-3.1-pro-high       Gemini 3.1 Pro (High)
claude-sonnet-4-6         Claude Sonnet 4.6 (Thinking)
""".strip()

        models = AntigravityAdapter._parse_model_listing(stdout)

        self.assertEqual(
            models["gemini-3.8-flash-high"]["label"],
            "Gemini 3.8 Flash (High)",
        )
        self.assertEqual(
            models["claude-sonnet-4-6"]["label"],
            "Claude Sonnet 4.6 (Thinking)",
        )

    def test_parse_model_listing_tolerates_bullets_and_ignores_headers(self):
        stdout = """
Available models:
- gemini-3.8-flash-low Gemini 3.8 Flash (Low)
* gpt-oss-120b-medium GPT-OSS 120B (Medium)
""".strip()

        models = AntigravityAdapter._parse_model_listing(stdout)

        self.assertEqual(
            set(models),
            {"gemini-3.8-flash-low", "gpt-oss-120b-medium"},
        )

    def test_empty_listing_returns_empty_catalog(self):
        self.assertEqual(AntigravityAdapter._parse_model_listing(""), {})


if __name__ == "__main__":
    unittest.main()
