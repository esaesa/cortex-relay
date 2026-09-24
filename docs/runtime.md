# Runtime delegation

CortexRelay 0.8 supports OpenCode, Antigravity CLI, and OpenAI Codex CLI through the same provider-neutral runtime, with direct CLI, MCP, and A2A frontends.

The primary coding agent remains the orchestrator. CortexRelay does not attempt to replace its planning loop. It receives an already-bounded task, applies deterministic routing policy, executes the selected provider, and returns a compact normalized result.

## Runtime flow

```text
Codex / Gemini / another coding agent
              |
              | bounded TaskSpec
              v
        CortexRelay runtime
              |
      +-------+--------+
      |                |
 routing policy   workspace policy
      |                |
      +-------+--------+
              |
        provider adapter
              |
        +--------+--------+--------+
        |        |        |
        v        v        v
    OpenCode  Antigravity  Codex
        |        |        |
        +--------+--------+
              |
              v
       normalized TaskResult
```

The runtime currently ships adapters for OpenCode, Antigravity CLI, and OpenAI Codex CLI. Additional providers should implement the same `ProviderAdapter` contract rather than leaking provider-specific flags into the core.

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
- worker task ID, role, profile, provider, model, reasoning and billing class;
- fallback attempts;
- worktree path/branch;
- elapsed/provider-reported duration;
- normalized input/output/total token counts when available;
- provider-reported cost when available;
- tests, changed files, risks, result summary and errors;
- provider conversation/session identifiers.

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

## Normalized contract

`TaskSpec` carries provider-neutral inputs:

- objective
- role
- provider
- workspace
- access mode
- reasoning level
- optional model
- acceptance criteria
- timeout
- write-isolation preference

Every adapter returns a `TaskResult` with the same shape:

- status
- provider/model
- summary
- compact evidence
- changed files
- commands
- tests
- risks
- conversation identifier
- usage metadata
- runtime metadata

This lets the calling coding agent reason over results without learning each provider's CLI envelope.

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

Delegated OpenCode workers are deliberately different from the host: they run as fresh `--pure` processes with the worker permission overlay, and the inherited `cortex-relay` MCP server is disabled inside those child runs to prevent recursive delegation loops.

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

The OpenCode adapter uses non-interactive `opencode --pure run --format json` execution. It accepts provider/model IDs through `--model`, maps the profile reasoning field to OpenCode `--variant`, parses JSON events, captures the session identifier and usage metadata, and normalizes the final JSON text into `TaskResult`.

Discover the model IDs exposed by the installed OpenCode/provider configuration:

```bash
cortex-relay models --provider opencode --refresh
cortex-relay models --provider opencode --refresh --verbose
cortex-relay models --provider opencode --refresh --json
cortex-relay models --provider antigravity
cortex-relay models --provider antigravity --json
```

OpenCode variant validation is best-effort. When verbose model metadata explicitly lists variants, CortexRelay rejects a requested variant that is absent. When metadata is unavailable or does not advertise variants, the value is passed through.

CortexRelay injects a worker-only `OPENCODE_CONFIG_CONTENT` permission overlay rather than modifying the user's persistent OpenCode settings. Broad automatic approval is not enabled. Read-only workers deny edits; external directories, native task/subagent delegation, skills, and web tools are denied; shell execution defaults to approval-required with a narrow repository/test/build allowlist. `--pure` is also used to exclude external plugins from delegated worker runs.

Read-only OpenCode tasks receive the same before/after git-status contract check as the other provider adapters.

## Codex adapter

The Codex adapter uses non-interactive `codex exec` with:

- `--json` JSONL lifecycle events;
- `--output-schema` for the normalized result schema;
- `--output-last-message` for reliable final structured output;
- `--model` when the task pins a model;
- `model_reasoning_effort` for the requested effort;
- `--sandbox read-only` or `--sandbox workspace-write` according to the task access contract.

CortexRelay does **not** pass Codex's dangerous approval/sandbox bypass option.

Current compatibility hints are:

| Model | Known reasoning efforts |
| --- | --- |
| `gpt-6-astra` | low, medium, high, xhigh, max |
| `gpt-6-sol` | none, low, medium, high, xhigh, max |
| `gpt-6-luna` | none, low, medium, high, xhigh, max |

These are hints, not an allowlist. Unknown future model IDs and reasoning names are passed through to Codex CLI so a newer installed Codex can use them without waiting for a CortexRelay release. CortexRelay only rejects model/effort combinations it knows are incompatible.

Read-only Codex tasks also compare git status before and after execution. Write-capable tasks inherit the same provider-neutral worktree isolation used by other adapters.

## Antigravity adapter

CortexRelay discovers the live Antigravity model catalog through `agy models`. The command currently returns a human-readable list rather than JSON, so CortexRelay parses the model slug and display label conservatively and falls back to manual model entry only if discovery returns no usable models.

The adapter uses headless `agy -p` execution with:

- JSON output;
- an enforced JSON Schema;
- explicit reasoning effort;
- optional model selection;
- a print timeout;
- terminal sandbox restrictions.

Read-only tasks additionally compare git status before and after the run. Any workspace change is reported as a contract violation.

CortexRelay intentionally does not pass Antigravity's global auto-approval flag. Provider permissions should remain scoped by the user's Antigravity configuration.

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

The MCP surface is deliberately small:

- `providers`
- `profiles`
- `status`
- `history`
- `delegate`
- `delegate_parallel`

`delegate_parallel` is intended for independent tasks. Each task may choose a profile or an explicit `opencode`, `codex`, or `antigravity` provider; write-capable tasks are isolated into separate git worktrees by default.

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

New providers should implement:

```python
class ProviderAdapter(ABC):
    name: str

    def capabilities(self) -> ProviderCapabilities:
        ...

    def execute(self, task: TaskSpec) -> TaskResult:
        ...
```

Provider-specific command flags, response envelopes, authentication behavior, and error translation belong inside the adapter. Routing, task semantics, and result semantics belong in the core.
