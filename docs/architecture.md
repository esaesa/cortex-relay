# Architecture

## Core idea

CortexRelay separates **decision complexity** from **execution volume**.

A strong primary model handles the parts of software work where judgment has the highest leverage:

- interpreting ambiguous user intent;
- choosing task boundaries;
- sequencing dependencies;
- deciding what can run in parallel;
- resolving contradictory worker findings;
- deciding whether evidence is sufficient;
- producing the final synthesis.

Specialized workers handle bounded tasks that can be described with clear inputs and acceptance criteria.

## Information-flow objective

The preferred flow is:

```text
large repository
    -> worker reads
    -> compact evidence
    -> primary model decides
```

The anti-pattern is:

```text
large repository
    -> worker reads
    -> worker emits another huge transcript
    -> primary model rereads everything
```

Cost-aware orchestration only works if the relay boundary also compresses context.

## Default worker roles

### Explorer

Read-only repository discovery. Produces paths, symbols, dependencies, and execution-flow evidence.

### Architect

Read-only design analysis. Produces boundaries, interfaces, invariants, alternatives, migration risks, and trade-offs.

### Implementer

Workspace-write role that owns a narrow change after the problem is sufficiently specified.

### Tester

Workspace-write role focused on proving behavior and detecting regressions.

### Reviewer

Read-only independent challenge function. It assumes the implementation may be wrong and looks for concrete defects.

## Delegation DAG

A typical change can be represented as:

```text
                 primary model
                      |
             decompose / scope
                      |
        +-------------+-------------+
        |             |             |
     explorer      architect     test mapper
        |             |             |
        +-------------+-------------+
                      |
                 implementer
                      |
              +-------+-------+
              |               |
           tester          reviewer
              |               |
              +-------+-------+
                      |
                 primary model
                final synthesis
```

Not every task needs every node. A good orchestrator avoids spawning agents whose expected information gain is lower than their coordination cost.

## Escalation policy

Workers should not silently broaden scope when they encounter ambiguity.

Recommended sequence:

1. Worker reports the ambiguity and evidence.
2. Primary model decides whether the ambiguity matters.
3. Primary model launches a targeted follow-up worker if additional evidence is cheap.
4. Primary model resolves the conflict or, if necessary, handles the difficult reasoning directly.

This keeps expensive reasoning concentrated at the points where uncertainty is highest.

## Runtime boundary

CortexRelay now has two deliberately separate layers:

1. **Configuration bootstrap** writes standard Codex or Gemini CLI configuration, role files, and orchestration instructions.
2. **Delegation runtime** accepts a bounded provider-neutral task, applies deterministic policy, executes a provider adapter, and returns a lossless normalized result: the complete child final answer plus structured summary/evidence metadata.

The primary coding agent remains responsible for decomposition, sequencing, arbitration, and final synthesis. CortexRelay does not add a second LLM planning loop.

The runtime is built around three provider-neutral primitives:

```text
TaskSpec        orchestration intent, policy and acceptance
    ↓
AgentSession    live/resumable provider conversation and child topology
    ↓
TaskResult      completed-turn envelope and full final answer
```

`AgentSession` is the execution primitive. It carries a Cortex session ID, provider-native session/thread/conversation ID, parent/root links, provider/model/reasoning/access metadata and durable state. `AgentEvent` stores semantic live activity such as text deltas, tool work, lifecycle transitions and child updates. `AgentMessage` stores host↔agent and agent↔agent handoffs. `TaskResult.final_text` remains the complete semantic answer produced for the parent; `summary`, evidence, tests, changed files, commands, and risks are machine-readable indexes over that answer rather than replacements for it.

Provider-specific transports remain inside adapters. Codex uses app-server threads as its preferred session path, OpenCode uses its persistent session/server model, and Antigravity uses resumable conversation IDs plus `stream-json`. A provider may still expose `session_mode="closed_end"` for one-shot execution when no richer control channel exists.

### Execution profiles

Runtime routing is separated into two layers:

```text
semantic role
    ↓
named execution profile
    ↓
provider adapter + model + reasoning + billing path
```

A role such as `implementer` or `reviewer` never has to encode a provider. It points to a named profile. The same underlying model can therefore exist as separate resources such as `luna-via-zen` and `luna-via-codex`.

Profile resolution is deterministic: an explicit task profile has highest priority, followed by preset/base role mappings, followed by the legacy provider routing policy. This preserves compatibility while allowing users to change the orchestration topology entirely through configuration.

User-level profile configuration is a base layer and project configuration overrides it. Presets only overlay role assignments; they do not mutate the profile definitions themselves.

Fallbacks are profile-to-profile edges. The default transition condition is provider unavailability, not arbitrary execution failure, so CortexRelay does not silently replace a failed implementation with a different writer unless the user explicitly opts into that policy.

### Workflow artifacts and lineage

Dependency edges are not assumed to imply filesystem state. A write worker's isolated worktree can be normalized into an immutable task artifact containing the exact binary patch and provenance. Downstream workers explicitly inherit that artifact into their own fresh worktree. This separates three concerns:

```text
ordering dependency
        ↓
immutable artifact lineage
        ↓
explicit source-checkout handoff
```

The scheduler never mutates the user's source checkout merely because one task depends on another. `task_apply` remains an explicit handoff after inspection.

Every async task can carry a delegation context with trace/root/parent IDs, ancestry and depth. This gives provider-neutral loop protection independent of any host agent's native subagent controls.

### Workflow scheduling and acceptance

TaskService is the protocol-neutral workflow control plane. It manages durable task handles, dependency readiness, priorities, workspace/provider/profile concurrency limits, group/session budgets, artifact inheritance, and post-execution quality gates.

Provider success and workflow acceptance are deliberately separate. A provider result may be normalized successfully but transition to `failed_gate` or `budget_exceeded` before dependent tasks are released.

The task-control interface is defined as a protocol so the current custom MCP tools and a future native MCP Tasks adapter can share the same scheduler/persistence implementation.

### Workspace isolation

Read-only tasks are instructed not to modify files, and all current runtime adapters compare git status before and after execution. Write-capable tasks can be isolated into linked git worktrees by the provider registry, so parallel writers do not share the same checkout.

### Agent/session and observability boundary

The registry creates a Cortex `AgentSession` before launching provider work, then mirrors provider-native events into a durable semantic event log. Native provider child IDs are upserted as child `AgentSession` records instead of being flattened into progress text. Cortex-managed child tasks also inherit the parent agent-session link, so both kinds of delegation appear in the same tree.

Dashboard-oriented `ProgressEvent` records are derived from this richer stream and may remain bounded. The authoritative session stream is separate: response deltas, lifecycle events, provider-session bindings, tool activity and child updates are preserved through `agent_events`. Sensitive keys/tokens in rich event payloads are redacted before persistence.

State is external to the Git checkout and keyed by the resolved workspace path. Terminal results are persisted as JSON and complete child final answers are additionally persisted as dedicated UTF-8 output files so large answers can be retrieved in bounded chunks without semantic truncation. Agent sessions, semantic events and message histories are persisted alongside task state so a new MCP process can inspect completed or resumable sessions without relying on process-local futures. The CLI status snapshot joins both stores by resolved workspace, so `status --watch` is a unified view over task lifecycle and agent-session lifecycle, including mirrored native children and recent semantic activity.

Interactive host launches propagate only non-secret session identity fields (session ID, host profile/model/reasoning and workspace) to the injected MCP process. Provider credentials are not copied into observability records.

### Protocol frontends

MCP and A2A are peer frontends over the same runtime. MCP exposes provider/profile discovery, runtime status/history, single/parallel delegation, and first-class agent controls for starting direct persistent sessions synchronously or asynchronously, waiting for durable turn results, listing sessions, reading semantic events/messages, traversing children, sending follow-ups, and closing Cortex session handles. Direct `agent_start`/`agent_start_async` sessions bypass workflow scheduling/worktree/artifact semantics; task delegation remains the workflow-oriented path. Asynchronous direct starts expose the session before provider execution completes so events can be consumed immediately. A2A exposes a server-side fixed delegation policy through an Agent Card, JSON-RPC, and HTTP+JSON so remote agents such as Gemini CLI can send bounded text tasks without controlling local filesystem or permission policy.

## Configuration ownership

CortexRelay still uses standard host configuration:

- `.codex/config.toml` or `~/.codex/config.toml` for Codex primary and global subagent defaults;
- `.codex/agents/*.toml` or `~/.codex/agents/*.toml` for Codex custom roles;
- repository or global `AGENTS.md` for Codex orchestration behavior;
- `.gemini/settings.json`, `.gemini/agents/*.md`, and `GEMINI.md` for Gemini CLI;
- `~/.cortex-relay/config.toml` and project `.cortex-relay/config.toml` for provider-neutral execution profiles, role mappings, presets, and fallback policy.

The configuration writers remain idempotent and back up managed files. The runtime is additive and does not replace host-native configuration.

## Design principles

- **Provider-neutral core:** roles point to execution profiles; model IDs, billing paths, and provider-specific CLI details are data or adapters, not core architectural dependencies.
- **Explicit roles:** workers have narrow responsibilities and sandbox modes.
- **Least privilege:** read-only contracts are checked for workspace changes, write-capable tasks can use isolated git worktrees, and A2A callers cannot alter the server-fixed workspace/access policy.
- **Preservation:** unrelated Codex configuration should survive installation.
- **Idempotence:** repeated initialization should update the managed policy instead of duplicating it.
- **Traceability:** generated files can be reviewed and committed like any other project configuration; runtime task/session state is inspectable without modifying the repository.
- **Progressive adoption:** teams can start with one repository before introducing user-level defaults.
