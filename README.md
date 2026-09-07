# CortexRelay

**Cost-aware multi-agent orchestration and reasoning-budget routing for Codex and Gemini CLI.**

CortexRelay is an open-source configuration toolkit for developers who want to spend the strongest reasoning where mistakes have the highest downstream cost, while routing high-volume bounded work to efficient subagents.

It supports two provider backends today:

| Provider | Default orchestrator | Worker strategy |
| --- | --- | --- |
| OpenAI Codex | GPT-6 Astra / low | GPT-5.6 Luna / xhigh |
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
GPT-5.6 Luna / xhigh workers
```

Customize it:

```bash
cortex-relay init --provider codex \
  --orchestrator-model gpt-6-astra \
  --orchestrator-effort low \
  --worker-model gpt-5.6-luna \
  --worker-effort xhigh \
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

- CortexRelay changes local Codex or Gemini CLI configuration; it does not proxy API calls or intercept model traffic.
- Model availability depends on your account, workspace policy, authentication method, product surface, and rollout status.
- Model names, pricing, thinking levels, and subagent schemas can change. Review upstream documentation before organization-wide rollout.
- Lower reasoning is appropriate for bounded/mechanical tasks, not automatically for every worker.
- Do not grant a subagent broader tool access than its task requires.
- Gemini project settings are ignored in untrusted workspaces; trust the workspace before expecting `.gemini/settings.json` to load.

## Official references

### Codex

- Codex subagents: https://learn.chatgpt.com/docs/agent-configuration/subagents
- Codex `AGENTS.md`: https://learn.chatgpt.com/docs/agent-configuration/agents-md
- Codex config reference: https://learn.chatgpt.com/docs/config-file/config-reference

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
