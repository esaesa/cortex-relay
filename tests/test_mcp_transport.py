import unittest

from cortex_relay.transports.mcp import _task_from_mapping, _task_from_values


class MCPTransportMappingTests(unittest.TestCase):
    def test_mapping_preserves_profile_and_preset(self):
        task = _task_from_mapping(
            {
                "objective": "Review auth",
                "role": "reviewer",
                "profile": "zen-luna",
                "preset": "cheap",
                "workspace": ".",
                "access": "read_only",
            }
        )
        self.assertEqual(task.profile, "zen-luna")
        self.assertEqual(task.preset, "cheap")
        self.assertEqual(task.role, "reviewer")

    def test_write_mapping_enables_requested_isolation(self):
        task = _task_from_values(
            objective="Implement",
            role="implementer",
            profile="zen-luna",
            preset=None,
            provider="auto",
            workspace=".",
            access="workspace_write",
            reasoning="high",
            model=None,
            acceptance_criteria=[],
            timeout_seconds=300,
            isolate_write=True,
        )
        self.assertTrue(task.isolate_write)

    def test_invalid_access_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "access"):
            _task_from_values(
                objective="Bad",
                role="reviewer",
                profile=None,
                preset=None,
                provider="auto",
                workspace=".",
                access="admin",
                reasoning="high",
                model=None,
                acceptance_criteria=[],
                timeout_seconds=300,
                isolate_write=False,
            )


if __name__ == "__main__":
    unittest.main()
