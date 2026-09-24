"""Durable MCP task control with process-local execution futures."""

from __future__ import annotations

import threading

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.observability import TERMINAL_STATUSES
from cortex_relay.runtime.state_lock import FileLock


@dataclass
class _Job:
    task: TaskSpec
    workspace: Path
    cancel_event: threading.Event
    future: Future[TaskResult]
    result: TaskResult | None = None


class TaskService:
    """Control local jobs and recover observable state from disk."""

    def __init__(self, registry: ProviderRegistry, *, max_workers: int = 4) -> None:
        self.registry = registry
        self.store = registry.run_store
        self.owner_instance_id = uuid4().hex
        self._owner_lock = FileLock(self.store.owner_lock_path(self.owner_instance_id))
        self._owner_lock.acquire()
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cortex-worker")
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="cortex-owner-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
        self._reconcile_indexed()

    def _reconcile_indexed(self) -> None:
        for task_id in self.store.indexed_task_ids():
            try:
                self._resolve(task_id)
            except (OSError, ValueError):
                # Keep other task handles available if one record is damaged.
                continue

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(5):
            with self._lock:
                jobs = [(task_id, job.workspace) for task_id, job in self._jobs.items()]
            for task_id, workspace in jobs:
                try:
                    self.store.record_owner_heartbeat(workspace, task_id, self.owner_instance_id)
                except (OSError, ValueError):
                    continue

    def _resolve(self, task_id: str) -> tuple[Path, dict[str, Any]]:
        workspace, record = self.store.find_task(task_id)
        if record.get("async") and record.get("status") not in TERMINAL_STATUSES:
            record = self.store.reconcile_task(workspace, task_id)
        elif not record.get("async") and record.get("status") not in TERMINAL_STATUSES:
            record = {**record, "owner_unknown": True}
        return workspace, record

    def submit(self, task: TaskSpec) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("task service is shutting down")
        task_id = self.store.start_task(
            task, async_task=True, owner_instance_id=self.owner_instance_id
        )
        cancel_event = threading.Event()
        metadata = dict(task.metadata)
        metadata.update(_task_id=task_id, _prestarted=True, _cancel_event=cancel_event)
        prepared = replace(task, metadata=metadata)
        future = self.executor.submit(self._run, prepared, cancel_event)
        with self._lock:
            self._jobs[task_id] = _Job(task, task.workspace, cancel_event, future)
        return {"task_id": task_id, "status": "routing", "workspace": str(task.workspace)}

    def _run(self, task: TaskSpec, cancel_event: threading.Event) -> TaskResult:
        task_id = task.metadata["_task_id"]
        if cancel_event.is_set():
            result = self._cancelled_result(task)
            self.store.complete_task(task.workspace, task_id, result)
            return result
        try:
            return self.registry.execute(task)
        except Exception as exc:
            result = TaskResult(
                status="error", provider=task.provider, model=task.model,
                summary="Asynchronous delegation failed.", error=str(exc),
            )
            self.store.complete_task(task.workspace, task_id, result)
            return result

    @staticmethod
    def _cancelled_result(task: TaskSpec) -> TaskResult:
        return TaskResult(
            status="cancelled", provider=task.provider, model=task.model,
            summary="Delegation cancelled before execution.",
        )

    def status(self, task_id: str) -> dict[str, Any]:
        return self._resolve(task_id)[1]

    def events(self, task_id: str, *, after_sequence: int = 0, limit: int = 20) -> dict[str, Any]:
        workspace, _ = self._resolve(task_id)
        return self.store.list_events(
            workspace, task_id, after_sequence=after_sequence, limit=limit
        )

    def wait(self, task_id: str, *, timeout_seconds: float = 0) -> dict[str, Any]:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        wait_seconds = min(timeout_seconds, 5.0)
        with self._lock:
            job = self._jobs.get(task_id)
        if job is not None:
            try:
                job.future.result(timeout=wait_seconds)
            except (TimeoutError, CancelledError):
                pass
        workspace, record = self._resolve(task_id)
        if record.get("status") in TERMINAL_STATUSES:
            result = self.store.get_result(workspace, task_id)
            if result is None:
                return {"task_id": task_id, "status": record["status"], "result_unavailable": True}
            return {"task_id": task_id, "result": result}
        return {**record, "waited_seconds": wait_seconds if job is not None else 0}

    def cancel(self, task_id: str) -> dict[str, Any]:
        workspace, record = self._resolve(task_id)
        if record.get("status") in TERMINAL_STATUSES:
            return record
        with self._lock:
            job = self._jobs.get(task_id)
        if job is None:
            return {**record, "cancel_status": "owned_elsewhere" if record.get("async") else "owner_unknown"}
        if job.future.done():
            return self.status(task_id)
        job.cancel_event.set()
        if job.future.cancel():
            job.result = self._cancelled_result(job.task)
            self.store.complete_task(workspace, task_id, job.result)
        else:
            self.store.update_task(workspace, task_id, current_activity="Cancellation requested")
        return self.status(task_id)

    def tasks(self, workspace: Path | None = None) -> list[dict[str, Any]]:
        resolved = workspace.expanduser().resolve() if workspace is not None else None
        records: list[dict[str, Any]] = []
        for task_id in self.store.indexed_task_ids():
            try:
                task_workspace, record = self._resolve(task_id)
            except (OSError, ValueError):
                continue
            if resolved is None or task_workspace == resolved:
                records.append(record)
        return sorted(records, key=lambda item: str(item.get("started_at", "")), reverse=True)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            task_ids = list(self._jobs)
        for task_id in task_ids:
            self.cancel(task_id)
        self.executor.shutdown(wait=True, cancel_futures=True)
        self._heartbeat_stop.set()
        self._heartbeat_thread.join(timeout=1)
        self._owner_lock.release()
