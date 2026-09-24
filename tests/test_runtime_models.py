import tempfile
import unittest
from pathlib import Path

from cortex_relay.core.models import Evidence, TaskResult, TaskSpec


class RuntimeModelTests(unittest.TestCase):
    def test_task_normalizes_workspace_and_criteria(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = TaskSpec(
                objective="Review auth",
                workspace=Path(tmp),
                acceptance_criteria=("  find regressions  ", ""),
            )
            self.assertEqual(task.workspace, Path(tmp).resolve())
            self.assertEqual(task.acceptance_criteria, ("find regressions",))

    def test_invalid_task_rejected(self):
        with self.assertRaises(ValueError):
            TaskSpec(objective="")
        with self.assertRaises(ValueError):
            TaskSpec(objective="x", timeout_seconds=0)

    def test_result_serialization(self):
        result = TaskResult(
            status="success",
            provider="fake",
            summary="ok",
            evidence=(Evidence(finding="issue", path="a.py"),),
        )
        payload = result.to_dict()
        self.assertTrue(result.ok)
        self.assertEqual(payload["evidence"][0]["path"], "a.py")


if __name__ == "__main__":
    unittest.main()
