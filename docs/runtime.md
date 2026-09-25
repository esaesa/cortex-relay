# Runtime delegation

CortexRelay supports OpenCode, Antigravity CLI, and OpenAI Codex through one provider-neutral, session-first runtime with direct CLI, MCP, and A2A frontends.

The primary coding agent remains the orchestrator. CortexRelay does not replace its planning loop. A bounded `TaskSpec` describes orchestration policy, but provider execution happens through a durable `AgentSession`. The session preserves the provider-native conversation/thread/session handle, rich semantic events, child-agent topology, messages, and a lossless `TaskResult` for each completed turn.

## Runtime flow

```text
Codex / Gemini / another coding agent
              |
              | bounded TaskSpec
              v
        CortexRelay runtime
              |
      routing / budgets / workspace policy
              |
              v
        durable AgentSession
              |
      +-------+---------+---------+
      |                 |         |
 OpenCode session   AGY conversation   Codex app-server thread
      |                 |         |
      +---- semantic events / child sessions ----+
              |
              v
           TaskResult
```

The built-in providers are session-aware:

- **Codex:** app-server thread + streamed JSON-RPC notifications; `codex exec` remains a closed-end fallback.
- **OpenCode:** persistent OpenCode session, including continuation by session ID and native task/subagent visibility; `--pure run` remains a closed-end fallback.
- **Antigravity:** resumable conversation ID with `stream-json` events and native subagent metadata.

Additional providers can implement the same `ProviderAdapter` contract. Providers without a session API can intentionally remain `session_mode="closed_end"`; that path is useful for unknown future providers and CI-style one-shot work, but it no longer defines the core architecture.

## Install

The base package has no runtime Python dependencies:

```bash
python -m pip install -e .
```

Protocol transports are optional:

```bash
python -m pip install -e ".[mcp]"
python -m pip install -e ".[a2a]"
# or both
python -m pip install -e ".[runtime]"
```

OpenCode is discovered through `opencode`, Antigravity through `agy`, and Codex through `codex` on `PATH`. Authenticate each CLI using its normal upstream flow before delegation.

## Easy configuration workflow

Most users should start with the interactive commands rather than writing `.cortex-relay/config.toml` manually:

```bash
cortex-relay setup
cortex-relay config
cortex-relay launch
```

`setup` detects live providers/models, creates orchestrator and worker profiles, assigns roles, optionally configures a fallback, validates the result, and saves it atomically with a `.bak` backup when overwriting an existing config.

`config` edits the project or user config interactively. It supports role reassignment, profile editing/creation, and active-preset selection. If no config exists, it starts `setup`.

`launch` already defaults to `--role orchestrator`, so normal day-to-day use does not need the role flag.

## Runtime observability

Every delegation is assigned a task ID and recorded through a provider-neutral lifecycle:

```text
routing
   ↓
preparing
   ↓
running
   ↓
fallback   (when configured/needed)
   ↓
success | error | timeout | unavailable | cancelled
```

An interactive `cortex-relay launch` also creates a host-session record and propagates that session identity into the injected MCP process. This allows worker records to preserve the full host-to-worker route.

Use a second terminal for a live dashboard:

```bash
cortex-relay status --watch
```

One-shot and machine-readable forms:

```bash
cortex-relay status
cortex-relay status --active-only
cortex-relay status --json
cortex-relay history
cortex-relay history --json
```

The state captures:
- host session/profile/model/reasoning when launched through CortexRelay;
- orchestration task ID, role, profile, provider, model, reasoning and billing class;
- Cortex agent-session ID and provider-native session/thread/conversation ID;
- parent/root agent links and provider-native child sessions;
- durable semantic events such as response deltas, tools, lifecycle transitions and child updates;
- host↔agent message history and complete final text;
- fallback attempts, worktree path/branch, usage, tests, changed files, risks and errors.

State is deliberately kept outside the project checkout:
- Windows: `%LOCALAPPDATA%\CortexRelay\state`;
- `XDG_STATE_HOME` systems: `$XDG_STATE_HOME/cortex-relay`;
- fallback: `~/.local/state/cortex-relay`.

Override with `CORTEX_RELAY_STATE_DIR`.

Observability persistence is best-effort and cannot make execution fail. Completed task history can be removed with:

```bash
cortex-relay history --clear
# or keep active tasks and clear completed records:
cortex-relay status --clear-completed
```

The same data is exposed to MCP-capable orchestrators through the `status` and `history` tools.

## Inspect providers

```bash
cortex-relay providers
cortex-relay doctor --runtime-only
```

`doctor` still checks generated Codex/Gemini configuration by default. Missing optional runtime providers are reported but do not make a normal configuration check fail.

## Delegate from the CLI

Read-only review with Codex:

```bash
cortex-relay delegate \
  --provider codex \
  --model gpt-6-sol \
  --role reviewer \
  --reasoning high \
  --workspace . \
  "Review the authentication implementation for regressions"
```

The same task can be sent to Antigravity by changing `--provider codex` to `--provider antigravity`.

Machine-readable result:

```bash
cortex-relay delegate --json "Map the cache invalidation path"
```

Write-capable task:

```bash
cortex-relay delegate \
  --access workspace_write \
  --role implementer \
  "Implement the bounded change described in issue 42"
```

Write-capable CLI tasks use an isolated git worktree by default. The result includes `worktree_path` and `worktree_branch`. Use `--no-isolate-write` only when you intentionally want the provider to work directly in the supplied workspace.

## Runtime contracts

CortexRelay separates orchestration, execution state and completed-turn output:

### `TaskSpec`

Provider-neutral intent and policy: objective, role/profile/provider, workspace, access, reasoning/model, acceptance criteria, timeout, budget, quality gates and isolation.

### `AgentSession`

The execution primitive. It stores the Cortex session ID, provider-native session handle, provider/model/reasoning/access, task/parent/root links, state and metadata.

### `AgentEvent`

The durable semantic stream. Events include response deltas, provider-session binding, lifecycle changes, tool activity, diagnostics and native child-agent updates. Sensitive keys/tokens are redacted before persistence.

### `AgentMessage`

Durable host→agent, agent→host and agent→agent handoffs.

### `TaskResult`

The completed-turn envelope: status, provider/model, compact summary/evidence, changed files, commands, tests, risks, provider session identifier, usage metadata and `final_text`, the complete answer intended for the parent.

This lets the orchestrator inspect or continue a worker without learning each provider's native protocol.

## Execution profiles and role routing

Runtime profiles live in `~/.cortex-relay/config.toml` and/or a project `.cortex-relay/config.toml`. User configuration is loaded first and the nearest project configuration overrides it.

A profile is an executable resource, not a semantic role:

```toml
[profiles.zen-luna]
provider = "opencode"
model = "opencode/gpt-6-luna"
reasoning = "max"
access = "workspace_write"
billing_class = "zen-cheap"
fallbacks = ["codex-luna"]

[profiles.codex-luna]
provider = "codex"
model = "gpt-6-luna"
reasoning = "max"
access = "workspace_write"
billing_class = "chatgpt"

[roles]
implementer = "zen-luna"
tester = "zen-luna"
reviewer = "zen-luna"
```

When automatic provider routing is requested and role profiles are configured, the role must exist in the effective preset/base role map. Unknown roles fail before an async task is persisted or scheduled. Legacy role-to-provider routing remains available when no role profile mappings are configured; an explicit profile, provider, or model bypasses role mapping.

Profiles may be reused by any role, including `orchestrator`. Presets overlay the base role mapping:

```toml
active_preset = "cheap"

[presets.cheap]

[presets.premium-review.roles]
reviewer = "codex-luna"
```

Task resolution order is:

```text
explicit task profile
        ↓
selected preset + role mapping
        ↓
base role mapping
        ↓
legacy provider routing
```

An explicit legacy `--provider` or `--model` continues to bypass role profile mapping unless `--profile` is also supplied.

Inspect the effective configuration:

```bash
cortex-relay profiles
cortex-relay profiles --preset premium-review
cortex-relay profiles --json
```

### Interactive orchestrator host

When an OpenCode profile is assigned to the `orchestrator` role, CortexRelay can launch it as the actual interactive host:

```bash
python -m pip install -e ".[mcp]"
cortex-relay launch --role orchestrator
```

The launcher uses the profile's OpenCode model and reasoning variant, keeps the interactive OpenCode session user-facing, and injects a local CortexRelay MCP server into that host session. The orchestrator can then delegate bounded roles through the same profile registry.

Delegated OpenCode workers use OpenCode's session model as the primary path. The inherited `cortex-relay` MCP server remains disabled inside those worker sessions to prevent recursive re-delegation through CortexRelay, while OpenCode's native `task` subagents are allowed and mirrored into the Cortex agent tree. The old `--pure run` path remains available only as closed-end execution.

`cortex-relay launch` currently supports OpenCode host profiles. Other host agents can use CortexRelay via MCP or A2A.


Override a single task:

```bash
cortex-relay delegate --profile zen-luna --access workspace_write "Implement the change"
```

Profiles also define an access ceiling. A profile declared `access = "read_only"` cannot be used for a workspace-write task.

### Fallbacks

Profile fallbacks are named profiles:

```toml
[profiles.zen-luna]
provider = "opencode"
model = "opencode/gpt-6-luna"
reasoning = "max"
fallbacks = ["codex-luna"]
fallback_on = ["unavailable"]
```

The safe default is to fallback only when a provider is unavailable. Users can explicitly add `timeout` or `error`. Routing attempts are preserved in `TaskResult.metadata.routing_attempts`.

For write-capable tasks, each isolated attempt can produce its own worktree. Enabling fallback on errors/timeouts therefore requires inspecting the attempted worktrees rather than assuming only the final attempt changed files.

## OpenCode adapter

The preferred OpenCode path uses OpenCode's persistent session model. CortexRelay launches `opencode run --format json`, captures the provider session ID, and reuses it with `--session <id>` for follow-up turns. Session-mode workers may use OpenCode's native `task` subagents; their child session IDs are mirrored into `AgentSession` records. CortexRelay's own MCP server is disabled inside delegated OpenCode workers so native subagents cannot accidentally recurse through the same Cortex task.

The explicit closed-end path still uses `opencode --pure run --format json`. It is useful for one-shot jobs where session continuation and native child visibility are unnecessary.

OpenCode accepts provider/model IDs through `--model`, maps profile reasoning to `--variant`, streams JSON events, and receives a worker-only permission overlay. Read-only workers deny edits; external directories, skills and web tools remain denied; shell execution defaults to approval-required with a narrow repository/test/build allowlist.

Discover models/variants with:

```bash
cortex-relay models --provider opencode --refresh
cortex-relay models --provider opencode --refresh --verbose
cortex-relay models --provider opencode --refresh --json
```

## Codex adapter

The preferred Codex path uses `codex app-server --listen stdio://`. CortexRelay performs the JSON-RPC initialization handshake, starts or resumes a Codex thread, starts a turn with the Cortex result JSON Schema, consumes streamed notifications such as agent-message deltas/tool/turn events, and persists the thread ID as the provider session handle. Follow-ups resume the same thread through `thread/resume`.

`codex exec --json --output-schema --output-last-message` remains an explicit closed-end fallback rather than the primary orchestration transport.

CortexRelay does not pass Codex's dangerous approval/sandbox bypass option. Read-only Codex work also compares Git status before and after execution, while write-capable work uses the same provider-neutral worktree isolation as other adapters.

Current model/effort compatibility hints remain advisory rather than an allowlist; unknown future model IDs and reasoning names are passed through when they are not known to be incompatible.

## Antigravity adapter

Antigravity uses resumable headless conversations. The initial turn runs through `agy -p` with `stream-json`, an enforced result schema, model/effort selection and sandbox restrictions. CortexRelay stores the returned `conversation_id` and follow-up turns reuse it with `--conversation <id>`.

The rich stream is preserved as semantic agent events: response text deltas, tool updates and `subagent_info` including child conversation IDs, log URIs and workspace URIs. Native Antigravity children are mirrored into the Cortex agent tree and can be addressed through their provider session handles where supported.

CortexRelay discovers live Antigravity models with `agy models` and intentionally does not enable the provider's global auto-approval mode.

## Artifact-aware workflow control

Async dependency groups now carry workflow state as well as ordering.

A successful isolated write task can persist an immutable artifact with:
- source task ID and artifact ID;
- recorded base commit and worker HEAD;
- complete binary patch digest and patch file;
- changed files;
- provider/model and tests;
- normalized result digest.

Pass `inherit_workspace_from=<task-id>` on a downstream async task and include that task ID in `depends_on`. After the dependency succeeds, CortexRelay creates/loads its artifact and applies it to the downstream task's fresh worktree before launching the provider. The user's source checkout stays untouched. Inheritance also works for read-only downstream workers.

Every async task receives provider-neutral trace metadata. A child may name `parent_task_id`; CortexRelay carries the trace/root IDs, ancestry, depth and `max_depth`, rejecting loops or excessive nesting.

Quality gates are workflow-level acceptance checks after provider execution. A provider can finish successfully while CortexRelay records `failed_gate` when required changed files/tests/review evidence are absent, changed paths exceed the allowed scope, or failed-test limits are exceeded.

Task budgets can cap tokens and provider-reported cost. Live token events request cancellation when a task crosses its token budget; final normalized usage is checked again. Project-level budgets can cap group/session tokens and premium task counts before new work starts.

The scheduler is configurable:

```toml
[scheduler]
max_workers = 6

[scheduler.providers]
opencode = 3
antigravity = 2
codex = 1

[scheduler.profiles]
premium-review = 1
```

Queued tasks expose `queue_position` and `queued_reason`, such as a dependency wait or provider concurrency saturation. Higher `priority` values are considered first.

State retention is configurable:

```toml
[state]
retention_days = 30
max_completed_tasks = 1000
max_event_log_mb = 10
cleanup_on_start = true
```

Use `cortex-relay gc --dry-run` before pruning. Active tasks, their required dependency records, and Git worktrees are never deleted by state GC.

Workflow inspection:

```bash
cortex-relay group feature-auth
cortex-relay group feature-auth --watch
cortex-relay analyze-routing
```

Routing analysis is descriptive only: success/fallback/gate/budget rates plus median duration, tokens and provider-reported cost. CortexRelay does not automatically rewrite route configuration from these metrics.

A2A execution now forwards the same bounded normalized progress events used by MCP/terminal observability as repeated working-status messages.

## MCP

Install the optional MCP dependency, then run a local stdio server:

```bash
cortex-relay serve --transport mcp
```

Or Streamable HTTP:

```bash
cortex-relay serve \
  --transport mcp \
  --mcp-transport streamable-http \
  --host 127.0.0.1 \
  --port 8765
```

The MCP surface has two control layers.

Task/workflow tools:

- `providers`, `profiles`, `status`, `history`
- `delegate`, `delegate_parallel`, `delegate_async`
- `task_status`, `task_events`, `task_wait`, `task_output`, `task_cancel`, `tasks`
- `task_artifact`, `task_worktree`, `task_diff`, `task_apply`, `task_discard`

Agent/session tools:

- `agents`
- `agent_get`
- `agent_events`
- `agent_messages`
- `agent_children`
- `agent_send`
- `agent_close`

`delegate` and `delegate_parallel` are synchronous: the MCP request waits for the provider work to finish. Use them for short, bounded tasks. A client-side timeout does not cancel workers already launched by `delegate_parallel` and may prevent their results from reaching the caller. For longer work or parallel orchestration, call `delegate_async` for each task and retain the returned task IDs. Each task may choose a profile or an explicit `opencode`, `codex`, or `antigravity` provider; write-capable tasks are isolated into separate git worktrees by default.

`delegate_async` starts a task and immediately returns its full task ID. Once provider execution begins, the task record exposes `agent_session_id`. Use `agent_events` for the richer semantic stream, `agent_children` to traverse native or Cortex-managed children, `agent_messages` for durable handoffs, and `agent_send` to continue a resumable provider session without creating a fresh logical worker. It also accepts dependency inheritance, priority, trace-parent/depth, budgets and quality-gate inputs. Use `task_status` to read the latest observable tool activity and `task_events(task_id, after_sequence=0, limit=20)` to fetch subsequent events with a cursor. Pass the returned `next_sequence` into the next `task_events` call. `task_wait` retrieves the full normalized result (`timeout_seconds=0` polls), including `final_text`, the worker's complete answer intended for the parent. `task_output(task_id, offset=0, max_chars=65536)` reads that same durable answer in bounded chunks for large outputs and returns `next_offset`, `total_chars`, `complete`, and a SHA-256 digest. `task_cancel` requests cancellation. Each `task_wait` call waits at most five seconds so it stays within MCP client request deadlines, even if given a larger value. `tasks(workspace=".", group_id=None)` lists persisted async jobs in a workspace. Set `access=workspace_write` explicitly for implementation tasks. The synchronous `delegate` tool remains available.

Async task identity, status, events, the complete normalized result, and the child's complete final text are stored on disk. The result JSON retains `final_text`, while a dedicated UTF-8 output file provides lossless paged retrieval. A new MCP server can inspect a completed task by ID and return the exact result or page through the exact final answer. If the owning server exits while a task is active or queued, the task becomes `interrupted`; CortexRelay does not resume or kill the unknown worker. A task still owned by a live other server stays active, and `task_cancel` reports `owned_elsewhere` from this server. Older terminal records without full results return `result_unavailable`. Queued tasks are not relaunched automatically after owner loss. The dashboard remains a bounded operational view, while `agent_events` is the richer durable semantic stream. CortexRelay does not expose hidden chain-of-thought; it stores observable provider messages/deltas, tool/lifecycle events, usage, diagnostics and child-agent metadata.

`delegate_async` also accepts `group_id` and `depends_on`, a list of earlier async task IDs in the same workspace. A dependent task remains `queued` without occupying a worker until all prerequisites succeed. If one fails, the dependent becomes `blocked` and `blocked_by` names the failed IDs. For example, submit two explorers with `group_id="feature-auth"`, then an implementer with `depends_on=[explorer_a_id, explorer_b_id]`; inspect the group with `tasks(group_id="feature-auth")`. Group membership does not change permissions or merge worktree contents.

For an isolated write task, `task_worktree(task_id, attempt=None)` reports its recorded source repository, path, branch, base commit, existence, Git status, and handoff state. `task_diff(task_id, attempt=None, max_bytes=65536)` returns changed files, a SHA-256 digest, and a bounded patch preview for tasks with a recorded base commit. It includes committed, staged, unstaged, and nonignored untracked files in the worker worktree. Provider-reported files ignored by Git are listed as excluded, and `task_apply` refuses an incomplete patch. If profile fallback created several worktrees, pass an explicit `attempt` number. Legacy tasks without a base commit receive an inspection-only candidate diff against the current source/worktree merge base (or current `HEAD` if histories do not meet); it is labeled approximate, does not claim a complete patch, and cannot be applied or discarded through the managed handoff.

After reviewing a terminal task, `task_apply(task_id, attempt=None)` checks that the source checkout is clean, verifies the complete worker patch against the current target, and applies it. It leaves the source changes uncommitted and preserves the worker worktree. The operation is explicit and a second call reports the prior application. `task_discard(task_id, attempt=None)` first returns a five-minute confirmation token bound to the current worktree snapshot. Review its exact path, changed files, and any `excluded_ignored_files`; ignored files are removed along with the worktree. Then call `task_discard(task_id, attempt=None, confirmation_token=token)` to remove that managed worktree and its branch. A changed snapshot or an active task is refused. Task results and events remain available after discard.

`cortex-relay status --watch` lists active requests only and shows the host only while its launcher holds a live session lease. Closed or crashed launchers are shown as interrupted, and their completed task rows do not keep repeating. Use `cortex-relay status` or `cortex-relay history` to review past requests. `status --watch -v` shows five recent actions per active task. `-vv` shows fifteen actions, output and error previews, subagent states, and the current plan when available. The compact watcher shows the last provider event and a process heartbeat. A stale worker heartbeat is reported as "not recently observed", rather than proof that the worker is still alive. Normalized events are appended to `<state-directory>/events/<task-id>.jsonl`; each task record keeps the most recent 50. Clearing completed tasks also removes their event logs. Provider response text is excluded; outputs and diagnostics are truncated and common credential patterns are redacted before storage.

## A2A

Install the optional A2A runtime:

```bash
python -m pip install -e ".[a2a]"
```

Start a local remote agent backed by Codex:

```bash
cortex-relay serve \
  --transport a2a \
  --host 127.0.0.1 \
  --port 8765 \
  --a2a-profile zen-luna \
  --a2a-role reviewer \
  --a2a-workspace .
```

The command prints the Agent Card URL and a Gemini CLI `kind: remote` definition. The default Agent Card is:

```text
http://127.0.0.1:8765/.well-known/agent-card.json
```

The server exposes JSON-RPC at `/a2a/jsonrpc`, HTTP+JSON at `/a2a/rest`, and a health endpoint at `/healthz`.

CortexRelay uses the A2A Python SDK v1 server API and enables v0.3 compatibility on the same JSON-RPC and REST endpoints. The Agent Card advertises both v1.0 and v0.3-compatible interfaces. Current Gemini CLI normalizes v1 Agent Card interface fields before creating its A2A client, so it can consume this card directly.

### A2A policy boundary

A2A input is intentionally text-only at the CortexRelay boundary. Profile/preset, provider, model, reasoning effort, role, workspace, timeout, and access mode are server-side policy configured when the server starts. An inbound remote prompt cannot switch to another directory or escalate from read-only to write access.

For write-capable A2A tasks, `--a2a-isolate-write` is enabled by default and reuses CortexRelay's git-worktree isolation.

### Cancellation

A2A cancellation propagates through a shared cancellation event to the provider subprocess runner. OpenCode, Codex, or Antigravity processes are terminated instead of continuing silently after the remote task is canceled.

### Network safety

The A2A server is unauthenticated in version 0.8. CortexRelay therefore refuses to bind A2A to a non-loopback interface unless `--a2a-allow-remote` is explicitly supplied. Keep the default loopback binding for local Gemini CLI integration.

## Provider development

New providers advertise their session capabilities explicitly:

```python
class ProviderAdapter(ABC):
    name: str

    def capabilities(self) -> ProviderCapabilities:
        ...

    def execute_session(self, task: TaskSpec) -> TaskResult:
        ...

    def continue_session(
        self,
        task: TaskSpec,
        provider_session_id: str,
    ) -> TaskResult:
        ...

    def execute(self, task: TaskSpec) -> TaskResult:
        ...  # closed-end fallback
```

A provider with a native thread/conversation/session API should set `persistent_sessions`, `streaming_events`, `native_subagents` and `child_messaging` accurately. A provider with no richer control channel may deliberately stay `session_mode="closed_end"`; `execute_session()` defaults to that one-shot path.

Provider-specific command flags, response envelopes, authentication behavior and error translation remain inside adapters. Routing, session/event/message semantics and result semantics belong in the core.
