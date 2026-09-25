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

    def test_mapping_preserves_budgets_and_quality_gates(self):
        task = _task_from_mapping(
            {
                "objective": "Implement safely",
                "access": "workspace_write",
                "max_tokens": 12000,
                "max_cost": 0.25,
                "require_changed_files": True,
                "require_tests": True,
                "allowed_paths": ["src/**", "tests/**"],
                "max_failed_tests": 0,
            }
        )
        self.assertEqual(task.budget.max_tokens, 12000)
        self.assertEqual(task.budget.max_cost, 0.25)
        self.assertTrue(task.quality_gates.require_changed_files)
        self.assertTrue(task.quality_gates.require_tests)
        self.assertEqual(task.quality_gates.max_failed_tests, 0)
        self.assertEqual(task.quality_gates.allowed_paths, ("src/**", "tests/**"))

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

    def test_server_instructions_and_tool_descriptions(self):
        from cortex_relay.transports.mcp import create_server
        try:
            server = create_server()
        except RuntimeError as exc:
            if "MCP support is not installed" in str(exc):
                self.skipTest("MCP optional dependency is not installed")
            raise
        self.assertIn("synchronous delegate", server.instructions)
        self.assertIn("Never end a turn with uncompleted async tasks", server.instructions)
        self.assertIn("final_text", server.instructions)
        wait_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "task_wait"
        )
        self.assertIn("up to 30 seconds", wait_tool.description)
        self.assertIn("terminal status", wait_tool.description)
        output_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "task_output"
        )
        self.assertIn("complete child final answer", output_tool.description)


if __name__ == "__main__":
    unittest.main()
