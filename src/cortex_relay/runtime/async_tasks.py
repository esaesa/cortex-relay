"""Process-scoped asynchronous CortexRelay delegation lifecycle."""

from __future__ import annotations

import threading

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.registry import ProviderRegistry


@dataclass
class _Job:
    task: TaskSpec
    workspace: Path
    cancel_event: threading.Event
    future: Future[TaskResult]
    result: TaskResult | None = None


class AsyncTaskManager:
    """Run jobs beside the MCP server; persisted records provide visibility."""

    def __init__(self, registry: ProviderRegistry, *, max_workers: int = 4) -> None:
        self.registry = registry
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cortex-worker"
        )
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()

    def submit(self, task: TaskSpec) -> dict[str, Any]:
        task_id = self.registry.run_store.start_task(task)
        cancel_event = threading.Event()
        metadata = dict(task.metadata)
        metadata.update(_task_id=task_id, _prestarted=True, _cancel_event=cancel_event)
        prepared = replace(task, metadata=metadata)
        future = self.executor.submit(self._run, prepared, cancel_event)
        with self._lock:
            self._jobs[task_id] = _Job(task, task.workspace, cancel_event, future)
        return {"task_id": task_id, "status": "routing", "workspace": str(task.workspace)}

    def _run(self, task: TaskSpec, cancel_event: threading.Event) -> TaskResult:
        if cancel_event.is_set():
            result = self._cancelled_result(task)
            self.registry.run_store.complete_task(task.workspace, task.metadata["_task_id"], result)
            return result
        try:
            return self.registry.execute(task)
        except Exception as exc:
            result = TaskResult(
                status="error", provider=task.provider, model=task.model,
                summary="Asynchronous delegation failed.", error=str(exc),
            )
            self.registry.run_store.complete_task(task.workspace, task.metadata["_task_id"], result)
            return result

    @staticmethod
    def _cancelled_result(task: TaskSpec) -> TaskResult:
        return TaskResult(
            status="cancelled", provider=task.provider, model=task.model,
            summary="Delegation cancelled before execution.",
        )

    def status(self, task_id: str) -> dict[str, Any]:
        job = self._job(task_id)
        record = self.registry.run_store.get_task(job.workspace, task_id)
        if record is None:
            return {"task_id": task_id, "status": "unknown"}
        return record

    def wait(self, task_id: str, *, timeout_seconds: float = 0) -> dict[str, Any]:
        job = self._job(task_id)
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        # MCP clients impose their own request deadlines. A provider timeout is
        # never a safe duration for one task_wait tool call.
        wait_seconds = min(timeout_seconds, 5.0)
        try:
            result = job.future.result(timeout=wait_seconds)
        except TimeoutError:
            record = self.status(task_id)
            record["waited_seconds"] = wait_seconds
            return record
        except CancelledError:
            result = job.result or self._cancelled_result(job.task)
        return {"task_id": task_id, "result": result.to_dict()}

    def cancel(self, task_id: str) -> dict[str, Any]:
        job = self._job(task_id)
        if job.future.done():
            return self.status(task_id)
        job.cancel_event.set()
        if job.future.cancel():
            record = self.registry.run_store.get_task(job.workspace, task_id) or {}
            job.result = TaskResult(
                status="cancelled", provider=str(record.get("provider") or "auto"),
                model=record.get("model"), summary="Delegation cancelled before execution.",
            )
            self.registry.run_store.complete_task(job.workspace, task_id, job.result)
        else:
            self.registry.run_store.update_task(
                job.workspace, task_id, current_activity="Cancellation requested",
            )
        return self.status(task_id)

    def tasks(self, workspace: Path | None = None) -> list[dict[str, Any]]:
        resolved = workspace.expanduser().resolve() if workspace is not None else None
        with self._lock:
            jobs = list(self._jobs.items())
        records = [
            self.registry.run_store.get_task(job.workspace, task_id)
            for task_id, job in jobs
            if resolved is None or job.workspace == resolved
        ]
        return sorted(
            (record for record in records if record is not None),
            key=lambda record: str(record.get("started_at", "")), reverse=True,
        )

    def _job(self, task_id: str) -> _Job:
        with self._lock:
            job = self._jobs.get(task_id)
        if job is None:
            raise ValueError(f"unknown async task id: {task_id}")
        return job

    def shutdown(self) -> None:
        with self._lock:
            task_ids = list(self._jobs)
        for task_id in task_ids:
            self.cancel(task_id)
        self.executor.shutdown(wait=True, cancel_futures=True)
