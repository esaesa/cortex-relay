# Durable task lifecycle and worktree handoff

Date: 2026-09-24

## Purpose

An MCP client must be able to reconnect and inspect an asynchronous CortexRelay
task by ID. A worker that loses its controlling MCP server must have an honest
terminal state. Implementer work must be inspectable and explicitly transferable
from its isolated worktree. An orchestrator must be able to queue dependent work
without occupying worker slots while it waits.

This milestone covers durable async recovery, worktree handoff, and dependency
groups. Native MCP Tasks, MCP Apps, general priority scheduling, quality gates,
and A2A progress are separate milestones.

## Current boundaries

`AsyncTaskManager` owns futures in one MCP process. All of its task control
methods call `_job(task_id)`, so persisted records cannot be reached after that
process exits. `RunStore` stores a task JSON file and an event JSONL file in a
workspace-specific state directory, but it does not persist the full
`TaskResult` or a global task-to-workspace lookup. `WorktreeManager` creates a
branch and worktree with another short ID; the task record has a path and branch
but no base commit. Write-capable workers commonly leave uncommitted changes.

## Selected architecture

A `TaskService` becomes the MCP-facing boundary. It owns an in-process scheduler
and uses `RunStore` for durable identity, state, results, ownership, and events.
The service methods are `submit`, `status`, `events`, `wait`, `cancel`, `tasks`,
`worktree`, `diff`, `apply`, and `discard`. MCP tools adapt arguments and return
these method results. Provider execution and the existing synchronous `delegate`
path remain in `ProviderRegistry`.

The existing `AsyncTaskManager` may be refactored into this service rather than
duplicated. One task ID, generated as `task-` plus a full UUID4 hex value, owns
the task record, MCP handle, and all execution attempts. Isolated branch and
worktree names include that ID and an attempt number, since profile fallback
can create more than one worktree. The role remains a record field. Existing
short IDs remain readable through a legacy lookup; they are never reused for
newly submitted tasks.

## Durable task identity and state

`RunStore` writes an atomic task index entry under its state root for each new
async task. The entry contains the exact task ID, resolved source workspace,
record schema version, and creation time. Lookup validates the task ID, reads
the index, then verifies that the referenced task record has the same ID and
workspace. A lazy scan of task records under the state root can resolve old
IDs; the scan does not mutate them. Clearing a task removes its index entry,
result file, and event log with its task record.

The task record gains `async`, `owner_instance_id`, `group_id`, `depends_on`,
`blocked_by`, `worktree_attempts`, `handoff_status`, `result_path`, and
`owner_heartbeat_at` fields.
The complete normalized `TaskResult.to_dict()` is written atomically to a
separate per-task result file before the task record becomes terminal. A
terminal new-format record without a readable result is reported as a storage
error; a legacy record without a full result reports `result_unavailable`. The
service must not fabricate success. New task state writes use per-task in-process and
cross-process locks so progress, heartbeat, cancellation, scheduler, and
completion updates cannot overwrite each other. The JSONL cursor sequence is
allocated under the same task lock.

The service has an owner instance ID and holds an operating-system file lock
for its lifetime. Each async task records that owner. On startup and on lookup,
the service reconciles nonterminal tasks whose owner lock is no longer held:
`routing`, `preparing`, `running`, and `fallback` become `interrupted`;
`queued` becomes `interrupted` as well. If a complete persisted result exists
but the terminal record update was interrupted, reconciliation finishes that
record from the result instead. It preserves the last progress and
worktree location and records why control was lost. A live owner in another MCP
process remains authoritative even if its most recent provider event is old.
The owner updates its own heartbeat while running. An owner with a stale
heartbeat but a held lock is shown as unresponsive, not declared dead solely
from elapsed time. A restarted service does not adopt or kill a former
provider process.

`task_status` and `task_events` read the indexed task from disk when it has no
local future. `task_wait` keeps its current five-second maximum; it returns the
persisted result for any terminal task and persisted current state otherwise.
`task_cancel` cancels a local active or queued job. For a task owned by another
live process it returns a clear `owned_elsewhere` response; for an interrupted
or other terminal task it returns the persisted state without claiming to stop
a process. `tasks(workspace)` includes indexed async tasks from disk, not only
the current process's `_jobs` map.

`interrupted` and `blocked` are terminal task lifecycle statuses. They have
synthetic normalized results so `task_wait` behaves consistently; the result
explicitly says that no provider completion was observed. The CLI watcher and
history count and render both states accurately.

## Dependency groups

`delegate_async` gains optional `group_id` and `depends_on: list[str]` arguments.
An orchestrator submits prerequisite tasks first, receives their IDs, then
submits dependents that name those IDs. A dependency must exist, be an async
task in the same resolved workspace, and have a unique ID. A task cannot depend
on itself. Since dependencies can only reference already-created tasks and
cannot be edited, cycles cannot be introduced. `group_id` is an optional
validated label; dependencies may be used without a group label.

A task with unmet dependencies is persisted as `queued` but is not submitted
to the thread pool. The scheduler starts it once all dependencies are
`success`. If any dependency becomes `error`, `timeout`, `unavailable`,
`cancelled`, `interrupted`, or `blocked`, the dependent becomes terminal
`blocked`, with `blocked_by` and a synthetic result. A queued task can be
cancelled directly. When a task finishes, the scheduler re-evaluates its local
dependents under a lock, then dispatches only ready work. `tasks(workspace,
group_id=...)` returns the group in dependency order and includes queue and
blocked states. No worker thread waits on another worker's future.

Queued tasks belonging to an owner that exits are marked `interrupted`, not
automatically relaunched after restart. This keeps restart recovery truthful
without introducing duplicate billable work. A later resume feature can build
on persisted specifications and explicit user action.

## Worktree identity and inspection

`ProviderRegistry` passes the owning task ID and attempt number to
`WorktreeManager.create` and records the resolved base commit SHA before
creating the worktree. Every fallback attempt gets its own recorded worktree.
Only a worktree whose canonical path, branch, base SHA, and source repository
match the stored attempt may be managed by handoff tools. Existing tasks
without that provenance are inspectable by path but cannot be applied or
discarded by these tools. No handoff tool accepts an arbitrary filesystem path
from the caller.

`task_worktree(task_id, attempt=...)` returns the source repository, worktree
path, branch, base commit, existence, task status, handoff status, and a concise
Git status. With one attempt the argument may be omitted; with several, an
ambiguous handoff call requires an explicit attempt number.
`task_diff(task_id, attempt=..., max_bytes=...)` is read-only and returns a bounded file
summary plus a bounded patch preview. To include committed, staged,
unstaged, and untracked nonignored files without changing the worker's index,
the implementation builds a temporary alternate Git index from the base
commit, stages the worktree's current content into that index, and computes a
binary diff against the base. A truncated preview is explicitly labeled and
is never used as the apply input. Binary file changes are listed even when
their content is omitted from an MCP response.

## Explicit apply and discard

`task_apply(task_id, attempt=...)` requires a terminal task and a proven managed worktree.
It refuses a dirty source checkout, a missing worktree, an already applied or
discarded task, or a changed worktree/base since its preflight. It computes the
complete patch through the alternate index, checks application against the
current target HEAD, and applies it only after that check succeeds. It does not
commit, merge, delete, or automatically accept the worker's result. The
response lists changed files and records the target HEAD and patch digest in
`handoff_status=applied`. A repeated call reports the prior application and
does not apply the patch twice. Failure leaves the isolated worktree available
for inspection. A failed application reports any target changes precisely;
it does not silently claim rollback.

`task_discard(task_id, attempt=...)` is a two-call workflow. The first call returns a
short-lived confirmation token bound to the exact task, worktree path, branch,
base SHA, HEAD, and current worktree status. The second call supplies that
token. It refuses a running task, a changed snapshot, an unrecognized path,
or a path outside CortexRelay's managed worktree root. It removes the worktree
and its task branch, then records `handoff_status=discarded` while preserving
the task result and event history. No task is automatically applied or
discarded.

## Interface and compatibility

Existing `delegate_async`, `task_status`, `task_events`, `task_wait`,
`task_cancel`, and `tasks` tool names and their current required arguments
remain valid. New optional arguments extend `delegate_async` and `tasks`.
The new tools are `task_worktree`, `task_diff`, `task_apply`, and
`task_discard`. Returned records add fields but retain existing keys. The
CLI status/history commands recognize `queued`, `blocked`, and `interrupted`.
The current synchronous `delegate` behavior is unchanged.

## Operational limits and failure handling

The state root is local machine storage. This design supports MCP server
restarts on that machine; it is not a distributed queue or remote process
supervisor. Process lock files and task index records are retained only as
needed to distinguish a live owner from a dead one. Corrupt or missing index,
task, result, or Git data yields an explicit error with the task ID and path
context; the service does not infer success. Index and result files follow the
current local state-directory access assumptions. Handoff tools limit response
size, avoid exposing raw provider output, and retain the existing event
redaction rules.

## Acceptance criteria

1. After an MCP restart, `task_status`, `task_events`, `task_wait`, and
   `tasks` can inspect a completed async task and return its exact result.
2. An owner loss changes active and queued tasks to `interrupted` without
   reporting an unobserved provider result or killing an unknown process.
3. Two independent successful prerequisites release a queued dependent;
   a failed or interrupted prerequisite blocks it without consuming a worker.
4. `task_diff` reports committed and uncommitted tracked changes plus
   untracked nonignored files, with a bounded MCP preview.
5. `task_apply` changes only a clean target checkout after preflight and
   leaves the worktree intact; `task_discard` requires a matching confirmation
   token and removes only a proven managed worktree.
6. Old task records remain visible, while unsupported handoff actions on
   records without provenance fail clearly.
