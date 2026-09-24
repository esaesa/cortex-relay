import unittest

from cortex_relay.providers.result_schema import RESULT_SCHEMA


class ResultSchemaTests(unittest.TestCase):
    def test_object_schema_is_strict_ready(self):
        self.assertEqual(
            set(RESULT_SCHEMA["required"]),
            set(RESULT_SCHEMA["properties"]),
        )
        self.assertFalse(RESULT_SCHEMA["additionalProperties"])

    def test_evidence_items_require_every_declared_property(self):
        items = RESULT_SCHEMA["properties"]["evidence"]["items"]
        self.assertEqual(
            set(items["required"]),
            set(items["properties"]),
        )
        self.assertFalse(items["additionalProperties"])
        for name in ("path", "symbol", "severity"):
            self.assertEqual(items["properties"][name]["type"], ["string", "null"])


if __name__ == "__main__":
    unittest.main()
