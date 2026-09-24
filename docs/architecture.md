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
2. **Delegation runtime** accepts a bounded provider-neutral task, applies deterministic policy, executes a provider adapter, and returns a normalized result.

The primary coding agent remains responsible for decomposition, sequencing, arbitration, and final synthesis. CortexRelay does not add a second LLM planning loop.

The runtime is built around a provider-neutral `TaskSpec -> TaskResult` contract. Provider-specific CLI flags, response envelopes, authentication behavior, and error translation stay inside provider adapters. The first external runtime adapter targets Antigravity CLI.

### Workspace isolation

Read-only tasks are instructed not to modify files, and the Antigravity adapter compares git status before and after execution. Write-capable tasks can be isolated into linked git worktrees by the provider registry, so parallel writers do not share the same checkout.

### Protocol frontends

MCP is the first runtime frontend and exposes provider discovery, single delegation, and parallel delegation. Gemini CLI remote subagents use A2A; CortexRelay includes an A2A configuration helper, while a production A2A HTTP server remains a later milestone.

## Configuration ownership

CortexRelay still uses standard host configuration:

- `.codex/config.toml` or `~/.codex/config.toml` for Codex primary and global subagent defaults;
- `.codex/agents/*.toml` or `~/.codex/agents/*.toml` for Codex custom roles;
- repository or global `AGENTS.md` for Codex orchestration behavior;
- `.gemini/settings.json`, `.gemini/agents/*.md`, and `GEMINI.md` for Gemini CLI.

The configuration writers remain idempotent and back up managed files. The runtime is additive and does not replace host-native configuration.

## Design principles

- **Provider-neutral core:** model IDs and provider-specific CLI details are data or adapters, not core architectural dependencies.
- **Explicit roles:** workers have narrow responsibilities and sandbox modes.
- **Least privilege:** read-only contracts are checked for workspace changes, and write-capable tasks can use isolated git worktrees.
- **Preservation:** unrelated Codex configuration should survive installation.
- **Idempotence:** repeated initialization should update the managed policy instead of duplicating it.
- **Traceability:** generated files can be reviewed and committed like any other project configuration.
- **Progressive adoption:** teams can start with one repository before introducing user-level defaults.
