from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .configurator import ConfigValues
from .installer import install


EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cortex-relay",
        description="Configure cost-aware Codex orchestration with a strong primary model and efficient subagents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Install or update CortexRelay configuration.")
    init_parser.add_argument("--scope", choices=("project", "user"), default="project")
    init_parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    init_parser.add_argument("--preset", choices=("astra-luna",), default="astra-luna")
    init_parser.add_argument("--orchestrator-model", default="gpt-6-astra")
    init_parser.add_argument("--orchestrator-effort", choices=EFFORTS, default="low")
    init_parser.add_argument("--worker-model", default="gpt-5.6-luna")
    init_parser.add_argument("--worker-effort", choices=EFFORTS, default="xhigh")
    init_parser.add_argument("--threads", type=int, default=6)
    init_parser.add_argument("--dry-run", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Check whether expected CortexRelay files exist.")
    doctor_parser.add_argument("--scope", choices=("project", "user"), default="project")
    doctor_parser.add_argument("--project-dir", type=Path, default=Path.cwd())

    return parser


def _doctor(scope: str, project_dir: Path) -> int:
    if scope == "project":
        config_dir = project_dir.resolve() / ".codex"
        instructions = project_dir.resolve() / "AGENTS.md"
    else:
        config_dir = Path.home() / ".codex"
        instructions = config_dir / "AGENTS.md"

    expected = [config_dir / "config.toml", instructions]
    expected.extend(config_dir / "agents" / f"{name}.toml" for name in ("explorer", "architect", "implementer", "tester", "reviewer"))

    missing = [path for path in expected if not path.exists()]
    if missing:
        print("CortexRelay configuration is incomplete:")
        for path in missing:
            print(f"  MISSING {path}")
        return 1

    print("CortexRelay configuration looks complete:")
    for path in expected:
        print(f"  OK      {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor(args.scope, args.project_dir)

    if args.threads < 1:
        parser.error("--threads must be at least 1")

    values = ConfigValues(
        orchestrator_model=args.orchestrator_model,
        orchestrator_effort=args.orchestrator_effort,
        worker_model=args.worker_model,
        worker_effort=args.worker_effort,
        max_threads=args.threads,
    )
    result = install(
        scope=args.scope,
        project_dir=args.project_dir,
        values=values,
        dry_run=args.dry_run,
    )

    action = "Would configure" if args.dry_run else "Configured"
    print(f"{action} CortexRelay ({args.preset}):")
    print(f"  primary: {values.orchestrator_model} / {values.orchestrator_effort}")
    print(f"  workers: {values.worker_model} / {values.worker_effort}")
    print(f"  threads: {values.max_threads}")
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
