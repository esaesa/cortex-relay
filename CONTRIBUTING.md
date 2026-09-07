# Contributing to CortexRelay

Thanks for helping improve CortexRelay.

## Principles

Changes should preserve these properties:

- model routing remains configurable;
- existing Codex configuration is preserved when possible;
- installation is repeatable and idempotent;
- read-only worker roles do not receive write access without a clear reason;
- documentation distinguishes verified Codex syntax from product UI labels;
- claims about model cost or availability should link to current official documentation and avoid becoming hard-coded assumptions.

## Development setup

```bash
git clone https://github.com/esaesa/cortex-relay.git
cd cortex-relay
python -m venv .venv
```

Activate the environment, then:

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Pull requests

A pull request should explain:

1. the problem being solved;
2. the configuration or behavior changed;
3. compatibility implications;
4. tests performed;
5. documentation updates where relevant.
