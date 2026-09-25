import unittest

from cortex_relay.core.models import TaskBudget
from cortex_relay.runtime.progress import ProgressEvent
from cortex_relay.runtime.supervision import SupervisionTracker


class SupervisionTrackerTests(unittest.TestCase):
    def test_tool_and_repeat_limits(self):
        tracker = SupervisionTracker(
            TaskBudget(max_tool_calls=3, max_repeated_calls=2)
        )
        event = ProgressEvent(
            task_id="task-1",
            phase="tool",
            state="active",
            activity="Reading file",
            tool="view_file",
            path="src/example.py",
        )

        self.assertIsNone(tracker.observe(event))
        self.assertIsNone(tracker.observe(event))
        violation = tracker.observe(event)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertIn("repeated tool-call budget exceeded", violation.reason)
        self.assertEqual(violation.termination_reason, "repeated_tool_stall")

    def test_child_agent_limit_counts_unique_children(self):
        tracker = SupervisionTracker(TaskBudget(max_child_agents=1))
        first = ProgressEvent(
            task_id="task-1",
            phase="tool",
            state="active",
            activity="Delegating",
            subagents=({"provider_session_id": "child-1"},),
        )
        repeated = ProgressEvent(
            task_id="task-1",
            phase="response",
            state="active",
            activity="Child update",
            subagents=({"provider_session_id": "child-1"},),
        )
        second = ProgressEvent(
            task_id="task-1",
            phase="response",
            state="active",
            activity="Second child",
            subagents=({"provider_session_id": "child-2"},),
        )

        self.assertIsNone(tracker.observe(first))
        self.assertIsNone(tracker.observe(repeated))
        violation = tracker.observe(second)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertIn("child-agent budget exceeded", violation.reason)
        self.assertEqual(violation.termination_reason, "child_agent_budget")

    def test_token_limit_uses_total_or_input_output_fallback(self):
        tracker = SupervisionTracker(TaskBudget(max_tokens=100))
        total = ProgressEvent(
            task_id="task-1",
            phase="response",
            state="active",
            activity="Working",
            total_tokens=101,
        )
        violation = tracker.observe(total)
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertEqual(violation.termination_reason, "token_budget")

        fallback = SupervisionTracker(TaskBudget(max_tokens=100))
        split = ProgressEvent(
            task_id="task-2",
            phase="response",
            state="active",
            activity="Working",
            input_tokens=60,
            output_tokens=41,
        )
        violation = fallback.observe(split)
        self.assertIsNotNone(violation)


if __name__ == "__main__":
    unittest.main()
