import subprocess
import tempfile
import threading
import time
import unittest

from pathlib import Path

from cortex_relay.core.models import QualityGates, TaskResult, TaskSpec
from cortex_relay.core.profiles import runtime_config_from_mapping
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.observability import RunStore
from cortex_relay.providers.base import ProviderAdapter, ProviderCapabilities
from cortex_relay.runtime.analytics import analyze_routing
from cortex_relay.runtime.task_service import TaskService
from cortex_relay.runtime.workflow_view import group_snapshot, render_group


class StaticResolver:
    def __init__(self, mapping):
        self.config = runtime_config_from_mapping(mapping)

    def load(self, _workspace):
        return self.config


class WorkflowProvider(ProviderAdapter):
    name = "fake"

    def __init__(self):
        self.seen_inherited = False
        self.started = []

    def capabilities(self):
        return ProviderCapabilities(
            name=self.name,
            binary="fake",
            available=True,
            structured_output=True,
            model_selection=True,
            reasoning_control=True,
            read_only_policy=True,
            workspace_write=True,
            detail="fake",
        )

    def execute(self, task):
        self.started.append(task.role)
        if task.role == "implementer":
            (task.workspace / "feature.txt").write_text("from implementer\n", encoding="utf-8")
            return TaskResult(
                status="success",
                provider=self.name,
                summary="implemented",
                changed_files=("feature.txt",),
                tests=("1 passed",),
                usage={"total_tokens": 100},
            )
        if task.role == "tester":
            inherited = task.workspace / "feature.txt"
            self.seen_inherited = inherited.is_file() and inherited.read_text(
                encoding="utf-8"
            ) == "from implementer\n"
            return TaskResult(
                status="success" if self.seen_inherited else "error",
                provider=self.name,
                summary="tested inherited artifact",
                tests=("2 passed",),
                usage={"total_tokens": 50},
            )
        return TaskResult(
            status="success",
            provider=self.name,
            summary=task.objective,
            usage={"total_tokens": 10},
        )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def _init_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "CortexRelay Tests")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    return repo


def _wait(service: TaskService, task_id: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = service.wait(task_id, timeout_seconds=0.2)
        if "result" in response:
            return response["result"]
        time.sleep(0.05)
    raise AssertionError(f"task did not finish: {task_id}")


class WorkflowControlTests(unittest.TestCase):
    def _registry(self, repo: Path, state: Path, provider: WorkflowProvider):
        resolver = StaticResolver(
            {
                "profiles": {
                    "worker": {
                        "provider": "fake",
                        "access": "workspace_write",
                        "isolate_write": True,
                    }
                },
                "roles": {
                    "implementer": "worker",
                    "tester": "worker",
                    "reviewer": "worker",
                },
                "scheduler": {
                    "max_workers": 2,
                    "providers": {"fake": 2},
                },
                "budgets": {"max_group_tokens": 10000},
                "state": {
                    "retention_days": 30,
                    "max_completed_tasks": 100,
                    "max_event_log_mb": 5,
                    "cleanup_on_start": False,
                },
            }
        )
        return ProviderRegistry(
            [provider],
            profiles=resolver,
            run_store=RunStore(state),
        )

    def test_dependency_can_inherit_exact_implementer_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            provider = WorkflowProvider()
            registry = self._registry(repo, root / "state", provider)
            service = TaskService(registry)
            try:
                implementer = service.submit(
                    TaskSpec(
                        objective="implement",
                        role="implementer",
                        workspace=repo,
                        access="workspace_write",
                        isolate_write=True,
                    ),
                    group_id="feature",
                )
                implementer_result = _wait(service, implementer["task_id"])
                self.assertEqual(implementer_result["status"], "success")
                impl_status = service.status(implementer["task_id"])
                self.assertIsNotNone(impl_status.get("artifact_id"))

                tester = service.submit(
                    TaskSpec(
                        objective="test exact implementation",
                        role="tester",
                        workspace=repo,
                        access="workspace_write",
                        isolate_write=True,
                        quality_gates=QualityGates(require_tests=True),
                    ),
                    group_id="feature",
                    depends_on=(implementer["task_id"],),
                    inherit_workspace_from=implementer["task_id"],
                )
                tester_result = _wait(service, tester["task_id"])
                self.assertEqual(tester_result["status"], "success")
                self.assertTrue(provider.seen_inherited)

                self.assertFalse((repo / "feature.txt").exists())
                self.assertEqual(_git(repo, "status", "--porcelain"), "")
                tester_status = service.status(tester["task_id"])
                self.assertEqual(
                    tester_status["inherited_artifact_id"],
                    impl_status["artifact_id"],
                )

                rendered = render_group(group_snapshot(registry.run_store, repo, "feature"))
                self.assertIn("implementer [SUCCESS]", rendered)
                self.assertIn("tester [SUCCESS]", rendered)
                self.assertIn("inherits artifact-", rendered)
            finally:
                service.shutdown()

    def test_quality_gate_rejects_missing_tests(self):
        provider = WorkflowProvider()

        class NoTestProvider(WorkflowProvider):
            def execute(self, task):
                return TaskResult(
                    status="success",
                    provider=self.name,
                    summary="done",
                    changed_files=("x.txt",),
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            registry = self._registry(repo, root / "state", NoTestProvider())
            service = TaskService(registry)
            try:
                submitted = service.submit(
                    TaskSpec(
                        objective="must test",
                        role="reviewer",
                        workspace=repo,
                        quality_gates=QualityGates(require_tests=True),
                    )
                )
                result = _wait(service, submitted["task_id"])
                self.assertEqual(result["status"], "failed_gate")
                self.assertIn("test evidence", result["error"])
            finally:
                service.shutdown()

    def test_trace_depth_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _init_repo(root)
            provider = WorkflowProvider()
            registry = self._registry(repo, root / "state", provider)
            service = TaskService(registry)
            try:
                root_task = service.submit(
                    TaskSpec(objective="root", role="reviewer", workspace=repo),
                    max_depth=1,
                )
                _wait(service, root_task["task_id"])
                child = service.submit(
                    TaskSpec(objective="child", role="reviewer", workspace=repo),
                    parent_task_id=root_task["task_id"],
                    max_depth=1,
                )
                _wait(service, child["task_id"])
                with self.assertRaisesRegex(ValueError, "depth"):
                    service.submit(
                        TaskSpec(
                            objective="grandchild",
                            role="reviewer",
                            workspace=repo,
                        ),
                        parent_task_id=child["task_id"],
                        max_depth=1,
                    )
            finally:
                service.shutdown()

    def test_routing_analysis_is_descriptive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            store = RunStore(root / "state")
            for index in range(2):
                task_id = store.start_task(
                    TaskSpec(
                        objective=f"review {index}",
                        role="reviewer",
                        provider="fake",
                        workspace=repo,
                    ),
                    async_task=True,
                )
                store.complete_task(
                    repo,
                    task_id,
                    TaskResult(
                        status="success",
                        provider="fake",
                        summary="ok",
                        duration_seconds=1 + index,
                        usage={"total_tokens": 100 + index},
                    ),
                )
            report = analyze_routing(store, repo)
            self.assertEqual(report["samples"], 2)
            self.assertEqual(report["routes"][0]["success_rate"], 1.0)
            self.assertIn("does not automatically", report["note"])


if __name__ == "__main__":
    unittest.main()
