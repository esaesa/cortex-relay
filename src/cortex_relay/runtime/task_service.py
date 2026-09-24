"""Durable MCP task control with process-local execution futures."""

from __future__ import annotations

import threading
import re

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.observability import TERMINAL_STATUSES
from cortex_relay.runtime.state_lock import FileLock
from cortex_relay.runtime.worktree_handoff import WorktreeHandoff


@dataclass
class _Job:
    task: TaskSpec
    prepared: TaskSpec
    workspace: Path
    cancel_event: threading.Event
    future: Future[TaskResult] | None = None
    result: TaskResult | None = None


class TaskService:
    """Control local jobs and recover observable state from disk."""

    def __init__(self, registry: ProviderRegistry, *, max_workers: int = 4) -> None:
        self.registry = registry
        self.store = registry.run_store
        self.handoff = WorktreeHandoff(self.store)
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

    def submit(
        self, task: TaskSpec, *, group_id: str | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if group_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", group_id):
            raise ValueError("group_id must be a bounded alphanumeric label")
        if len(depends_on) != len(set(depends_on)):
            raise ValueError("depends_on contains duplicate task IDs")
        task_id = f"task-{uuid4().hex}"
        if task_id in depends_on:
            raise ValueError("a task cannot depend on itself")
        for dependency in depends_on:
            dep_workspace, record = self._resolve(dependency)
            if dep_workspace != task.workspace or not record.get("async"):
                raise ValueError(f"dependency must be an async task in the same workspace: {dependency}")

        cancel_event = threading.Event()
        metadata = dict(task.metadata)
        metadata.update(_task_id=task_id, _prestarted=True, _cancel_event=cancel_event)
        prepared = replace(task, metadata=metadata)
        with self._lock:
            if self._closed:
                raise RuntimeError("task service is shutting down")
            self.store.start_task(
                task, task_id=task_id, async_task=True,
                owner_instance_id=self.owner_instance_id,
                group_id=group_id, depends_on=depends_on,
            )
            if depends_on:
                self.store.update_task(task.workspace, task_id, status="queued")
            self._jobs[task_id] = _Job(task, prepared, task.workspace, cancel_event)
        self._schedule_ready()
        return {
            "task_id": task_id,
            "status": self.status(task_id)["status"],
            "workspace": str(task.workspace),
            "group_id": group_id,
            "depends_on": list(depends_on),
        }

    def _schedule_ready(self) -> None:
        callbacks: list[Future[TaskResult]] = []
        with self._lock:
            if self._closed:
                return
            changed = True
            while changed:
                changed = False
                for task_id, job in self._jobs.items():
                    if job.future is not None or job.result is not None:
                        continue
                    record = self.store.get_task(job.workspace, task_id)
                    if not record or record.get("status") in TERMINAL_STATUSES:
                        continue
                    dependencies = record.get("depends_on") or []
                    failed: list[str] = []
                    waiting = False
                    for dependency in dependencies:
                        try:
                            dep_status = self._resolve(dependency)[1]["status"]
                        except (OSError, ValueError):
                            dep_status = "unavailable"
                        if dep_status != "success":
                            if dep_status in TERMINAL_STATUSES:
                                failed.append(dependency)
                            else:
                                waiting = True
                    if failed:
                        blocked_result = TaskResult(
                            status="blocked", provider=job.task.provider,
                            model=job.task.model,
                            summary="Task dependencies did not succeed.",
                            error=f"blocked by: {', '.join(failed)}",
                            metadata={"task_id": task_id, "blocked_by": failed},
                        )
                        self.store.update_task(job.workspace, task_id, blocked_by=failed)
                        self.store.complete_task(job.workspace, task_id, blocked_result)
                        job.result = blocked_result
                        changed = True
                    elif not waiting:
                        self.store.update_task(job.workspace, task_id, status="routing")
                        job.future = self.executor.submit(self._run, job.prepared, job.cancel_event)
                        callbacks.append(job.future)
                        changed = True
        for future in callbacks:
            future.add_done_callback(lambda _: self._schedule_ready())

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
        if job is not None and job.future is not None:
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
            if job.future is not None and job.future.done():
                return self.status(task_id)
            job.cancel_event.set()
            future = job.future
            if future is None:
                cancelled_result = self._cancelled_result(job.task)
                self.store.complete_task(workspace, task_id, cancelled_result)
                job.result = cancelled_result
        if future is not None:
            if future.cancel():
                cancelled_result = self._cancelled_result(job.task)
                self.store.complete_task(workspace, task_id, cancelled_result)
                job.result = cancelled_result
            else:
                self.store.update_task(workspace, task_id, current_activity="Cancellation requested")
        self._schedule_ready()
        return self.status(task_id)

    def tasks(
        self, workspace: Path | None = None, *, group_id: str | None = None
    ) -> list[dict[str, Any]]:
        resolved = workspace.expanduser().resolve() if workspace is not None else None
        records: list[dict[str, Any]] = []
        for task_id in self.store.indexed_task_ids():
            try:
                task_workspace, record = self._resolve(task_id)
            except (OSError, ValueError):
                continue
            if (resolved is None or task_workspace == resolved) and (
                group_id is None or record.get("group_id") == group_id
            ):
                records.append(record)
        return sorted(
            records,
            key=lambda item: (str(item.get("started_at", "")), str(item.get("task_id", ""))),
            reverse=group_id is None,
        )

    def worktree(self, task_id: str, attempt: int | None = None) -> dict[str, Any]:
        return self.handoff.worktree(task_id, attempt)

    def diff(self, task_id: str, attempt: int | None = None,
             max_bytes: int = 65536) -> dict[str, Any]:
        return self.handoff.diff(task_id, attempt, max_bytes)

    def apply(self, task_id: str, attempt: int | None = None) -> dict[str, Any]:
        return self.handoff.apply(task_id, attempt)

    def discard(self, task_id: str, attempt: int | None = None,
                confirmation_token: str | None = None) -> dict[str, Any]:
        return self.handoff.discard(task_id, attempt, confirmation_token)

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
