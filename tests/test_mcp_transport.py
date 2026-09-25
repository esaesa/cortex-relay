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
        self.assertIn("cross-provider agent control plane", server.instructions)
        self.assertIn("persistent provider-native agent sessions", server.instructions)
        self.assertIn("agent_start_async for parallel direct specialists", server.instructions)
        self.assertIn("closed-end execution", server.instructions)
        self.assertIn("provider-native child", server.instructions)
        self.assertIn("complete worker final result is authoritative", server.instructions)
        self.assertNotIn("Use synchronous delegate", server.instructions)
        self.assertNotIn("poll task_wait", server.instructions)
        wait_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "task_wait"
        )
        self.assertIn("up to 30 seconds", wait_tool.description)
        self.assertIn("terminal status", wait_tool.description)
        output_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "task_output"
        )
        self.assertIn("complete child final answer", output_tool.description)
        delegate_async_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "delegate_async"
        )
        self.assertIn("workflow task", delegate_async_tool.description)
        self.assertIn("agent_start_async", delegate_async_tool.description)
        agent_start_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_start"
        )
        self.assertIn("direct persistent/resumable agent session", agent_start_tool.description)
        self.assertIn("access=auto", agent_start_tool.description)
        async_start_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_start_async"
        )
        self.assertIn("parallel direct specialists", async_start_tool.description)
        self.assertIn("access=auto", async_start_tool.description)
        agent_wait_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_wait"
        )
        self.assertIn("at most five seconds", agent_wait_tool.description)
        agent_result_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_result"
        )
        self.assertIn("latest durable completed-turn result", agent_result_tool.description)
        agent_watch_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_watch"
        )
        self.assertIn("surface visible progress", agent_watch_tool.description)
        self.assertIn("updates[]", agent_watch_tool.description)
        self.assertIn("do not replace it", agent_watch_tool.description)
        agent_events_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_events"
        )
        self.assertIn("semantic agent events", agent_events_tool.description)
        agent_messages_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_messages"
        )
        self.assertIn("monotonic sequence cursor", agent_messages_tool.description)
        agent_cancel_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_cancel"
        )
        self.assertIn("provider-backed cancellation", agent_cancel_tool.description)
        agent_send_tool = next(
            t for t in server._tool_manager.list_tools() if t.name == "agent_send"
        )
        self.assertIn("resumable provider session", agent_send_tool.description)


if __name__ == "__main__":
    unittest.main()
