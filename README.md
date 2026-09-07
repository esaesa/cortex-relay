# CortexRelay

**Cost-aware multi-agent orchestration and model routing for OpenAI Codex.**

CortexRelay is an open-source configuration toolkit for developers who want to reserve a stronger reasoning model for orchestration, arbitration, and final synthesis while delegating bounded, high-volume work to cheaper subagents.

The default preset is intentionally simple:

```text
Primary orchestrator: GPT-6 Astra / low reasoning
Worker subagents:     GPT-5.6 Luna / xhigh reasoning
```

The idea is not "use a cheaper model for everything." The idea is to make the expensive model do the work it is uniquely valuable for: understand ambiguous goals, decompose the task, resolve conflicts, and synthesize the final result. Repository exploration, implementation, testing, review, and other bounded work are delegated to specialized workers.

> CortexRelay is an independent community project and is not affiliated with or endorsed by OpenAI.

## Why CortexRelay?

Large coding tasks often spend most of their token budget on reading files, tracing code paths, implementing mechanical changes, running validation, and summarizing evidence. Those steps do not always require the same model used for top-level judgment.

CortexRelay introduces a deliberate boundary:

```text
User request
    |
    v
Strong primary model
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

### Benefits

- **Lower model spend** by moving bulk work to a cheaper worker model.
- **Preserved reasoning quality** because the primary model still owns ambiguity, planning, arbitration, and final synthesis.
- **Smaller context pressure** because workers return compact evidence instead of forwarding entire repositories to the primary model.
- **Parallelism** for independent exploration, review, documentation, and testing tasks.
- **Repeatability** through versioned Codex configuration instead of hand-tuned prompts in every repository.
- **Model portability** because the orchestrator and worker model IDs are CLI options, not hard-coded architectural assumptions.
- **Safe adoption** because CortexRelay backs up existing configuration and updates managed sections instead of replacing the whole project setup.

## Quick start

### 1. Install the CLI

Recommended with `pipx`:

```bash
pipx install git+https://github.com/esaesa/cortex-relay.git
```

Or with `uv`:

```bash
uv tool install git+https://github.com/esaesa/cortex-relay.git
```

Or with pip:

```bash
python -m pip install git+https://github.com/esaesa/cortex-relay.git
```

### 2. Configure a project

Run this from the root of the project you want Codex to work on:

```bash
cortex-relay init
```

That command creates or updates:

```text
.codex/config.toml
.codex/agents/explorer.toml
.codex/agents/architect.toml
.codex/agents/implementer.toml
.codex/agents/tester.toml
.codex/agents/reviewer.toml
AGENTS.md
```

Existing files are backed up before CortexRelay changes them.

### 3. Verify the installation

```bash
cortex-relay doctor
```

Then ask Codex to show the instructions it loaded:

```bash
codex --ask-for-approval never "Summarize the current instructions."
```

## Configure the routing policy from the command line

The default is the Astra + Luna preset:

```bash
cortex-relay init \
  --orchestrator-model gpt-6-astra \
  --orchestrator-effort low \
  --worker-model gpt-5.6-luna \
  --worker-effort xhigh \
  --threads 6
```

Use a different model pair without changing CortexRelay itself:

```bash
cortex-relay init \
  --orchestrator-model YOUR_PRIMARY_MODEL \
  --orchestrator-effort medium \
  --worker-model YOUR_WORKER_MODEL \
  --worker-effort high
```

Preview paths and settings without writing files:

```bash
cortex-relay init --dry-run
```

Install global defaults for every Codex project instead of only the current repository:

```bash
cortex-relay init --scope user
```

Project-level configuration is recommended for teams because it can be reviewed and versioned with the repository.

## What the default preset writes

The primary model is configured in `.codex/config.toml`:

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

Each custom agent also pins the worker model so the role remains explicit and auditable.

CortexRelay adds a managed block to `AGENTS.md` that tells the primary agent to behave as an orchestrator rather than absorbing all bulk work itself.

## Why `xhigh` instead of `max` in the CLI preset?

OpenAI's current subagent guidance describes both **Max** and **xhigh** as high reasoning levels when supported, but the current Codex `config.toml` reference lists `minimal`, `low`, `medium`, `high`, and `xhigh` for `model_reasoning_effort`.

CortexRelay therefore emits `xhigh` for portable Codex configuration today. In ChatGPT Work surfaces that expose a **Max** UI option, you can select Max there when supported. This distinction is documented rather than silently writing a config value that the Codex config reference does not currently list.

## Manual configuration

You do not need the CortexRelay CLI. Everything it configures is standard Codex configuration.

See **[Manual configuration](docs/manual-configuration.md)** for a complete step-by-step setup, including how to preserve an existing `config.toml` and `AGENTS.md`.

## Architecture

See **[Architecture](docs/architecture.md)** for the reasoning behind the orchestration boundary, worker roles, context compression, and escalation strategy.

## Example prompt

After installing CortexRelay, a useful task prompt is:

```text
Implement this feature end to end. Use subagents for repository exploration,
architecture review, implementation, tests, and independent review where useful.
Keep the primary agent focused on decomposition, conflict resolution, and final synthesis.
```

At lower primary reasoning levels, explicit delegation is useful because Codex documentation recommends asking for subagents explicitly when you want parallel delegation.

## Safety and limitations

- CortexRelay changes local Codex configuration; it does not proxy API calls or intercept model traffic.
- Model availability depends on your ChatGPT/Codex account, workspace policy, and product surface.
- Model names, reasoning levels, pricing, and subagent behavior can change. Review the official Codex documentation before adopting a preset organization-wide.
- Lower cost does not guarantee equal quality. Measure your own workload and adjust worker effort or model selection when tasks become ambiguous.
- Do not delegate sensitive operations to a broader sandbox than the task requires.

## Official references

- Codex subagents: https://learn.chatgpt.com/docs/agent-configuration/subagents
- Codex `AGENTS.md`: https://learn.chatgpt.com/docs/agent-configuration/agents-md
- Codex config reference: https://learn.chatgpt.com/docs/config-file/config-reference
- ChatGPT/Codex models: https://learn.chatgpt.com/docs/models

## Development

Clone the repository and run the tests with the standard library:

```bash
git clone https://github.com/esaesa/cortex-relay.git
cd cortex-relay
python -m unittest discover -s tests -v
```

Install the local CLI in editable mode:

```bash
python -m pip install -e .
cortex-relay --version
```

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
