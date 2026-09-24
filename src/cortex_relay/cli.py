from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .configurator import ConfigValues
from .core.models import TaskSpec
from .core.registry import default_registry
from .diagnostics import configuration_checks, runtime_checks
from .gemini import GEMINI_THINKING_LEVELS, GeminiConfigValues
from .installer import install


CODEX_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
CONFIG_PROVIDERS = ("codex", "gemini")
RUNTIME_ACCESS = ("read_only", "workspace_write")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex-relay",
        description="Configure and run provider-neutral coding-agent delegation.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Install or update CortexRelay configuration.")
    init_parser.add_argument("--provider", choices=CONFIG_PROVIDERS, default="codex")
    init_parser.add_argument("--scope", choices=("project", "user"), default="project")
    init_parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    init_parser.add_argument("--preset", choices=("astra-luna", "gemini-3.8-flash"))

    init_parser.add_argument("--orchestrator-model")
    init_parser.add_argument("--worker-model")

    known_efforts = ", ".join(CODEX_EFFORTS)
    init_parser.add_argument(
        "--orchestrator-effort",
        help=f"Codex effort. Current known values: {known_efforts}. New values are passed through.",
    )
    init_parser.add_argument(
        "--worker-effort",
        help=f"Codex effort. Current known values: {known_efforts}. New values are passed through.",
    )
    init_parser.add_argument("--threads", type=int)

    init_parser.add_argument("--orchestrator-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--explorer-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--architect-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--implementer-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--tester-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--reviewer-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--dry-run", action="store_true")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check generated configuration and runtime provider availability.",
    )
    doctor_parser.add_argument("--provider", choices=CONFIG_PROVIDERS, default="codex")
    doctor_parser.add_argument("--scope", choices=("project", "user"), default="project")
    doctor_parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    doctor_parser.add_argument(
        "--runtime-only",
        action="store_true",
        help="Skip generated configuration checks and require a runtime provider.",
    )

    subparsers.add_parser("providers", help="List runtime providers and capabilities.")

    delegate_parser = subparsers.add_parser(
        "delegate",
        help="Delegate one bounded task through the provider-neutral runtime.",
    )
    delegate_parser.add_argument("objective")
    delegate_parser.add_argument("--role", default="reviewer")
    delegate_parser.add_argument("--provider", default="auto")
    delegate_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    delegate_parser.add_argument("--access", choices=RUNTIME_ACCESS, default="read_only")
    delegate_parser.add_argument(
        "--reasoning",
        default="high",
        help=(
            "Provider reasoning effort. Passed through so newer provider effort names "
            "can work without a CortexRelay release."
        ),
    )
    delegate_parser.add_argument(
        "--model",
        help=(
            "Provider model ID. Passed through unchanged; CortexRelay does not use a "
            "fixed model allowlist."
        ),
    )
    delegate_parser.add_argument("--accept", action="append", default=[], dest="acceptance_criteria")
    delegate_parser.add_argument("--timeout", type=int, default=300, dest="timeout_seconds")
    delegate_parser.add_argument(
        "--isolate-write",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use a temporary git worktree for workspace-write tasks (default: enabled).",
    )
    delegate_parser.add_argument("--json", action="store_true", dest="as_json")

    serve_parser = subparsers.add_parser("serve", help="Expose CortexRelay to another coding agent.")
    serve_parser.add_argument("--transport", choices=("mcp", "a2a"), default="mcp")
    serve_parser.add_argument(
        "--mcp-transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--a2a-provider", default="auto")
    serve_parser.add_argument("--a2a-model")
    serve_parser.add_argument("--a2a-reasoning", default="high")
    serve_parser.add_argument("--a2a-role", default="reviewer")
    serve_parser.add_argument("--a2a-workspace", type=Path, default=Path.cwd())
    serve_parser.add_argument("--a2a-access", choices=RUNTIME_ACCESS, default="read_only")
    serve_parser.add_argument("--a2a-timeout", type=int, default=300)
    serve_parser.add_argument(
        "--a2a-isolate-write",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Isolate A2A workspace-write tasks in git worktrees (default: enabled).",
    )
    serve_parser.add_argument(
        "--a2a-public-url",
        help="Public HTTP(S) base URL advertised in the Agent Card.",
    )
    serve_parser.add_argument(
        "--a2a-name",
        default="cortex-relay",
        help="A2A/Gemini remote-agent name (lowercase slug).",
    )
    serve_parser.add_argument(
        "--a2a-allow-remote",
        action="store_true",
        help="Allow unauthenticated A2A binding on a non-loopback interface.",
    )

    return parser


def _doctor(provider: str, scope: str, project_dir: Path, *, runtime_only: bool) -> int:
    config = []
    if not runtime_only:
        config = configuration_checks(provider=provider, scope=scope, project_dir=project_dir)
    runtime = runtime_checks(default_registry())

    print("CortexRelay diagnostics:")
    for check in [*config, *runtime]:
        marker = "OK" if check.ok else ("MISSING" if check.name.startswith("config:") else "OPTIONAL")
        print(f"  {marker:<8} {check.name}: {check.detail}")

    if runtime_only:
        return 0 if runtime and any(check.ok for check in runtime) else 1
    return 0 if all(check.ok for check in config) else 1


def _providers() -> int:
    capabilities = default_registry().capabilities()
    if not capabilities:
        print("No runtime providers are registered.")
        return 1

    print("CortexRelay runtime providers:")
    for item in capabilities:
        status = "available" if item["available"] else "unavailable"
        print(f"  {item['name']}: {status}")
        print(f"    binary: {item['binary']}")
        print(f"    detail: {item['detail']}")
        known_models = item.get("known_models") or ()
        if known_models:
            print(f"    known models: {', '.join(known_models)}")
        reasoning_levels = item.get("reasoning_levels") or ()
        if reasoning_levels:
            print(f"    known efforts: {', '.join(reasoning_levels)}")
        print(
            "    features: structured_output={structured_output}, model_selection={model_selection}, "
            "reasoning_control={reasoning_control}, workspace_write={workspace_write}".format(**item)
        )
    return 0


def _delegate(args: argparse.Namespace) -> int:
    task = TaskSpec(
        objective=args.objective,
        role=args.role,
        provider=args.provider,
        workspace=args.workspace,
        access=args.access,
        reasoning=args.reasoning,
        model=args.model,
        acceptance_criteria=tuple(args.acceptance_criteria),
        timeout_seconds=args.timeout_seconds,
        isolate_write=args.access == "workspace_write" and args.isolate_write,
    )
    result = default_registry().execute(task)
    if args.as_json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"CortexRelay delegation: {result.status}")
        print(f"  provider: {result.provider}")
        if result.model:
            print(f"  model:    {result.model}")
        print(f"  summary:  {result.summary}")
        if result.error:
            print(f"  error:    {result.error}")
        if result.evidence:
            print("  evidence:")
            for item in result.evidence:
                location = ":".join(part for part in (item.path, item.symbol) if part)
                suffix = f" ({location})" if location else ""
                print(f"    - {item.finding}{suffix}")
        if result.tests:
            print("  tests:")
            for item in result.tests:
                print(f"    - {item}")
        if result.risks:
            print("  risks:")
            for item in result.risks:
                print(f"    - {item}")
        if result.metadata.get("worktree_path"):
            print(f"  worktree: {result.metadata['worktree_path']}")
            print(f"  branch:   {result.metadata['worktree_branch']}")
    return 0 if result.ok else 1


def _serve(args: argparse.Namespace) -> int:
    if args.transport == "mcp":
        from .transports.mcp import run_mcp

        try:
            run_mcp(
                transport=args.mcp_transport,
                host=args.host,
                port=args.port,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    if args.transport == "a2a":
        from .transports.a2a import (
            A2AServerPolicy,
            agent_card_url,
            gemini_remote_agent_markdown,
            resolve_public_url,
            run_a2a,
        )

        try:
            policy = A2AServerPolicy(
                provider=args.a2a_provider,
                model=args.a2a_model,
                reasoning=args.a2a_reasoning,
                role=args.a2a_role,
                workspace=args.a2a_workspace,
                access=args.a2a_access,
                timeout_seconds=args.a2a_timeout,
                isolate_write=args.a2a_isolate_write,
            )
            public_url = resolve_public_url(
                host=args.host,
                port=args.port,
                public_url=args.a2a_public_url,
            )
            card_url = agent_card_url(public_url)
            print(f"CortexRelay A2A Agent Card: {card_url}")
            print("Gemini CLI remote-agent definition:")
            print(
                gemini_remote_agent_markdown(
                    name=args.a2a_name,
                    agent_card_url=card_url,
                )
            )
            run_a2a(
                policy=policy,
                host=args.host,
                port=args.port,
                public_url=public_url,
                name=args.a2a_name,
                allow_remote=args.a2a_allow_remote,
            )
        except (RuntimeError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0

    raise ValueError(f"unsupported transport: {args.transport}")


def _codex_values(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, ConfigValues]:
    if args.preset not in (None, "astra-luna"):
        parser.error("--preset gemini-3.8-flash requires --provider gemini")
    if any(
        value is not None
        for value in (
            args.orchestrator_thinking,
            args.explorer_thinking,
            args.architect_thinking,
            args.implementer_thinking,
            args.tester_thinking,
            args.reviewer_thinking,
        )
    ):
        parser.error("Gemini thinking flags require --provider gemini")

    threads = 6 if args.threads is None else args.threads
    if threads < 1:
        parser.error("--threads must be at least 1")

    values = ConfigValues(
        orchestrator_model=args.orchestrator_model or "gpt-6-astra",
        orchestrator_effort=args.orchestrator_effort or "low",
        worker_model=args.worker_model or "gpt-6-luna",
        worker_effort=args.worker_effort or "max",
        max_threads=threads,
    )
    return "astra-luna", values


def _gemini_values(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, GeminiConfigValues]:
    if args.preset not in (None, "gemini-3.8-flash"):
        parser.error("--preset astra-luna requires --provider codex")
    if args.orchestrator_effort is not None or args.worker_effort is not None:
        parser.error("Codex effort flags are not valid for --provider gemini; use thinking flags")
    if args.threads is not None:
        parser.error("--threads is currently Codex-only; Gemini CLI manages subagent execution")

    values = GeminiConfigValues(
        orchestrator_model=args.orchestrator_model or "gemini-3.8-flash",
        orchestrator_thinking=args.orchestrator_thinking or "high",
        worker_model=args.worker_model or "gemini-3.8-flash",
        explorer_thinking=args.explorer_thinking or "medium",
        architect_thinking=args.architect_thinking or "high",
        implementer_thinking=args.implementer_thinking or "high",
        tester_thinking=args.tester_thinking or "low",
        reviewer_thinking=args.reviewer_thinking or "high",
    )
    return "gemini-3.8-flash", values


def _init(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.provider == "codex":
        preset, values = _codex_values(args, parser)
    else:
        preset, values = _gemini_values(args, parser)

    result = install(
        provider=args.provider,
        scope=args.scope,
        project_dir=args.project_dir,
        values=values,
        dry_run=args.dry_run,
    )

    action = "Would configure" if args.dry_run else "Configured"
    print(f"{action} CortexRelay ({args.provider} / {preset}):")
    if isinstance(values, ConfigValues):
        print(f"  primary: {values.orchestrator_model} / {values.orchestrator_effort}")
        print(f"  workers: {values.worker_model} / {values.worker_effort}")
        print(f"  threads: {values.max_threads}")
    else:
        print(f"  primary: {values.orchestrator_model} / {values.orchestrator_thinking.upper()}")
        print(f"  workers: {values.worker_model}")
        for name in ("explorer", "architect", "implementer", "tester", "reviewer"):
            print(f"    {name}: {values.thinking_for(name).upper()}")
    print(f"  config:  {result.config_path}")
    print(f"  guide:   {result.instructions_path}")
    for path in result.agent_paths:
        print(f"  agent:   {path}")
    if result.backups:
        print("  backups:")
        for path in result.backups:
            print(f"    {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(
            args.provider,
            args.scope,
            args.project_dir,
            runtime_only=args.runtime_only,
        )
    if args.command == "providers":
        return _providers()
    if args.command == "delegate":
        return _delegate(args)
    if args.command == "serve":
        return _serve(args)
    return _init(args, parser)


if __name__ == "__main__":
    sys.exit(main())
