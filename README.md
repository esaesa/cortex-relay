# CortexRelay

**Provider-neutral coding-agent delegation plus cost-aware orchestration configuration for Codex and Gemini CLI.**

CortexRelay is an open-source delegation runtime and configuration toolkit. It lets a primary coding agent keep ownership of planning and final synthesis while delegating bounded work through a common task/result contract to external providers such as OpenCode, Antigravity CLI, and OpenAI Codex CLI.

The configuration bootstrap supports two host backends today:

| Provider | Default orchestrator | Worker strategy |
| --- | --- | --- |
| OpenAI Codex | GPT-6 Astra / low | GPT-6 Luna / max |
| Gemini CLI | Gemini 3.8 Flash / HIGH | Gemini 3.8 Flash with role-specific LOW/MEDIUM/HIGH thinking |

> CortexRelay is an independent community project and is not affiliated with or endorsed by OpenAI or Google.

## The idea

The goal is not simply “use a cheaper model.” CortexRelay separates **high-leverage decisions** from **high-volume execution**:

```text
User request
    |
    v
Primary orchestrator
  plan / decompose / arbitrate / synthesize
    |
    +--> explorer      -> scoped repository discovery
    +--> architect     -> design and trade-off analysis
    +--> implementer   -> bounded code changes
    +--> tester        -> focused validation
    +--> reviewer      -> independent correctness review
    |
    v
Final synthesis by the primary model
```

A bad worker decision normally affects one task. A bad orchestration decision can multiply across every worker. CortexRelay therefore spends reasoning budget where the error multiplier is largest.

## Benefits

- **Better cost/quality allocation** instead of one model and one thinking level for every task.
- **Preserved planning quality** because the primary model owns decomposition, arbitration, and final synthesis.
- **Lower context pressure** because workers return compact evidence instead of forwarding raw repository volume.
- **Parallelism** for independent exploration, review, documentation, and testing work.
- **Repeatability** through version-controlled configuration.
- **Provider portability**: Codex and Gemini CLI are adapters over the same orchestration idea.
- **Safe adoption**: existing settings and instruction files are preserved and backed up before CortexRelay-managed changes.

## Install

Recommended with `uv`:

```bash
uv tool install --refresh git+https://github.com/esaesa/cortex-relay.git
```

Alternatives:

```bash
pipx install git+https://github.com/esaesa/cortex-relay.git
```

```bash
python -m pip install git+https://github.com/esaesa/cortex-relay.git
```

Verify:

```bash
cortex-relay --version
```

## Easy setup and daily use

For most users, CortexRelay now has a three-command workflow:

```bash
cortex-relay setup
cortex-relay config
cortex-relay launch
```

### 1. First time: `cortex-relay setup`

The setup wizard detects installed runtime providers, discovers OpenCode models/variants and Antigravity models when available, asks which provider/model should be the orchestrator and default worker, then lets you assign profiles to each role:

```text
orchestrator
explorer
architect
implementer
tester
reviewer
```

It writes a validated project config to:

```text
.cortex-relay/config.toml
```

Use `--scope user` for a user-wide config instead:

```bash
cortex-relay setup --scope user
```

If a config already exists, CortexRelay keeps a `config.toml.bak` backup before writing the new canonical TOML.

### 2. Change who is who: `cortex-relay config`

Use the interactive editor instead of hand-editing TOML:

```bash
cortex-relay config
```

The editor can reassign roles, edit a profile's provider/model/reasoning/access, add profiles, and switch the active preset. If no config exists yet, it automatically starts the setup wizard.

### 3. Work normally: `cortex-relay launch`

```bash
cortex-relay launch
```

`launch` defaults to the configured `orchestrator` role. For an OpenCode orchestrator profile, CortexRelay starts the interactive OpenCode host and injects the local CortexRelay MCP connection so the orchestrator can delegate bounded roles to the configured worker profiles.

The advanced commands remain available when you need explicit control:

```bash
cortex-relay profiles
cortex-relay models --provider opencode --refresh
cortex-relay models --provider antigravity
cortex-relay delegate --profile <name> "task"
cortex-relay launch --preset <name>
```

## Live delegation visibility

When you launch an interactive orchestrator, CortexRelay now creates a tracked host session and prints the live dashboard command:

```text
CortexRelay session: session-...
Live dashboard: open another terminal and run 'cortex-relay status --watch'
```

Typical two-terminal workflow:

```text
Terminal 1
──────────
cortex-relay launch

OpenCode / Muse
> Implement feature X


Terminal 2
──────────
cortex-relay status --watch

CortexRelay live status
=======================
Host: muse → opencode/muse/xhigh [RUNNING]
active 1 | success 2 | failed 0 | tokens 8.4k | cost $0.0063

● muse → implementer → worker → opencode/gpt-6-luna/max
  RUNNING     18s  Implement feature X
  worktree .../implementer-...
```

The dashboard records the routing path, role, profile, provider, model, reasoning variant, lifecycle status, elapsed time, worktree, tests, changed files, normalized token usage, provider-reported cost when available, result summary, and errors.

Useful commands:

```bash
cortex-relay status
cortex-relay status --watch
cortex-relay status --active-only
cortex-relay status --json
cortex-relay history
cortex-relay history --json
```

Completed history can be cleared without affecting active tasks:

```bash
cortex-relay history --clear
```

Runtime state is intentionally stored outside the Git checkout. On Windows the default is under `%LOCALAPPDATA%\CortexRelay\state`; on Unix-like systems CortexRelay uses `$XDG_STATE_HOME/cortex-relay` or `~/.local/state/cortex-relay`. Set `CORTEX_RELAY_STATE_DIR` to override it.

State writes are best-effort only: an unavailable or unwritable observability directory never causes a delegated coding task to fail.

## Runtime delegation

Version 0.8 supports OpenCode, Antigravity CLI, and OpenAI Codex CLI as provider-neutral runtime workers, exposed through direct CLI delegation, MCP, and an A2A server for remote agents such as Gemini CLI.

```text
Primary coding agent
        |
        | bounded task
        v
   CortexRelay
        |
        +--> routing policy
        +--> workspace isolation
        +--> provider adapter
                  |
                  v
        +---------+---------+---------+
        |                   |         |
    OpenCode         Antigravity   Codex CLI
        |                   |         |
        +---------+---------+---------+
                  v
          normalized result
```

The calling agent remains the orchestrator. CortexRelay handles deterministic routing, execution, isolation, normalization, and protocol exposure rather than adding another planning model.

Inspect runtime providers:

```bash
cortex-relay providers
cortex-relay doctor --runtime-only
```

Delegate a read-only review:

```bash
cortex-relay delegate \\
  --provider antigravity \\
  --role reviewer \\
  --reasoning high \\
  "Review the authentication implementation for regressions"
```

Write-capable tasks use an isolated git worktree by default:

```bash
cortex-relay delegate \\
  --access workspace_write \\
  --role implementer \\
  "Implement the bounded change"
```

Install the optional MCP transport and expose CortexRelay to an MCP-capable coding agent:

```bash
python -m pip install -e ".[mcp]"
cortex-relay serve --transport mcp
```

The MCP surface includes `providers`, `profiles`, `status`, `history`, `delegate`, `delegate_parallel`, `delegate_async`, `task_status`, `task_events`, `task_wait`, `task_cancel`, and `tasks`. OpenCode, Codex, and Antigravity are available through the same tools when their CLIs are installed. Async tasks report observable tool activity to the status watcher while they run; use `status --watch -v` for recent actions, `-vv` for bounded output previews, `task_events` for cursor-based updates, and `task_wait` for the full result.

See [Runtime delegation](docs/runtime.md) and [Architecture](docs/architecture.md) for the shared MCP/A2A runtime design.

## Dynamic execution profiles

Version 0.6 separates **roles** from **execution resources**. A named profile defines one provider/model/reasoning/billing path; roles and presets only point to profile names. This means the same model can be represented separately through OpenCode Zen and Codex without CortexRelay treating them as the same resource.

CortexRelay merges:

```text
~/.cortex-relay/config.toml
        ↓
.cortex-relay/config.toml
        ↓
project values override user defaults
```

Example project configuration:

```toml
active_preset = "zen-default"

[profiles.muse-orchestrator]
provider = "opencode"
model = "opencode/muse-spark-1.3-contributor-free"
reasoning = "xhigh"
access = "read_only"
billing_class = "zen-free"

[profiles.zen-luna]
provider = "opencode"
model = "opencode/gpt-6-luna"
reasoning = "max"
access = "workspace_write"
billing_class = "zen-cheap"
fallbacks = ["codex-luna"]

[profiles.zen-luna.options]
validate_variant = true

[profiles.codex-luna]
provider = "codex"
model = "gpt-6-luna"
reasoning = "max"
access = "workspace_write"
billing_class = "chatgpt"

[roles]
orchestrator = "muse-orchestrator"
explorer = "zen-luna"
architect = "muse-orchestrator"
implementer = "zen-luna"
tester = "zen-luna"
reviewer = "zen-luna"

[presets.zen-default]
orchestrator = "muse-orchestrator"

[presets.premium-review.roles]
reviewer = "codex-luna"
```

The model IDs above are examples. Discover the IDs actually exposed by the installed OpenCode/provider configuration:

```bash
cortex-relay models --provider opencode --refresh
cortex-relay models --provider opencode --refresh --verbose
```

Inspect the merged routing policy:

```bash
cortex-relay profiles
cortex-relay profiles --preset premium-review
```

Launch the configured OpenCode orchestrator as the interactive host:

```bash
python -m pip install -e ".[mcp]"
cortex-relay launch --role orchestrator
```

For the example above, that starts the Muse profile as the OpenCode host, selects its configured reasoning variant, and injects a local CortexRelay MCP connection. The host can then delegate `implementer`, `tester`, `reviewer`, or other roles back through CortexRelay. OpenCode worker profiles run as fresh restricted processes, so the selected host model and worker model remain separate.

Override the host topology without editing the base mapping:

```bash
cortex-relay launch --role orchestrator --preset premium-review
cortex-relay launch --profile muse-orchestrator
```

The built-in interactive launcher currently targets OpenCode profiles. Codex, Gemini, or other host agents can continue to use CortexRelay through MCP/A2A while still benefiting from the same role/profile routing.

Use the configured role mapping:

```bash
cortex-relay delegate --role implementer --access workspace_write "Implement the bounded change"
```

Or override one task without changing configuration:

```bash
cortex-relay delegate --profile codex-luna "Review this change"
```

Resolution is deterministic: explicit `--profile`, then the selected preset/role mapping, then the legacy provider routing behavior. Explicit `--provider` and `--model` continue to work for backward compatibility.

Profile fallbacks default to `unavailable` only. A user may opt into fallback on `timeout` or `error`, but doing so for write-capable profiles can leave separate isolated worktrees from failed attempts that need inspection.

### OpenCode worker safety

CortexRelay launches delegated OpenCode workers non-interactively with a CortexRelay-owned permission overlay. It does not enable broad auto-approval. Read-only profiles deny edits; write profiles can edit only within the active workspace/worktree policy; external directories, native subagent delegation, skills, and web tools are denied by the injected worker policy. Shell commands default to approval-required, with a narrow allowlist for repository inspection and common test/build commands.

OpenCode model variants are validated when the installed OpenCode model metadata explicitly advertises them. If metadata is unavailable, CortexRelay passes the requested variant through rather than imposing a stale allowlist.

## A2A: let Gemini CLI delegate through CortexRelay

Install the optional A2A runtime:

```bash
python -m pip install -e ".[a2a]"
```

Start a local Codex-backed remote agent:

```bash
cortex-relay serve \
  --transport a2a \
  --a2a-profile zen-luna \
  --a2a-role reviewer \
  --a2a-workspace .
```

CortexRelay prints the Agent Card URL and a ready-to-copy Gemini remote-agent definition. Save that definition as, for example:

```text
.gemini/agents/cortex-codex.md
```

The local server exposes:

```text
/.well-known/agent-card.json
/a2a/jsonrpc
/a2a/rest
/healthz
```

The server targets A2A v1 and enables v0.3 compatibility on its JSON-RPC and REST endpoints, which matches current Gemini CLI remote-agent behavior.

A2A policy is fixed when the server starts. It may select an explicit profile (`--a2a-profile`) or a preset plus role (`--a2a-preset`). Incoming agents send only the task text; they cannot choose another workspace, profile, provider, model, or write permission through the prompt. Read-only is the default. Workspace-write tasks can use isolated git worktrees.

For safety, CortexRelay refuses non-loopback A2A binding unless `--a2a-allow-remote` is explicitly supplied. That flag exposes an unauthenticated service, so local binding is the recommended default.

## Quick start: Codex

From the project root:

```bash
cortex-relay init --provider codex
cortex-relay doctor --provider codex
```

The default Codex preset writes:

```text
.codex/config.toml
.codex/agents/explorer.toml
.codex/agents/architect.toml
.codex/agents/implementer.toml
.codex/agents/tester.toml
.codex/agents/reviewer.toml
AGENTS.md
```

Default routing:

```text
GPT-6 Astra / low
        |
        v
GPT-6 Luna / max workers
```

Customize it:

```bash
cortex-relay init --provider codex \
  --orchestrator-model gpt-6-astra \
  --orchestrator-effort low \
  --worker-model gpt-6-luna \
  --worker-effort max \
  --threads 6
```

See [Manual Codex configuration](docs/manual-configuration.md).

## Quick start: Gemini CLI

From the project root:

```bash
cortex-relay init --provider gemini
cortex-relay doctor --provider gemini
```

The Gemini backend writes:

```text
.gemini/settings.json
.gemini/agents/explorer.md
.gemini/agents/architect.md
.gemini/agents/implementer.md
.gemini/agents/tester.md
.gemini/agents/reviewer.md
GEMINI.md
```

### Default Gemini 3.8 routing

CortexRelay uses **Gemini 3.8 Flash HIGH for the orchestrator**. It does not reduce planning quality by putting the primary agent on MEDIUM while workers consume HIGH reasoning during large implementations.

| Role | Model | Thinking |
| --- | --- | --- |
| Primary orchestrator | Gemini 3.8 Flash | **HIGH** |
| Explorer | Gemini 3.8 Flash | MEDIUM |
| Architect | Gemini 3.8 Flash | **HIGH** |
| Implementer | Gemini 3.8 Flash | **HIGH** |
| Tester | Gemini 3.8 Flash | LOW |
| Reviewer | Gemini 3.8 Flash | **HIGH** |

This routes the **reasoning budget**, not only the model name. Mechanical validation should not automatically consume the same thinking budget as architecture, difficult implementation, or final arbitration.

Customize the Gemini policy:

```bash
cortex-relay init --provider gemini \
  --orchestrator-model gemini-3.8-flash \
  --orchestrator-thinking high \
  --worker-model gemini-3.8-flash \
  --explorer-thinking medium \
  --architect-thinking high \
  --implementer-thinking high \
  --tester-thinking low \
  --reviewer-thinking high
```

See [Manual Gemini configuration](docs/gemini-configuration.md).

## How the Gemini backend works

CortexRelay configures the main session through a named Gemini CLI model alias and sets:

```json
{
  "model": {
    "name": "cortex-relay-orchestrator"
  }
}
```

The alias targets `gemini-3.8-flash` and injects `thinkingLevel: "HIGH"`. Each custom subagent then gets its own override under `agents.overrides` with a role-specific thinking level.

Custom Gemini subagents are standard `.gemini/agents/*.md` files with YAML frontmatter, so the generated configuration remains inspectable and editable without CortexRelay.

## Dry run and user scope

Preview paths/settings without writing:

```bash
cortex-relay init --provider gemini --dry-run
```

Install user-wide defaults instead of project configuration:

```bash
cortex-relay init --provider gemini --scope user
```

Project scope is recommended for team repositories because the policy can be reviewed and versioned with the codebase.

## Context compression is part of the design

The expensive mistake is not only picking the wrong model. It is also letting the primary model repeatedly consume huge worker transcripts.

```text
large repository
      |
      v
specialized worker
      |
      | compact evidence
      v
primary orchestrator
      |
      v
    decision
```

Worker reports should contain the smallest evidence needed for the primary agent to make a reliable decision.

## Safety and limitations

- CortexRelay can generate Codex/Gemini configuration and invoke registered OpenCode, Codex, or Antigravity provider CLIs for delegated tasks. It does not proxy or intercept provider API traffic.
- Model availability depends on your account, workspace policy, authentication method, product surface, and rollout status.
- Current Codex compatibility hints include `gpt-6-astra`, `gpt-6-sol`, and `gpt-6-luna`, but runtime model IDs are passed through rather than restricted to a fixed allowlist. Model availability still depends on the installed Codex version and account.
- Lower reasoning is appropriate for bounded/mechanical tasks, not automatically for every worker.
- Do not grant a delegated provider broader tool access than its task requires. CortexRelay does not pass Antigravity's global auto-approval flag or Codex's dangerous sandbox/approval bypass flag.
- Gemini project settings are ignored in untrusted workspaces; trust the workspace before expecting `.gemini/settings.json` or project remote-agent files to load.
- The A2A server is unauthenticated in 0.8; keep it on loopback unless you explicitly accept remote network exposure.

## Official references

### Codex

- Codex subagents: https://learn.chatgpt.com/docs/agent-configuration/subagents
- Codex `AGENTS.md`: https://learn.chatgpt.com/docs/agent-configuration/agents-md
- Codex config reference: https://learn.chatgpt.com/docs/config-file/config-reference

### OpenCode

- OpenCode CLI: https://dev.opencode.ai/docs/cli/
- OpenCode configuration: https://dev.opencode.ai/docs/config/
- OpenCode permissions: https://dev.opencode.ai/docs/permissions/

### Gemini CLI

- Gemini CLI configuration: https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md
- Gemini CLI subagents: https://github.com/google-gemini/gemini-cli/blob/main/docs/core/subagents.md
- Gemini CLI model configuration: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/generation-settings.md
- Gemini CLI `GEMINI.md`: https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/gemini-md.md

## Development

```bash
git clone https://github.com/esaesa/cortex-relay.git
cd cortex-relay
python -m unittest discover -s tests -v
```

Install locally:

```bash
python -m pip install -e .
cortex-relay --version
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
