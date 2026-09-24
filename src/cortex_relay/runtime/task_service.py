"""Durable workflow task service with scheduling, DAGs, artifacts and policy gates."""

from __future__ import annotations

import fnmatch
import re
import threading

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from cortex_relay.core.models import DelegationContext, TaskResult, TaskSpec
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.observability import TERMINAL_STATUSES
from cortex_relay.runtime.artifacts import ArtifactStore
from cortex_relay.runtime.state_lock import FileLock
from cortex_relay.runtime.worktree_handoff import WorktreeHandoff


@dataclass
class _Job:
    task: TaskSpec
    prepared: TaskSpec
    workspace: Path
    cancel_event: threading.Event
    priority: int = 0
    inherit_workspace_from: str | None = None
    future: Future[TaskResult] | None = None
    result: TaskResult | None = None


class TaskService:
    """Control local jobs and recover observable state from disk."""

    def __init__(self, registry: ProviderRegistry, *, max_workers: int = 16) -> None:
        self.registry = registry
        self.store = registry.run_store
        self.handoff = WorktreeHandoff(self.store)
        self.artifacts = ArtifactStore(self.store)
        self.owner_instance_id = uuid4().hex
        self._owner_lock = FileLock(self.store.owner_lock_path(self.owner_instance_id))
        self._owner_lock.acquire()
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="cortex-worker"
        )
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._cleaned_workspaces: set[Path] = set()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="cortex-owner-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        self._reconcile_indexed()

    def _reconcile_indexed(self) -> None:
        for task_id in self.store.indexed_task_ids():
            try:
                self._resolve(task_id)
            except (OSError, ValueError):
                continue

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(5):
            with self._lock:
                jobs = [(task_id, job.workspace) for task_id, job in self._jobs.items()]
            for task_id, workspace in jobs:
                try:
                    self.store.record_owner_heartbeat(
                        workspace, task_id, self.owner_instance_id
                    )
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
        self,
        task: TaskSpec,
        *,
        group_id: str | None = None,
        depends_on: tuple[str, ...] = (),
        inherit_workspace_from: str | None = None,
        priority: int = 0,
        parent_task_id: str | None = None,
        max_depth: int = 8,
    ) -> dict[str, Any]:
        if group_id is not None and not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", group_id
        ):
            raise ValueError("group_id must be a bounded alphanumeric label")
        if len(depends_on) != len(set(depends_on)):
            raise ValueError("depends_on contains duplicate task IDs")
        if not -100 <= priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        if inherit_workspace_from is not None and inherit_workspace_from not in depends_on:
            raise ValueError("inherit_workspace_from must also appear in depends_on")

        config = self.registry.profiles.load(task.workspace)
        config.profile_for_task(task)
        self._maybe_gc(task.workspace, config)
        task_id = f"task-{uuid4().hex}"
        if task_id in depends_on:
            raise ValueError("a task cannot depend on itself")

        for dependency in depends_on:
            dep_workspace, record = self._resolve(dependency)
            if dep_workspace != task.workspace or not record.get("async"):
                raise ValueError(
                    f"dependency must be an async task in the same workspace: {dependency}"
                )

        context = self._context_for(
            task,
            task_id=task_id,
            parent_task_id=parent_task_id,
            max_depth=max_depth,
        )
        task = replace(task, context=context)

        cancel_event = threading.Event()
        metadata = dict(task.metadata)
        metadata.update(
            _task_id=task_id,
            _prestarted=True,
            _cancel_event=cancel_event,
        )
        prepared = replace(task, metadata=metadata)

        with self._lock:
            if self._closed:
                raise RuntimeError("task service is shutting down")
            self.store.start_task(
                task,
                task_id=task_id,
                async_task=True,
                owner_instance_id=self.owner_instance_id,
                group_id=group_id,
                depends_on=depends_on,
                priority=priority,
                inherit_workspace_from=inherit_workspace_from,
            )
            if depends_on:
                self.store.update_task(
                    task.workspace,
                    task_id,
                    status="queued",
                    queued_reason="waiting for dependencies",
                )
            self._jobs[task_id] = _Job(
                task,
                prepared,
                task.workspace,
                cancel_event,
                priority=priority,
                inherit_workspace_from=inherit_workspace_from,
            )
        self._schedule_ready()
        record = self.status(task_id)
        return {
            "task_id": task_id,
            "status": record["status"],
            "workspace": str(task.workspace),
            "group_id": group_id,
            "depends_on": list(depends_on),
            "inherit_workspace_from": inherit_workspace_from,
            "priority": priority,
            "context": context.to_dict(),
        }

    def _maybe_gc(self, workspace: Path, config: Any) -> None:
        resolved = workspace.expanduser().resolve()
        if resolved in self._cleaned_workspaces or not config.state.cleanup_on_start:
            return
        self.store.gc(
            workspace=resolved,
            retention_days=config.state.retention_days,
            max_completed_tasks=config.state.max_completed_tasks,
            max_event_log_mb=config.state.max_event_log_mb,
            dry_run=False,
        )
        self._cleaned_workspaces.add(resolved)

    def _context_for(
        self,
        task: TaskSpec,
        *,
        task_id: str,
        parent_task_id: str | None,
        max_depth: int,
    ) -> DelegationContext:
        if parent_task_id is None:
            if task.context is not None:
                if task.context.depth > task.context.max_depth:
                    raise ValueError("delegation depth exceeds max_depth")
                return task.context
            return DelegationContext(
                trace_id=f"trace-{uuid4().hex}",
                root_task_id=task_id,
                depth=0,
                max_depth=max_depth,
            )

        parent_workspace, parent = self._resolve(parent_task_id)
        if parent_workspace != task.workspace:
            raise ValueError("parent task must be in the same workspace")
        raw = parent.get("context")
        if not isinstance(raw, dict):
            trace_id = f"trace-{uuid4().hex}"
            ancestry: tuple[str, ...] = (parent_task_id,)
            root_task_id = parent_task_id
            depth = 1
            inherited_max = max_depth
        else:
            trace_id = str(raw.get("trace_id") or f"trace-{uuid4().hex}")
            parent_ancestry = tuple(
                str(item) for item in raw.get("ancestry") or [] if isinstance(item, str)
            )
            if parent_task_id in parent_ancestry:
                raise ValueError("delegation ancestry loop detected")
            ancestry = (*parent_ancestry, parent_task_id)
            root_task_id = str(raw.get("root_task_id") or parent_task_id)
            depth = int(raw.get("depth") or 0) + 1
            inherited_max = min(int(raw.get("max_depth") or max_depth), max_depth)
        return DelegationContext(
            trace_id=trace_id,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            depth=depth,
            max_depth=inherited_max,
            ancestry=ancestry,
        )

    def _schedule_ready(self) -> None:
        callbacks: list[Future[TaskResult]] = []
        with self._lock:
            if self._closed:
                return

            pending = [
                (task_id, job)
                for task_id, job in self._jobs.items()
                if job.future is None and job.result is None
            ]
            pending.sort(
                key=lambda item: (
                    item[1].priority,
                    -len(self.store.get_task(item[1].workspace, item[0]) or {}),
                    item[0],
                ),
                reverse=True,
            )

            running = [
                (task_id, job)
                for task_id, job in self._jobs.items()
                if job.future is not None and not job.future.done()
            ]

            queue_position = 0
            for task_id, job in pending:
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
                        status="blocked",
                        provider=job.task.provider,
                        model=job.task.model,
                        summary="Task dependencies did not succeed.",
                        error=f"blocked by: {', '.join(failed)}",
                        metadata={"task_id": task_id, "blocked_by": failed},
                    )
                    self.store.update_task(job.workspace, task_id, blocked_by=failed)
                    self.store.complete_task(job.workspace, task_id, blocked_result)
                    job.result = blocked_result
                    continue

                if waiting:
                    queue_position += 1
                    self.store.update_task(
                        job.workspace,
                        task_id,
                        status="queued",
                        queue_position=queue_position,
                        queued_reason="waiting for dependencies",
                    )
                    continue

                budget_error = self._workflow_budget_error(job, record)
                if budget_error:
                    result = TaskResult(
                        status="budget_exceeded",
                        provider=job.task.provider,
                        model=job.task.model,
                        summary="Workflow budget prevented task execution.",
                        error=budget_error,
                        metadata={"task_id": task_id},
                    )
                    self.store.complete_task(job.workspace, task_id, result)
                    job.result = result
                    continue

                allowed, reason = self._scheduler_allows(job, running)
                if not allowed:
                    queue_position += 1
                    self.store.update_task(
                        job.workspace,
                        task_id,
                        status="queued",
                        queue_position=queue_position,
                        queued_reason=reason,
                    )
                    continue

                prepared = job.prepared
                if job.inherit_workspace_from:
                    try:
                        artifact = self.artifacts.for_task(
                            job.inherit_workspace_from, create=True
                        )
                    except (OSError, ValueError) as exc:
                        result = TaskResult(
                            status="blocked",
                            provider=job.task.provider,
                            model=job.task.model,
                            summary="Dependency output could not be inherited.",
                            error=str(exc),
                            metadata={
                                "task_id": task_id,
                                "blocked_by": [job.inherit_workspace_from],
                            },
                        )
                        self.store.complete_task(job.workspace, task_id, result)
                        job.result = result
                        continue
                    prepared = replace(
                        prepared,
                        metadata={
                            **prepared.metadata,
                            "_inherit_artifact_id": artifact["artifact_id"],
                        },
                    )
                    job.prepared = prepared
                    self.store.update_task(
                        job.workspace,
                        task_id,
                        inherited_artifact_id=artifact["artifact_id"],
                    )

                self.store.update_task(
                    job.workspace,
                    task_id,
                    status="routing",
                    queue_position=None,
                    queued_reason=None,
                )
                job.future = self.executor.submit(
                    self._run, prepared, job.cancel_event
                )
                running.append((task_id, job))
                callbacks.append(job.future)

        for future in callbacks:
            future.add_done_callback(lambda _: self._schedule_ready())

    def _scheduler_allows(
        self, job: _Job, running: list[tuple[str, _Job]]
    ) -> tuple[bool, str | None]:
        config = self.registry.profiles.load(job.workspace)
        scheduler = config.scheduler
        same_workspace = [
            item for _, item in running if item.workspace == job.workspace
        ]
        if len(same_workspace) >= scheduler.max_workers:
            return False, f"workspace concurrency {len(same_workspace)}/{scheduler.max_workers}"

        provider, profile = self._resource_for(job.task)
        if provider:
            active_provider = sum(
                self._resource_for(item.task)[0] == provider
                for _, item in running
                if item.workspace == job.workspace
            )
            limit = scheduler.provider_limits.get(provider)
            if limit is not None and active_provider >= limit:
                return False, f"{provider} concurrency {active_provider}/{limit}"
        if profile:
            active_profile = sum(
                self._resource_for(item.task)[1] == profile
                for _, item in running
                if item.workspace == job.workspace
            )
            limit = scheduler.profile_limits.get(profile)
            if limit is not None and active_profile >= limit:
                return False, f"{profile} profile concurrency {active_profile}/{limit}"
        return True, None

    def _resource_for(self, task: TaskSpec) -> tuple[str | None, str | None]:
        try:
            config = self.registry.profiles.load(task.workspace)
            profile = config.profile_for_task(task)
        except (OSError, ValueError):
            return task.provider if task.provider != "auto" else None, task.profile
        if profile is not None:
            return profile.provider, profile.name
        if task.provider != "auto":
            return task.provider, task.profile
        try:
            return self.registry.resolve(task).name, task.profile
        except (KeyError, ValueError):
            return None, task.profile

    def _workflow_budget_error(
        self, job: _Job, record: dict[str, Any]
    ) -> str | None:
        config = self.registry.profiles.load(job.workspace)
        budgets = config.budgets
        group_id = record.get("group_id")
        session_id = record.get("session_id")
        if budgets.max_group_tokens is not None and isinstance(group_id, str):
            used = self.store.usage_totals(
                job.workspace, group_id=group_id
            )["total_tokens"]
            if used >= budgets.max_group_tokens:
                return (
                    f"group token budget exhausted: {used} >= "
                    f"{budgets.max_group_tokens}"
                )
        if budgets.max_session_tokens is not None and isinstance(session_id, str):
            used = self.store.usage_totals(
                job.workspace, session_id=session_id
            )["total_tokens"]
            if used >= budgets.max_session_tokens:
                return (
                    f"session token budget exhausted: {used} >= "
                    f"{budgets.max_session_tokens}"
                )
        if budgets.max_premium_tasks is not None:
            _, profile_name = self._resource_for(job.task)
            profile = (
                config.profiles.get(profile_name)
                if isinstance(profile_name, str)
                else None
            )
            if profile and profile.billing_class and "premium" in profile.billing_class.lower():
                used = self.store.usage_totals(
                    job.workspace,
                    group_id=group_id if isinstance(group_id, str) else None,
                    session_id=session_id if not isinstance(group_id, str) else None,
                )["premium_tasks"]
                if used >= budgets.max_premium_tasks:
                    return (
                        f"premium task budget exhausted: {used} >= "
                        f"{budgets.max_premium_tasks}"
                    )
        return None

    def _run(self, task: TaskSpec, cancel_event: threading.Event) -> TaskResult:
        task_id = str(task.metadata["_task_id"])
        if cancel_event.is_set():
            result = self._cancelled_result(task)
            self.store.complete_task(task.workspace, task_id, result)
            return result
        try:
            result = self.registry.execute(task)
        except Exception as exc:
            result = TaskResult(
                status="error",
                provider=task.provider,
                model=task.model,
                summary="Asynchronous delegation failed.",
                error=str(exc),
            )
            self.store.complete_task(task.workspace, task_id, result)
            return result

        result = self._apply_quality_gates(task, result)
        if result.status != self.status(task_id).get("status"):
            self.store.complete_task(task.workspace, task_id, result)

        if result.status == "success" and task.access == "workspace_write":
            record = self.store.get_task(task.workspace, task_id) or {}
            if record.get("worktree_path") and (
                result.changed_files or record.get("progress_files")
            ):
                try:
                    artifact = self.artifacts.create(task_id)
                    self.store.update_task(
                        task.workspace,
                        task_id,
                        artifact_id=artifact["artifact_id"],
                        artifact_sha256=artifact["patch_sha256"],
                    )
                except (OSError, ValueError):
                    # Artifact production is required only when another task asks
                    # to inherit this output. Preserve the successful result here.
                    pass
        return result

    def _apply_quality_gates(
        self, task: TaskSpec, result: TaskResult
    ) -> TaskResult:
        if result.status != "success":
            return result
        gates = task.quality_gates
        failures: list[str] = []
        if gates.require_changed_files and not result.changed_files:
            failures.append("no changed files were reported")
        if gates.require_tests and not result.tests:
            failures.append("no test evidence was reported")
        if gates.allowed_paths:
            for changed in result.changed_files:
                normalized = changed.replace("\\", "/")
                if not any(
                    fnmatch.fnmatch(normalized, pattern)
                    or normalized.startswith(pattern.rstrip("*") + "/")
                    for pattern in gates.allowed_paths
                ):
                    failures.append(f"changed path is outside allowed_paths: {changed}")
        if gates.max_failed_tests is not None:
            failed = 0
            for item in result.tests:
                text = item.lower()
                match = re.search(r"(\d+)\s+failed", text)
                if match:
                    failed += int(match.group(1))
                elif "failed" in text and "0 failed" not in text:
                    failed += 1
            if failed > gates.max_failed_tests:
                failures.append(
                    f"failed tests {failed} exceed gate {gates.max_failed_tests}"
                )
        if gates.require_review and task.role != "reviewer":
            record = self.store.get_task(task.workspace, str(task.metadata["_task_id"])) or {}
            reviewed = False
            for dependency in record.get("depends_on") or []:
                try:
                    dep = self._resolve(str(dependency))[1]
                except (OSError, ValueError):
                    continue
                if dep.get("role") == "reviewer" and dep.get("status") == "success":
                    reviewed = True
                    break
            if not reviewed:
                failures.append("no successful reviewer dependency was found")

        if not failures:
            return result
        return TaskResult(
            status="failed_gate",
            provider=result.provider,
            model=result.model,
            summary="Provider work completed, but CortexRelay quality gates rejected it.",
            evidence=result.evidence,
            changed_files=result.changed_files,
            commands=result.commands,
            tests=result.tests,
            risks=result.risks,
            conversation_id=result.conversation_id,
            error="; ".join(failures),
            duration_seconds=result.duration_seconds,
            usage=result.usage,
            metadata={**result.metadata, "quality_gate_failures": failures},
        )

    @staticmethod
    def _cancelled_result(task: TaskSpec) -> TaskResult:
        return TaskResult(
            status="cancelled",
            provider=task.provider,
            model=task.model,
            summary="Delegation cancelled before execution.",
        )

    def status(self, task_id: str) -> dict[str, Any]:
        return self._resolve(task_id)[1]

    def events(
        self, task_id: str, *, after_sequence: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        workspace, _ = self._resolve(task_id)
        return self.store.list_events(
            workspace,
            task_id,
            after_sequence=after_sequence,
            limit=limit,
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
                return {
                    "task_id": task_id,
                    "status": record["status"],
                    "result_unavailable": True,
                }
            return {"task_id": task_id, "result": result}
        return {**record, "waited_seconds": wait_seconds if job is not None else 0}

    def cancel(self, task_id: str) -> dict[str, Any]:
        workspace, record = self._resolve(task_id)
        if record.get("status") in TERMINAL_STATUSES:
            return record
        with self._lock:
            job = self._jobs.get(task_id)
            if job is None:
                return {
                    **record,
                    "cancel_status": (
                        "owned_elsewhere" if record.get("async") else "owner_unknown"
                    ),
                }
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
                self.store.update_task(
                    workspace,
                    task_id,
                    current_activity="Cancellation requested",
                )
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
            key=lambda item: (
                int(item.get("priority") or 0),
                str(item.get("started_at", "")),
                str(item.get("task_id", "")),
            ),
            reverse=group_id is None,
        )

    def artifact(self, task_id: str, *, create: bool = True) -> dict[str, Any]:
        return self.artifacts.for_task(task_id, create=create)

    def worktree(self, task_id: str, attempt: int | None = None) -> dict[str, Any]:
        return self.handoff.worktree(task_id, attempt)

    def diff(
        self,
        task_id: str,
        attempt: int | None = None,
        max_bytes: int = 65536,
    ) -> dict[str, Any]:
        return self.handoff.diff(task_id, attempt, max_bytes)

    def apply(self, task_id: str, attempt: int | None = None) -> dict[str, Any]:
        return self.handoff.apply(task_id, attempt)

    def discard(
        self,
        task_id: str,
        attempt: int | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
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
