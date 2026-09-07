from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .configurator import ConfigValues
from .gemini import GEMINI_THINKING_LEVELS, GeminiConfigValues
from .installer import expected_paths, install


CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
PROVIDERS = ("codex", "gemini")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex-relay",
        description="Configure cost-aware multi-agent orchestration for Codex or Gemini CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Install or update CortexRelay configuration.")
    init_parser.add_argument("--provider", choices=PROVIDERS, default="codex")
    init_parser.add_argument("--scope", choices=("project", "user"), default="project")
    init_parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    init_parser.add_argument("--preset", choices=("astra-luna", "gemini-3.8-flash"))

    init_parser.add_argument("--orchestrator-model")
    init_parser.add_argument("--worker-model")

    init_parser.add_argument("--orchestrator-effort", choices=CODEX_EFFORTS)
    init_parser.add_argument("--worker-effort", choices=CODEX_EFFORTS)
    init_parser.add_argument("--threads", type=int)

    init_parser.add_argument("--orchestrator-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--explorer-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--architect-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--implementer-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--tester-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--reviewer-thinking", choices=GEMINI_THINKING_LEVELS)
    init_parser.add_argument("--dry-run", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Check whether expected CortexRelay files exist.")
    doctor_parser.add_argument("--provider", choices=PROVIDERS, default="codex")
    doctor_parser.add_argument("--scope", choices=("project", "user"), default="project")
    doctor_parser.add_argument("--project-dir", type=Path, default=Path.cwd())

    return parser


def _doctor(provider: str, scope: str, project_dir: Path) -> int:
    expected = expected_paths(provider=provider, scope=scope, project_dir=project_dir)
    missing = [path for path in expected if not path.exists()]
    if missing:
        print(f"CortexRelay {provider} configuration is incomplete:")
        for path in missing:
            print(f"  MISSING {path}")
        return 1

    print(f"CortexRelay {provider} configuration looks complete:")
    for path in expected:
        print(f"  OK      {path}")
    return 0


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
        worker_model=args.worker_model or "gpt-5.6-luna",
        worker_effort=args.worker_effort or "xhigh",
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(args.provider, args.scope, args.project_dir)

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


if __name__ == "__main__":
    sys.exit(main())
