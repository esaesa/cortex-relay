# Durable Task Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make async task results inspectable after MCP restart, execute dependency groups deterministically, and let a user inspect and explicitly apply or discard isolated worker changes.

**Architecture:** `RunStore` persists identity, owner, results, and worktree provenance under cross-process task locks. `TaskService` owns only live futures and dependency scheduling; all read methods can recover from disk. `WorktreeHandoff` verifies recorded Git provenance and uses a temporary alternate index to inspect and transfer the full worker snapshot.

**Tech Stack:** Python 3.11+, standard library, Git CLI, optional MCP Python package, Windows PowerShell primary environment.

**Spec:** `docs/superpowers/specs/2026-09-24-durable-task-lifecycle-design.md`

## Global Constraints

- Keep existing MCP tool names and required arguments valid; add optional fields and four handoff tools.
- Keep synchronous `delegate` behavior and provider execution boundaries intact.
- New task IDs are `task-` plus all 32 hexadecimal UUID4 characters.
- Do not resume or kill a worker after its controlling MCP process has died.
- Do not automatically apply, commit, merge, or discard an isolated worktree.
- Use only recorded, canonical, managed worktree paths for any handoff mutation.
- Do not add dependencies. Keep provider response text out of progress records.
- Current instruction: do not add or run automated tests unless the user asks. Each task uses syntax compilation and `git diff --check`; behavioral acceptance remains unverified until authorized.
- Implement natively in this checkout. Do not spawn subagents without an explicit user request.

## File map

| File | Responsibility |
| --- | --- |
| `src/cortex_relay/runtime/state_lock.py` | Cross-platform file locks for task records and service ownership. |
| `src/cortex_relay/observability.py` | Durable task index, full result storage, locked state transitions, legacy lookup, dashboard states. |
| `src/cortex_relay/runtime/task_service.py` | MCP-facing task control, owner reconciliation, in-process dependency scheduler. |
| `src/cortex_relay/runtime/async_tasks.py` | Compatibility import for `AsyncTaskManager` if existing callers still use it. |
| `src/cortex_relay/runtime/worktree.py` | Worktree creation with base SHA, task ID, and attempt number. |
| `src/cortex_relay/runtime/worktree_handoff.py` | Git provenance checks, alternate-index snapshot, bounded diff, apply, discard. |
| `src/cortex_relay/core/models.py` | `blocked` and `interrupted` lifecycle result statuses. |
| `src/cortex_relay/core/registry.py` | Persist result in the returned shape; record each writer attempt's worktree. |
| `src/cortex_relay/transports/mcp.py` | Service delegation and new optional fields/tools. |
| `src/cortex_relay/cli.py`, `docs/runtime.md`, `README.md` | Visible states, group/handoff commands, operational limits. |

## Review Focus

1. A PID can be reused after restart. Task 3 uses an owner lock rather than PID alone to decide interruption.
2. A crash can occur between result-file persistence and task-state update. Tasks 2 and 3 reconcile from the complete result file.
3. A dependency can belong to another workspace or fail before its dependent is submitted. Task 4 validates workspace and blocks immediately on failed prerequisites.
4. A profile fallback can leave more than one worktree. Task 5 records every attempt, and Tasks 6–8 require an explicit attempt when selection is ambiguous.
5. A worker can leave committed, staged, unstaged, untracked, or binary changes. Task 6 builds the full snapshot with an alternate index; Task 7 applies that same complete patch only after a clean-target preflight.

---

### Task 1: File locks and task transition boundary

**Files:** Create `src/cortex_relay/runtime/state_lock.py`; modify `src/cortex_relay/observability.py`.

**Interfaces:** Produce `FileLock(path: Path)`, `acquire(blocking: bool = True) -> bool`, `release() -> None`, and `RunStore._task_lock(workspace: Path, task_id: str) -> FileLock`.

- [ ] **Step 1: Add a cross-platform lock.** Open the lock file in append-binary mode, ensure byte zero exists, and lock byte zero with `msvcrt.locking` on Windows or `fcntl.flock` elsewhere. A nonblocking acquire returns `False` when another process holds it. `release()` unlocks and closes in `finally`.

  ```python
  lock = FileLock(path)
  if not lock.acquire(blocking=False):
      return False
  try:
      mutate()
  finally:
      lock.release()
  ```

- [ ] **Step 2: Lock task read-modify-write paths.** In `RunStore`, wrap `update_task`, `record_progress`, `record_heartbeat`, `complete_task`, and later reconciliation/handoff transitions with `_task_lock(workspace, task_id)`. Use one lock path per workspace hash and exact task ID. Allocate JSONL sequence and append the event while holding that lock.

  ```python
  with self._task_lock(workspace, task_id):
      record = self._read_record(self._task_path(workspace, task_id))
      # Compute one transition from this record, then write it once.
  ```

- [ ] **Step 3: Preserve lock ordering.** Service scheduler lock precedes task locks only for a short dependency decision; no task-store method calls back into the scheduler. Never hold a repository Git lock while waiting for a task lock.
- [ ] **Step 4: Check and commit.** Run `python -m py_compile` on touched Python files and `git diff --check`. Commit only this slice with `git commit -m "feat: lock durable task transitions"`.

### Task 2: Durable identity, index, and full result

**Files:** Modify `src/cortex_relay/observability.py`, `src/cortex_relay/core/registry.py`, and `src/cortex_relay/core/models.py`.

**Interfaces:** Produce `RunStore.start_task(task, *, task_id=None, async_task=False, owner_instance_id=None, group_id=None, depends_on=()) -> str`, `RunStore.find_task(task_id) -> tuple[Path, dict]`, and `RunStore.get_result(workspace, task_id) -> dict | None`.

- [ ] **Step 1: Use full opaque IDs and write an index for async tasks.** Generate `f"task-{uuid4().hex}"`, validate requested IDs before path construction, and write `root/task-index/<task-id>.json` atomically with the resolved workspace, ID, schema version, and creation time. Reject an existing ID rather than replacing its record. Keep `async_task=False` as the synchronous default.

  ```python
  task_id = task_id or f"task-{uuid4().hex}"
  record["async"] = async_task
  record["owner_instance_id"] = owner_instance_id
  if async_task:
      self._write_required(self._index_path(task_id), index_record)
  ```

- [ ] **Step 2: Resolve IDs without an in-memory job.** `find_task` validates the index record against the task record's ID and workspace. If no index exists, scan `root/*/tasks/*.json` for an exact legacy ID and return that record without rewriting it. Missing or contradictory records raise an explicit lookup error, not `unknown` for a valid existing task.
- [ ] **Step 3: Persist the exact returned result.** In `ProviderRegistry.execute`, assemble the `observability` metadata before `complete_task`. `RunStore.complete_task` writes `TaskResult.to_dict()` to `results/<task-id>.json` with a required atomic write, then marks the task terminal and stores `result_path`. Extend `TaskStatus` with `blocked` and `interrupted` for synthetic lifecycle results. If a new-format terminal record loses its result file, surface storage failure; for an older record return `result_unavailable`.

  ```python
  result = replace(result, metadata={**result.metadata, "observability": observability})
  self.run_store.complete_task(source_workspace, task_id, result)
  return result
  ```

- [ ] **Step 4: Clear all linked state.** `clear_completed` removes the exact index and result files alongside the task JSON and event JSONL. It never removes another task's files after a mismatched index check.
- [ ] **Step 5: Check and commit.** Run Python compilation and `git diff --check`. Commit with `git commit -m "feat: persist async identity and complete results"`.

### Task 3: Owner recovery and disk-backed task control

**Files:** Create `src/cortex_relay/runtime/task_service.py`; modify `src/cortex_relay/runtime/async_tasks.py`, `src/cortex_relay/transports/mcp.py`, and `src/cortex_relay/observability.py`.

**Interfaces:** Produce `TaskService(registry, max_workers=4)`, `submit(task, *, group_id=None, depends_on=())`, `status(task_id)`, `events(task_id, *, after_sequence=0, limit=20)`, `wait(task_id, *, timeout_seconds=0)`, `cancel(task_id)`, `tasks(workspace=None, *, group_id=None)`, and `shutdown()`.

- [ ] **Step 1: Acquire an owner lease.** On service construction generate `owner_instance_id = uuid4().hex`, acquire `root/owners/<id>.lock` for the service lifetime, and update owner heartbeat on a five-second timer. The owner lock remains held while jobs execute, including when no provider event arrives.
- [ ] **Step 2: Reconcile dead owners.** On startup and any disk lookup, inspect a nonterminal async record's owner lock. If no other process holds it, read the result file first: finalize from a complete result if present; otherwise write an `interrupted` synthetic result and terminal state. If the lock is held, leave the task unchanged even if heartbeat is stale. Legacy records without an owner ID stay readable with `owner_unknown` and are not mutated.

  ```python
  if record.get("async") and record["status"] not in TERMINAL_STATUSES:
      if not owner_lock_is_held(record["owner_instance_id"]):
          result = store.get_result(workspace, task_id)
          store.finalize_or_interrupt(workspace, task_id, result)
  ```

- [ ] **Step 3: Route reads through durable lookup.** `status`, `events`, and `tasks` resolve from `RunStore` even when `_jobs` lacks the ID. `wait` uses a local future for at most five seconds if present, then reads the stored terminal result or state. `cancel` sets the local cancellation event or cancels a local queued job; a live foreign owner yields `owned_elsewhere`, and terminal tasks return their stored state.
- [ ] **Step 4: Preserve imports and lifecycle.** Re-export or alias `AsyncTaskManager` from `runtime/async_tasks.py` for callers. `create_server` and `run_mcp` instantiate `TaskService`; graceful shutdown cancels local work, waits for the executor, then releases the owner lock.
- [ ] **Step 5: Check and commit.** Run Python compilation and `git diff --check`. Commit with `git commit -m "feat: recover async task control from disk"`.

### Task 4: Dependency scheduler and group view

**Files:** Modify `src/cortex_relay/runtime/task_service.py`, `src/cortex_relay/observability.py`, `src/cortex_relay/transports/mcp.py`, and `src/cortex_relay/core/models.py`.

**Interfaces:** Extend `TaskService.submit(task, *, group_id: str | None = None, depends_on: tuple[str, ...] = ())`; expose optional `group_id` and `depends_on` on `delegate_async`, and optional `group_id` on `tasks`.

- [ ] **Step 1: Validate and persist dependencies.** Resolve every dependency by exact ID, require `async=True` and the same canonical source workspace, reject duplicates and self-reference, and validate `group_id` as a bounded token. Dependencies reference only earlier submitted immutable tasks, so a cycle cannot form. Persist `group_id`, `depends_on`, and `blocked_by` on the new task record.
- [ ] **Step 2: Queue without taking a worker.** If any prerequisite is nonterminal, store `status="queued"` and retain its `TaskSpec` in a local pending map. If all are successful, submit it to the executor. If any is terminal and unsuccessful, complete it as `blocked` with offending IDs and a synthetic result.

  ```python
  states = [self.status(dep)["status"] for dep in depends_on]
  if any(state in FAILURE_STATUSES for state in states):
      self._block(task_id, depends_on)
  elif all(state == "success" for state in states):
      self._dispatch(task_id)
  else:
      self._pending[task_id] = prepared_task
  ```

- [ ] **Step 3: Advance dependents on completion.** The future completion callback rechecks local pending tasks under the scheduler lock, collects newly ready IDs, and recursively blocks those with failed prerequisites. Register callbacks and submit ready futures after releasing the scheduler lock, since a completed future may invoke its callback immediately. Queued cancellation creates a persisted `cancelled` result without starting a provider. After owner loss, queued records become `interrupted` and are not relaunched.
- [ ] **Step 4: Render groups.** `tasks(workspace, group_id=...)` returns matching persisted records in dependency order, with stable creation order for siblings. Add `queued`, `blocked`, and `interrupted` to dashboard active/terminal counts and symbols; show `depends_on` and `blocked_by` in verbose output.
- [ ] **Step 5: Check and commit.** Run Python compilation and `git diff --check`. Commit with `git commit -m "feat: schedule dependent task groups"`.

### Task 5: Record every isolated worktree attempt

**Files:** Modify `src/cortex_relay/runtime/worktree.py`, `src/cortex_relay/core/registry.py`, and `src/cortex_relay/observability.py`.

**Interfaces:** Extend `Worktree` with `base_commit: str`; use `WorktreeManager.create(repository, *, task_id, attempt=1, base_ref="HEAD", root=None) -> Worktree`; record `worktree_attempts: list[dict]` in task state. The default attempt preserves existing direct callers.

- [ ] **Step 1: Resolve and bind provenance.** Resolve `base_ref` with `git rev-parse --verify <ref>^{commit}` in the source repository before `git worktree add`. Name the branch and directory from the full task ID plus `-a<attempt>`. Return canonical path, branch, and exact base SHA. Keep the managed root adjacent to the source repository.
- [ ] **Step 2: Pass the existing task ID and attempt.** In `ProviderRegistry._execute_provider`, use metadata `_task_id` and `attempt` set by `_execute_profile_chain`; use attempt `1` for a direct route. Append each created worktree's source repository, path, branch, base SHA, profile, attempt, and creation time under the task lock before launching the worker. Keep existing `worktree_path`/`worktree_branch` fields for compatibility.
- [ ] **Step 3: Preserve fallback artifacts.** Do not overwrite earlier `worktree_attempts` when a candidate fails and fallback starts. Return each attempt's provenance in routing metadata. Handoff selection rejects an omitted attempt number when more than one managed worktree exists.
- [ ] **Step 4: Check and commit.** Run Python compilation and `git diff --check`. Commit with `git commit -m "feat: bind worktrees to task attempts"`.

### Task 6: Read-only handoff inspection

**Files:** Create `src/cortex_relay/runtime/worktree_handoff.py`; modify `src/cortex_relay/runtime/task_service.py` and `src/cortex_relay/transports/mcp.py`.

**Interfaces:** Produce `WorktreeHandoff.worktree(task_id, attempt=None) -> dict`, `diff(task_id, attempt=None, max_bytes=65536) -> dict`, and MCP `task_worktree`/`task_diff` with the same optional arguments.

- [ ] **Step 1: Resolve a recorded attempt only.** Lookup the task through the index, select the unique attempt or exact requested number, canonicalize source and worktree paths, and verify `git worktree list --porcelain`, branch, and recorded base SHA. For legacy records without base SHA, return an inspection-only view and refuse later mutations.
- [ ] **Step 2: Build a full snapshot without editing the worker index.** Allocate a temporary directory outside the worktree and point `GIT_INDEX_FILE` at a not-yet-created file inside it. Run `git read-tree <base-sha>`, then `git add -A -- .` from the worktree root. Use `git diff --cached --binary <base-sha>` with the alternate index. Capture `git diff --cached --name-status -z <base-sha>` for a file list. Include committed, staged, unstaged, and untracked nonignored content; remove the temporary directory in `finally`.

  ```python
  env = {**os.environ, "GIT_INDEX_FILE": str(temp_index)}
  git(worktree, ["read-tree", base_sha], env=env)
  git(worktree, ["add", "-A", "--", "."], env=env)
  patch = git_bytes(worktree, ["diff", "--cached", "--binary", base_sha], env=env)
  ```

- [ ] **Step 3: Bound MCP output.** Compute a SHA-256 patch digest, total byte count, changed-file summary, and UTF-8 replacement-decoded preview capped by `max_bytes` in a documented safe range. Set `truncated=true` when capped; never use that preview as the apply source. Avoid returning raw ignored files or provider transcripts.
- [ ] **Step 4: Check and commit.** Run Python compilation and `git diff --check`. Commit with `git commit -m "feat: inspect complete worker snapshots"`.

### Task 7: Explicit conflict-checked apply

**Files:** Modify `src/cortex_relay/runtime/worktree_handoff.py`, `src/cortex_relay/runtime/task_service.py`, and `src/cortex_relay/transports/mcp.py`.

**Interfaces:** Produce `WorktreeHandoff.apply(task_id, attempt=None) -> dict` and MCP `task_apply(task_id, attempt=None)`.

- [ ] **Step 1: Enforce eligibility.** Require terminal task status, a proven managed attempt, an existing worktree, `handoff_status` neither `applied` nor `discarded`, and a source checkout with empty `git status --porcelain -z`. Capture source HEAD, worker HEAD, worktree status, and complete patch digest.
- [ ] **Step 2: Preflight and apply the exact patch.** Take a repository handoff lock. Recheck source HEAD/cleanliness and worker snapshot digest, run `git apply --check` with the complete binary patch in the source checkout, then `git apply` with the same bytes. Preserve the worktree and leave target changes uncommitted. If the target is modified despite preflight failure, report the exact changed files and avoid claiming rollback.

  ```python
  git(source, ["apply", "--check", "--binary", "-"], input=patch)
  git(source, ["apply", "--binary", "-"], input=patch)
  store.record_handoff(workspace, task_id, attempt, "applied", head, patch_digest)
  ```

- [ ] **Step 3: Make retry idempotent.** Persist target HEAD, patch digest, attempt number, changed files, and applied time. A repeated call returns that prior application without applying again. A changed source or worker between preflight and apply yields a conflict response.
- [ ] **Step 4: Check and commit.** Run Python compilation and `git diff --check`. Commit with `git commit -m "feat: apply reviewed worker changes explicitly"`.

### Task 8: Snapshot-bound discard

**Files:** Modify `src/cortex_relay/runtime/worktree_handoff.py`, `src/cortex_relay/runtime/task_service.py`, and `src/cortex_relay/transports/mcp.py`.

**Interfaces:** Produce `WorktreeHandoff.discard(task_id, attempt=None, confirmation_token=None) -> dict` and MCP `task_discard` with the same arguments.

- [ ] **Step 1: Prepare a reviewable confirmation.** On the first call require a terminal task and proven managed attempt. Generate a random 256-bit token, store only its hash with a five-minute expiry and snapshot digest over task ID, attempt, canonical path, branch, base SHA, worker HEAD, and complete patch digest. Return the path, branch, changed-file summary, and token.
- [ ] **Step 2: Verify exact target before removal.** On the second call compare the token hash, expiry, and freshly computed snapshot digest. Resolve the worktree and managed root to absolute paths and require `worktree_path.is_relative_to(managed_root)` with no symlink escape. Verify Git still registers that exact worktree and branch. Reject a running task, changed contents, foreign path, or already discarded attempt.
- [ ] **Step 3: Remove and retain history.** Under the repository handoff lock, run `git worktree remove --force <exact-path>`, then delete the exact task-created branch only after verifying it is no longer checked out. Record `handoff_status=discarded`; retain result, task, and events. If branch deletion fails, report the remaining branch and the completed worktree-removal step accurately.
- [ ] **Step 4: Check and commit.** Run Python compilation and `git diff --check`. Commit with `git commit -m "feat: discard confirmed worker worktrees"`.

### Task 9: Documentation and integration review

**Files:** Modify `README.md`, `docs/runtime.md`, `src/cortex_relay/transports/mcp.py`, and `src/cortex_relay/observability.py`.

**Interfaces:** Document all durable task and handoff tool arguments and returned states; preserve existing MCP calls.

- [ ] **Step 1: Document restart semantics.** Explain exact-result recovery, `interrupted` owner loss, `owned_elsewhere`, legacy `result_unavailable`, and the fact that queued tasks do not restart automatically.
- [ ] **Step 2: Document orchestrator flow.** Show `delegate_async` prerequisite IDs, dependent submission, `tasks(group_id=...)`, `task_events` cursor use, `task_worktree`/`task_diff`, explicit `task_apply`, and two-call `task_discard`. State that worktree apply leaves checkout changes uncommitted.
- [ ] **Step 3: Review signatures and state transitions.** Compare every MCP tool signature against the interfaces above; ensure `RunStore` terminal sets, dashboard counts, and result persistence agree. Run Python compilation and `git diff --check`; report that automated and live runtime tests were not run under the current instruction.
- [ ] **Step 4: Commit.** Commit the documentation and integration slice with `git commit -m "docs: explain durable task handoff"`.
