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

## Configuration ownership

CortexRelay uses standard Codex configuration:

- `.codex/config.toml` or `~/.codex/config.toml` for primary and global subagent defaults;
- `.codex/agents/*.toml` or `~/.codex/agents/*.toml` for custom roles;
- repository or global `AGENTS.md` for orchestration behavior.

The CLI is only an idempotent configuration writer with backups. It is not a runtime proxy.

## Design principles

- **Model-agnostic:** model IDs are data, not architectural dependencies.
- **Explicit roles:** workers have narrow responsibilities and sandbox modes.
- **Least privilege:** read-only roles remain read-only.
- **Preservation:** unrelated Codex configuration should survive installation.
- **Idempotence:** repeated initialization should update the managed policy instead of duplicating it.
- **Traceability:** generated files can be reviewed and committed like any other project configuration.
- **Progressive adoption:** teams can start with one repository before introducing user-level defaults.
