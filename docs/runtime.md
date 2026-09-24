# Runtime delegation

CortexRelay 0.4 supports Antigravity CLI and OpenAI Codex CLI through the same provider-neutral runtime beside the existing Codex and Gemini configuration writers.

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
        +-----+-----+
        |           |
        v           v
 Antigravity CLI  Codex CLI
        |           |
        +-----+-----+
              |
              v
       normalized TaskResult
```

The runtime currently ships adapters for Antigravity CLI and OpenAI Codex CLI. Additional providers should implement the same `ProviderAdapter` contract rather than leaking provider-specific flags into the core.

## Install

The base package has no runtime Python dependencies:

```bash
python -m pip install -e .
```

MCP support is optional:

```bash
python -m pip install -e ".[mcp]"
```

Antigravity CLI is discovered through the `agy` executable and Codex CLI through `codex` on `PATH`. Authenticate each CLI using its normal upstream flow before delegation.

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
- `delegate`
- `delegate_parallel`

`delegate_parallel` is intended for independent tasks. Each task may choose `codex` or `antigravity`; write-capable tasks are isolated into separate git worktrees by default.

## A2A direction

Gemini CLI can consume remote subagents through A2A. CortexRelay currently includes a helper for rendering Gemini remote-agent configuration, but 0.3 does not claim to ship a production A2A HTTP server yet.

The intended next step is to expose the same provider-neutral `TaskSpec -> TaskResult` runtime through A2A without adding a second planning layer.

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
