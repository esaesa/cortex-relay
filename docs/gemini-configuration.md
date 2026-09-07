# Manual Gemini CLI configuration

CortexRelay's Gemini backend uses only standard Gemini CLI files. You can reproduce the setup manually without installing the CortexRelay CLI.

## 1. Project settings

Create `.gemini/settings.json` in the repository root.

The orchestrator is configured through a named model alias so its HIGH thinking policy does not accidentally force every worker using the same concrete model to HIGH.

```json
{
  "model": {
    "name": "cortex-relay-orchestrator"
  },
  "modelConfigs": {
    "customAliases": {
      "cortex-relay-orchestrator": {
        "extends": "chat-base",
        "modelConfig": {
          "model": "gemini-3.8-flash",
          "generateContentConfig": {
            "thinkingConfig": {
              "thinkingLevel": "HIGH"
            }
          }
        }
      }
    }
  },
  "agents": {
    "overrides": {
      "explorer": {
        "enabled": true,
        "modelConfig": {
          "model": "gemini-3.8-flash",
          "generateContentConfig": {
            "thinkingConfig": {
              "thinkingLevel": "MEDIUM"
            }
          }
        }
      },
      "architect": {
        "enabled": true,
        "modelConfig": {
          "model": "gemini-3.8-flash",
          "generateContentConfig": {
            "thinkingConfig": {
              "thinkingLevel": "HIGH"
            }
          }
        }
      },
      "implementer": {
        "enabled": true,
        "modelConfig": {
          "model": "gemini-3.8-flash",
          "generateContentConfig": {
            "thinkingConfig": {
              "thinkingLevel": "HIGH"
            }
          }
        }
      },
      "tester": {
        "enabled": true,
        "modelConfig": {
          "model": "gemini-3.8-flash",
          "generateContentConfig": {
            "thinkingConfig": {
              "thinkingLevel": "LOW"
            }
          }
        }
      },
      "reviewer": {
        "enabled": true,
        "modelConfig": {
          "model": "gemini-3.8-flash",
          "generateContentConfig": {
            "thinkingConfig": {
              "thinkingLevel": "HIGH"
            }
          }
        }
      }
    }
  }
}
```

If the file already exists, merge these keys instead of replacing unrelated Gemini CLI settings.

## 2. Custom subagents

Gemini CLI discovers project custom agents from `.gemini/agents/*.md`.

Each file uses YAML frontmatter followed by the system prompt. Example `.gemini/agents/explorer.md`:

```markdown
---
name: explorer
description: "Map relevant files, symbols, dependencies, and execution paths before implementation."
kind: local
model: "gemini-3.8-flash"
max_turns: 30
---

You are the repository exploration specialist.

Your job is discovery, not implementation.

1. Locate the smallest relevant set of files and symbols.
2. Trace the real execution path and important dependencies.
3. Prefer targeted searches and reads over broad scans.
4. Identify existing abstractions before suggesting new ones.
5. Do not modify files.
6. Return compact evidence to the parent agent.
```

Create equivalent files for `architect`, `implementer`, `tester`, and `reviewer`.

## 3. Orchestrator instructions

Create or update `GEMINI.md` at the repository root with an orchestration policy:

```markdown
## CortexRelay orchestration

- Keep the primary model focused on planning, decomposition, arbitration, and final synthesis.
- Delegate bounded work to specialized subagents.
- Parallelize independent tasks.
- Give each worker exact scope, constraints, expected evidence, and acceptance criteria.
- Keep worker reports compact.
- Use explorer for discovery, architect for design, implementer for changes, tester for validation, and reviewer for independent review.
- Resolve contradictory findings before final synthesis.
```

Gemini CLI automatically loads project `GEMINI.md` context files.

## 4. Verify

Start Gemini CLI from the project root, then inspect loaded agents and context:

```text
/agents
/memory show
```

You can explicitly invoke a custom subagent with `@`, for example:

```text
@explorer Trace the authorization path for organization-scoped user listing.
```

## Why the orchestrator is HIGH

CortexRelay intentionally does **not** use MEDIUM for the primary orchestrator merely to save thinking tokens. Planning errors multiply across every delegated task. The orchestrator therefore receives HIGH reasoning, while lower thinking is reserved for bounded work where extra reasoning has lower marginal value.

The default policy is:

| Role | Thinking |
| --- | --- |
| Orchestrator | HIGH |
| Explorer | MEDIUM |
| Architect | HIGH |
| Implementer | HIGH |
| Tester | LOW |
| Reviewer | HIGH |

Adjust these levels based on your workload rather than assuming HIGH everywhere is optimal.
