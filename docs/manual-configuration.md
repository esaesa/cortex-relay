# Manual configuration

CortexRelay is only a convenience layer. You can reproduce the complete setup manually with standard Codex files.

This guide uses the default policy:

```text
Primary: GPT-6 Astra / low
Workers: GPT-5.6 Luna / xhigh
```

## 1. Choose project or user scope

For one repository, create:

```text
<project>/.codex/
```

For a reusable default across repositories, use:

```text
~/.codex/
```

Project scope is usually better for teams because the configuration can be reviewed and versioned.

## 2. Configure the primary model and global subagent defaults

Create or edit `.codex/config.toml` for project scope, or `~/.codex/config.toml` for user scope.

Add or merge these values:

```toml
model = "gpt-6-astra"
model_reasoning_effort = "low"

[agents]
enabled = true
max_concurrent_threads_per_session = 6
default_subagent_model = "gpt-5.6-luna"
default_subagent_reasoning_effort = "xhigh"
interrupt_message = true
```

Do not replace unrelated settings already present in your config. Merge the root keys and the `[agents]` keys into the existing file.

## 3. Create custom worker roles

Create `.codex/agents/` and add the following files.

### `.codex/agents/explorer.toml`

```toml
name = "explorer"
description = "Map relevant files, symbols, dependencies, and execution paths before implementation."
model = "gpt-5.6-luna"
model_reasoning_effort = "xhigh"
sandbox_mode = "read-only"
developer_instructions = """
Locate the smallest relevant set of files and symbols. Trace the real execution path and dependencies. Prefer targeted reads over broad scans. Do not modify files. Return compact evidence to the parent agent.
"""
```

### `.codex/agents/architect.toml`

```toml
name = "architect"
description = "Evaluate design boundaries, abstractions, scalability, and implementation strategy."
model = "gpt-5.6-luna"
model_reasoning_effort = "xhigh"
sandbox_mode = "read-only"
developer_instructions = """
Define boundaries, responsibilities, interfaces, invariants, trade-offs, and migration risks. Prefer existing abstractions. Do not edit code unless explicitly asked. Return a concise recommendation.
"""
```

### `.codex/agents/implementer.toml`

```toml
name = "implementer"
description = "Implement a well-defined change after scope and acceptance criteria are clear."
model = "gpt-5.6-luna"
model_reasoning_effort = "xhigh"
sandbox_mode = "workspace-write"
developer_instructions = """
Implement only the requested scope. Follow repository conventions. Preserve compatibility unless explicitly changed. Add tests when behavior changes. Avoid unrelated refactors. Report changed files and validation.
"""
```

### `.codex/agents/tester.toml`

```toml
name = "tester"
description = "Design and run focused validation for changed behavior and regression risks."
model = "gpt-5.6-luna"
model_reasoning_effort = "xhigh"
sandbox_mode = "workspace-write"
developer_instructions = """
Prove behavior rather than only covering changed lines. Reuse the existing test framework. Run focused checks first. Report commands, outcomes, failures, and remaining untested risk.
"""
```

### `.codex/agents/reviewer.toml`

```toml
name = "reviewer"
description = "Independently review correctness, security, regressions, and missing tests."
model = "gpt-5.6-luna"
model_reasoning_effort = "xhigh"
sandbox_mode = "read-only"
developer_instructions = """
Review independently. Prioritize correctness, authorization, data leakage, concurrency, error handling, compatibility, architecture consistency, and missing tests. For every material finding give severity, location, failure scenario, and correction. Return PASS when no material issue exists.
"""
```

## 4. Teach the primary model to orchestrate

For project scope, add the following section to the repository-root `AGENTS.md`.

For user scope, add it to `~/.codex/AGENTS.md`.

```markdown
## CortexRelay orchestration

- Treat the primary model as the orchestration and arbitration layer, not the default bulk worker.
- For non-trivial tasks, decompose work into narrow tasks and delegate suitable work to subagents.
- Prefer parallel delegation when tasks are independent.
- Give workers exact objectives, scope, constraints, expected evidence, and acceptance criteria.
- Keep worker reports compact so the primary model consumes summaries rather than raw repository volume.
- Resolve contradictory worker findings with targeted follow-up.
- Do not override the configured worker model unless explicitly requested or unavailable.
- The primary model owns final synthesis and communication with the user.
```

Codex reads global and project `AGENTS.md` files with project-specific instructions taking precedence as it walks toward the working directory.

## 5. Verify the active instructions

From the project root:

```bash
codex --ask-for-approval never "Summarize the current instructions."
```

You should see the CortexRelay orchestration policy reflected in the response.

## 6. Test delegation

Try a prompt that explicitly asks for parallel workers:

```text
Map the repository, identify the implementation path for this feature, then implement and review it. Use explorer, architect, implementer, tester, and reviewer where useful. Keep the primary agent focused on orchestration and final synthesis.
```

## 7. Tune the policy

The architecture is model-agnostic. You can change:

```toml
model = "YOUR_PRIMARY_MODEL"
model_reasoning_effort = "medium"

[agents]
default_subagent_model = "YOUR_WORKER_MODEL"
default_subagent_reasoning_effort = "high"
```

Then update the `model` and `model_reasoning_effort` values in each custom agent file if you want them explicitly pinned.

## Reasoning effort note

The current Codex config reference lists `minimal`, `low`, `medium`, `high`, and `xhigh` for `model_reasoning_effort`. The subagent documentation may also describe a Max level on surfaces/models that support it. Use the value accepted by the surface you are configuring rather than assuming UI labels and TOML values are interchangeable.
