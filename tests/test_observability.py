import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.observability import RunStore, render_dashboard, summarize_usage


class ObservabilityTests(unittest.TestCase):
    def test_task_lifecycle_persists_route_usage_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            store = RunStore(root)

            with patch.dict(
                "os.environ",
                {
                    "CORTEX_RELAY_SESSION_ID": "session-abc",
                    "CORTEX_RELAY_HOST_PROFILE": "muse",
                    "CORTEX_RELAY_HOST_MODEL": "opencode/muse",
                    "CORTEX_RELAY_HOST_REASONING": "xhigh",
                },
                clear=False,
            ):
                task_id = store.start_task(
                    TaskSpec(
                        objective="Implement authentication fix",
                        role="implementer",
                        workspace=workspace,
                        access="workspace_write",
                    )
                )

            store.update_task(
                workspace,
                task_id,
                status="running",
                profile="luna",
                provider="opencode",
                model="opencode/gpt-6-luna",
                reasoning="max",
                worktree_path=str(workspace / ".worktree"),
            )
            record = store.complete_task(
                workspace,
                task_id,
                TaskResult(
                    status="success",
                    provider="opencode",
                    model="opencode/gpt-6-luna",
                    summary="Implemented.",
                    final_text="Implemented the authentication fix in full detail.\nSecond line.",
                    changed_files=("auth.py",),
                    tests=("18 passed",),
                    duration_seconds=12.5,
                    usage={"tokens": {"input": 1200, "output": 300}, "cost": 0.004},
                    metadata={
                        "profile": "luna",
                        "worktree_path": str(workspace / ".worktree"),
                        "worktree_branch": "cortex/implementer",
                    },
                ),
            )

            self.assertEqual(record["session_id"], "session-abc")
            self.assertEqual(record["host_profile"], "muse")
            self.assertEqual(record["profile"], "luna")
            self.assertEqual(record["status"], "success")
            self.assertEqual(record["usage_summary"]["total_tokens"], 1500)
            self.assertEqual(record["usage_summary"]["cost"], 0.004)
            self.assertEqual(record["changed_files"], ["auth.py"])
            self.assertEqual(record["tests"], ["18 passed"])
            self.assertEqual(record["output_chars"], len("Implemented the authentication fix in full detail.\nSecond line."))
            output = store.get_output(workspace, task_id, offset=0, max_chars=20)
            self.assertEqual(output["text"], "Implemented the auth")
            self.assertFalse(output["complete"])
            remainder = store.get_output(
                workspace,
                task_id,
                offset=output["next_offset"],
                max_chars=200,
            )
            self.assertTrue(remainder["complete"])
            self.assertEqual(
                output["text"] + remainder["text"],
                "Implemented the authentication fix in full detail.\nSecond line.",
            )

            snapshot = store.snapshot(workspace)
            self.assertEqual(snapshot["summary"]["success"], 1)
            self.assertEqual(snapshot["summary"]["total_tokens"], 1500)
            self.assertEqual(snapshot["summary"]["cost"], 0.004)

    def test_session_lifecycle_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            store = RunStore(root)

            session_id = store.start_session(
                workspace,
                profile="muse",
                provider="opencode",
                model="opencode/muse",
                reasoning="xhigh",
            )
            running = store.list_sessions(workspace)
            self.assertEqual(running[0]["status"], "running")
            self.assertEqual(running[0]["session_id"], session_id)

            store.end_session(workspace, session_id, exit_code=0)
            completed = store.list_sessions(workspace)
            self.assertEqual(completed[0]["status"], "success")
            self.assertEqual(completed[0]["exit_code"], 0)

    def test_dashboard_shows_full_host_worker_route_and_metrics(self):
        snapshot = {
            "workspace": "D:/src/project",
            "sessions": [
                {
                    "profile": "muse",
                    "provider": "opencode",
                    "model": "opencode/muse",
                    "reasoning": "xhigh",
                    "status": "running",
                }
            ],
            "summary": {
                "active": 1,
                "success": 1,
                "failed": 0,
                "total_tokens": 1500,
                "cost": 0.004,
            },
            "tasks": [
                {
                    "status": "running",
                    "host_profile": "muse",
                    "role": "implementer",
                    "profile": "luna",
                    "provider": "opencode",
                    "model": "opencode/gpt-6-luna",
                    "reasoning": "max",
                    "objective": "Implement authentication fix",
                    "started_at": "2026-09-24T15:00:00+00:00",
                    "usage_summary": {},
                    "tests": [],
                    "changed_files": [],
                    "worktree_path": "D:/tmp/worktree",
                }
            ],
        }

        rendered = render_dashboard(snapshot)
        self.assertIn("Host: muse", rendered)
        self.assertIn("muse → implementer → luna → opencode/gpt-6-luna/max", rendered)
        self.assertIn("worktree D:/tmp/worktree", rendered)
        self.assertIn("tokens 1.5k", rendered)
        self.assertIn("cost $0.0040", rendered)

    def test_usage_normalization_supports_opencode_and_codex_shapes(self):
        opencode = summarize_usage(
            {"tokens": {"input": 20, "output": 10}, "cost": 0.01}
        )
        self.assertEqual(opencode["input_tokens"], 20)
        self.assertEqual(opencode["output_tokens"], 10)
        self.assertEqual(opencode["total_tokens"], 30)
        self.assertEqual(opencode["cost"], 0.01)

        opencode_cached = summarize_usage(
            {
                "tokens": {
                    "input": 6315,
                    "output": 209,
                    "reasoning": 124,
                    "cache": {"read": 5602, "write": 0},
                    "total": 12250,
                },
                "cost": 0.0,
            }
        )
        self.assertEqual(opencode_cached["input_tokens"], 11917)
        self.assertEqual(opencode_cached["cache_read_tokens"], 5602)
        self.assertEqual(opencode_cached["output_tokens"], 209)
        self.assertEqual(opencode_cached["total_tokens"], 12250)

        antigravity_cached = summarize_usage(
            {
                "input_tokens": 18080,
                "cache_read_tokens": 15946,
                "output_tokens": 226,
                "thinking_tokens": 0,
                "total_tokens": 18306,
            }
        )
        self.assertEqual(antigravity_cached["input_tokens"], 34026)
        self.assertEqual(antigravity_cached["cache_read_tokens"], 15946)
        self.assertEqual(antigravity_cached["output_tokens"], 226)
        self.assertEqual(antigravity_cached["total_tokens"], 34252)

        codex = summarize_usage(
            {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125}
        )
        self.assertEqual(codex["total_tokens"], 125)


    def test_clear_completed_keeps_active_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "state"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            store = RunStore(root)

            done_id = store.start_task(TaskSpec(objective="Done", workspace=workspace))
            done_record = store.complete_task(
                workspace,
                done_id,
                TaskResult(
                    status="success",
                    provider="fake",
                    summary="Done",
                    final_text="full",
                ),
            )
            output_path = Path(done_record["output_path"])
            self.assertTrue(output_path.exists())
            active_id = store.start_task(TaskSpec(objective="Active", workspace=workspace))
            store.update_task(workspace, active_id, status="running")

            removed = store.clear_completed(workspace)
            self.assertEqual(removed, 1)
            self.assertFalse(output_path.exists())
            tasks = store.list_tasks(workspace)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0]["task_id"], active_id)

    def test_gc_prunes_old_terminal_state_but_keeps_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            store = RunStore(root / "state")

            old_id = store.start_task(
                TaskSpec(objective="old", workspace=workspace),
                async_task=True,
            )
            store.complete_task(
                workspace,
                old_id,
                TaskResult(status="success", provider="fake", summary="done"),
            )
            store.update_task(
                workspace,
                old_id,
                completed_at="2020-01-01T00:00:00+00:00",
            )

            active_id = store.start_task(
                TaskSpec(objective="active", workspace=workspace),
                async_task=True,
            )
            store.update_task(workspace, active_id, status="running")

            preview = store.gc(
                workspace=workspace,
                retention_days=1,
                max_completed_tasks=100,
                max_event_log_mb=10,
                dry_run=True,
            )
            self.assertEqual(preview["count"], 1)
            self.assertEqual(preview["tasks"][0]["task_id"], old_id)

            result = store.gc(
                workspace=workspace,
                retention_days=1,
                max_completed_tasks=100,
                max_event_log_mb=10,
                dry_run=False,
            )
            self.assertEqual(result["count"], 1)
            self.assertIsNone(store.get_task(workspace, old_id))
            self.assertIsNotNone(store.get_task(workspace, active_id))

    def test_state_is_outside_workspace_and_write_failures_are_nonfatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            root = Path(tmp) / "external-state"
            store = RunStore(root)
            task_id = store.start_task(TaskSpec(objective="Check", workspace=workspace))

            self.assertTrue((root).exists())
            self.assertFalse((workspace / ".cortex-relay" / "state").exists())
            self.assertTrue(task_id)

            with patch.object(Path, "mkdir", side_effect=OSError("blocked")):
                store.update_task(workspace, task_id, status="running")


if __name__ == "__main__":
    unittest.main()
