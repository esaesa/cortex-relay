from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

from . import __version__
from .configurator import ConfigValues
from .core.models import QualityGates, TaskBudget, TaskSpec
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

    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactively configure provider/model profiles and role assignments.",
    )
    setup_parser.add_argument("--scope", choices=("project", "user"), default="project")
    setup_parser.add_argument("--workspace", type=Path, default=Path.cwd())

    config_parser = subparsers.add_parser(
        "config",
        help="Interactively edit CortexRelay profiles, roles, and presets.",
    )
    config_parser.add_argument("--scope", choices=("project", "user"), default="project")
    config_parser.add_argument("--workspace", type=Path, default=Path.cwd())

    profiles_parser = subparsers.add_parser(
        "profiles",
        help="Show merged execution profiles, presets, and role assignments.",
    )
    profiles_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    profiles_parser.add_argument("--preset")
    profiles_parser.add_argument("--json", action="store_true", dest="as_json")

    status_parser = subparsers.add_parser(
        "status",
        help="Show current and recent CortexRelay delegation status for this workspace.",
    )
    status_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    status_parser.add_argument("--limit", type=int, default=12)
    status_parser.add_argument("--watch", action="store_true")
    status_parser.add_argument("-v", "--verbose", action="count", default=0)
    status_parser.add_argument("--interval", type=float, default=1.0)
    status_parser.add_argument(
        "--active-only", action="store_true",
        help="Show active requests only (also the default for --watch).",
    )
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    status_parser.add_argument("--clear-completed", action="store_true")

    history_parser = subparsers.add_parser(
        "history",
        help="Show completed CortexRelay delegations for this workspace.",
    )
    history_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    history_parser.add_argument("--limit", type=int, default=30)
    history_parser.add_argument("--json", action="store_true", dest="as_json")
    history_parser.add_argument("--clear", action="store_true")

    group_parser = subparsers.add_parser(
        "group",
        help="Show a persisted dependency group as a workflow graph.",
    )
    group_parser.add_argument("group_id")
    group_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    group_parser.add_argument("--watch", action="store_true")
    group_parser.add_argument("--interval", type=float, default=1.0)
    group_parser.add_argument("--json", action="store_true", dest="as_json")

    analytics_parser = subparsers.add_parser(
        "analyze-routing",
        help="Summarize historical route outcomes without changing routing.",
    )
    analytics_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    analytics_parser.add_argument("--min-samples", type=int, default=1)
    analytics_parser.add_argument("--json", action="store_true", dest="as_json")

    gc_parser = subparsers.add_parser(
        "gc",
        help="Prune old terminal CortexRelay state without deleting worktrees.",
    )
    gc_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    gc_parser.add_argument("--dry-run", action="store_true")
    gc_parser.add_argument("--retention-days", type=int)
    gc_parser.add_argument("--max-completed-tasks", type=int)
    gc_parser.add_argument("--max-event-log-mb", type=int)
    gc_parser.add_argument("--json", action="store_true", dest="as_json")

    models_parser = subparsers.add_parser(
        "models",
        help="Discover models exposed by a runtime provider.",
    )
    models_parser.add_argument(
        "--provider",
        choices=("opencode", "antigravity"),
        default="opencode",
    )
    models_parser.add_argument("--refresh", action="store_true")
    models_parser.add_argument("--verbose", action="store_true")
    models_parser.add_argument("--json", action="store_true", dest="as_json")

    launch_parser = subparsers.add_parser(
        "launch",
        help="Launch an interactive host agent from a configured orchestrator profile.",
    )
    launch_parser.add_argument("--role", default="orchestrator")
    launch_parser.add_argument("--profile")
    launch_parser.add_argument("--preset")
    launch_parser.add_argument("--workspace", type=Path, default=Path.cwd())
    launch_parser.add_argument("--prompt")

    delegate_parser = subparsers.add_parser(
        "delegate",
        help="Delegate one bounded task through the provider-neutral runtime.",
    )
    delegate_parser.add_argument("objective")
    delegate_parser.add_argument("--role", default="reviewer")
    delegate_parser.add_argument(
        "--profile",
        help="Named execution profile. Overrides the configured role mapping.",
    )
    delegate_parser.add_argument(
        "--preset",
        help="Runtime preset used for role-to-profile mapping.",
    )
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
    delegate_parser.add_argument("--max-tokens", type=int)
    delegate_parser.add_argument("--max-cost", type=float)
    delegate_parser.add_argument("--require-changed-files", action="store_true")
    delegate_parser.add_argument("--require-tests", action="store_true")
    delegate_parser.add_argument("--allowed-path", action="append", default=[], dest="allowed_paths")
    delegate_parser.add_argument("--max-failed-tests", type=int)
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
    serve_parser.add_argument("--a2a-profile")
    serve_parser.add_argument("--a2a-preset")
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


def _setup_runtime(args: argparse.Namespace) -> int:
    from .wizard import run_setup

    try:
        run_setup(
            workspace=args.workspace,
            scope=args.scope,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _config_runtime(args: argparse.Namespace) -> int:
    from .wizard import run_config_editor

    try:
        run_config_editor(
            workspace=args.workspace,
            scope=args.scope,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


def _profiles(args: argparse.Namespace) -> int:
    registry = default_registry()
    try:
        data = registry.profile_config(args.workspace, preset=args.preset)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0

    sources = data.get("sources") or []
    print("CortexRelay execution profiles:")
    print(f"  sources: {', '.join(sources) if sources else '(built-in legacy routing only)'}")
    print(f"  preset:  {data.get('active_preset') or '(none)'}")
    roles = data.get("roles") or {}
    if roles:
        print("  roles:")
        for role, profile in sorted(roles.items()):
            print(f"    {role}: {profile}")
    profiles = data.get("profiles") or {}
    if profiles:
        print("  profiles:")
        for name, profile in sorted(profiles.items()):
            model = profile.get("model") or "(provider default)"
            billing = profile.get("billing_class") or "unspecified"
            print(
                f"    {name}: {profile['provider']} / {model} / "
                f"{profile['reasoning']} [{billing}]"
            )
            fallbacks = profile.get("fallbacks") or []
            if fallbacks:
                print(f"      fallbacks: {', '.join(fallbacks)}")
    return 0


def _group(args: argparse.Namespace) -> int:
    from .observability import RunStore
    from .runtime.workflow_view import group_snapshot, render_group

    if args.interval < 0.2:
        print("--interval must be at least 0.2 seconds", file=sys.stderr)
        return 2
    store = RunStore()

    def snapshot():
        return group_snapshot(store, args.workspace, args.group_id)

    if args.as_json:
        if args.watch:
            print("--watch cannot be combined with --json", file=sys.stderr)
            return 2
        print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
        return 0
    if not args.watch:
        _print_dashboard(render_group(snapshot()))
        return 0
    try:
        while True:
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            _print_dashboard(render_group(snapshot()))
            print("\nWatching group. Ctrl+C to exit.")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def _analyze_routing(args: argparse.Namespace) -> int:
    from .observability import RunStore
    from .runtime.analytics import analyze_routing, render_routing_analysis

    if args.min_samples < 1:
        print("--min-samples must be at least 1", file=sys.stderr)
        return 2
    report = analyze_routing(
        RunStore(), args.workspace, min_samples=args.min_samples
    )
    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_dashboard(render_routing_analysis(report))
    return 0


def _gc(args: argparse.Namespace) -> int:
    from .observability import RunStore

    registry = default_registry()
    try:
        state = registry.profiles.load(args.workspace).state
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    result = RunStore().gc(
        workspace=args.workspace,
        retention_days=args.retention_days or state.retention_days,
        max_completed_tasks=args.max_completed_tasks or state.max_completed_tasks,
        max_event_log_mb=args.max_event_log_mb or state.max_event_log_mb,
        dry_run=args.dry_run,
    )
    if args.as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        mode = "Would prune" if args.dry_run else "Pruned"
        print(f"{mode} {result['count']} terminal task record(s).")
        for item in result["tasks"][:25]:
            print(f"  {item['task_id']}")
        if len(result["tasks"]) > 25:
            print(f"  ... and {len(result['tasks']) - 25} more")
    return 0


def _status(args: argparse.Namespace, *, completed_only: bool = False) -> int:
    from .observability import RunStore, render_dashboard

    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2

    store = RunStore()
    clear_requested = bool(
        getattr(args, "clear_completed", False) or getattr(args, "clear", False)
    )
    if clear_requested:
        removed = store.clear_completed(args.workspace)
        print(f"Cleared {removed} completed CortexRelay task record(s).")
        if completed_only or not getattr(args, "watch", False):
            return 0

    if getattr(args, "watch", False) and args.as_json:
        print("--watch cannot be combined with --json", file=sys.stderr)
        return 2

    interval = float(getattr(args, "interval", 1.0))
    if interval < 0.2:
        print("--interval must be at least 0.2 seconds", file=sys.stderr)
        return 2

    def snapshot():
        return store.snapshot(
            args.workspace,
            limit=args.limit,
            active_only=bool(getattr(args, "active_only", False) or getattr(args, "watch", False)),
            completed_only=completed_only,
        )

    if args.as_json:
        print(json.dumps(snapshot(), indent=2, ensure_ascii=False))
        return 0

    if not getattr(args, "watch", False):
        _print_dashboard(
            render_dashboard(
                snapshot(),
                title="CortexRelay history" if completed_only else "CortexRelay status",
                completed_only=completed_only,
                verbosity=getattr(args, "verbose", 0),
            )
        )
        return 0

    try:
        while True:
            view = render_dashboard(
                snapshot(), title="CortexRelay live status",
                verbosity=getattr(args, "verbose", 0),
                live_only=True,
            )
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            _print_dashboard(view)
            print("\nWatching for changes. Ctrl+C to exit.")
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _print_dashboard(text: str) -> None:
    """Print dashboard symbols safely on limited Windows code pages."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def _models(args: argparse.Namespace) -> int:
    if args.provider == "opencode":
        from .providers.opencode import OpenCodeAdapter

        adapter = OpenCodeAdapter()
        if not adapter.capabilities().available:
            print(adapter.capabilities().detail, file=sys.stderr)
            return 1

        models = adapter.discover_models(
            refresh=args.refresh,
            verbose=args.verbose or args.as_json,
        )
        title = "OpenCode"
    else:
        from .providers.antigravity import AntigravityAdapter

        adapter = AntigravityAdapter()
        if not adapter.capabilities().available:
            print(adapter.capabilities().detail, file=sys.stderr)
            return 1

        models = adapter.discover_models()
        title = "Antigravity"

    if not models:
        print(f"No {title} models were discovered.", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(models, indent=2, ensure_ascii=False))
    else:
        print(f"{title} models:")
        for model_id in sorted(models):
            metadata = models.get(model_id) or {}
            label = metadata.get("label")
            suffix = f" — {label}" if isinstance(label, str) and label != model_id else ""
            print(f"  {model_id}{suffix}")
    return 0


def _orchestrator_contract(config, *, preset: str | None = None) -> str:
    roles = config.effective_roles(preset)
    lines = [
        "You are the CortexRelay orchestrator.",
        "Keep planning, arbitration, and final synthesis in this host session.",
        "Use CortexRelay delegate_async for substantial implementation, testing, review, or parallel exploration.",
        "Use task_events with sequence cursors for progress instead of repeatedly requesting full status.",
        "For staged write workflows, use depends_on and inherit_workspace_from so downstream workers consume the exact immutable artifact produced upstream.",
        "Inspect task_diff before task_apply. Never apply or discard a worker worktree without explicit user intent.",
        "Respect configured budgets, quality gates, and scheduler queues. Do not bypass them with native subagents.",
        "",
        "Configured roles:",
    ]
    for role, profile_name in sorted(roles.items()):
        profile = config.profiles.get(profile_name)
        if profile is None:
            lines.append(f"- {role}: {profile_name} (unresolved)")
            continue
        model = profile.model or "(provider default)"
        lines.append(
            f"- {role}: {profile.name} → {profile.provider}/{model} "
            f"reasoning={profile.reasoning} access={profile.access}"
        )
    return "\n".join(lines)


def _launch(args: argparse.Namespace) -> int:
    if importlib.util.find_spec("mcp") is None:
        print(
            'Interactive orchestrator launch requires MCP support. Install with: '
            'pip install "cortex-relay[mcp]"',
            file=sys.stderr,
        )
        return 2

    registry = default_registry()
    try:
        config = registry.profiles.load(args.workspace)
        selector = TaskSpec(
            objective="Launch interactive CortexRelay orchestrator",
            role=args.role,
            profile=args.profile,
            preset=args.preset,
            workspace=args.workspace,
        )
        profile = config.profile_for_task(selector)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if profile is None:
        print(
            f"No execution profile is configured for role {args.role!r}. "
            "Run 'cortex-relay setup' or pass --profile.",
            file=sys.stderr,
        )
        return 2
    if profile.provider != "opencode":
        print(
            "Interactive host launch currently supports OpenCode profiles. "
            f"Selected profile {profile.name!r} uses provider {profile.provider!r}.",
            file=sys.stderr,
        )
        return 2

    from .providers.opencode import OpenCodeAdapter

    from .observability import RunStore

    run_store = RunStore()
    session_id = run_store.start_session(
        args.workspace,
        profile=profile.name,
        provider=profile.provider,
        model=profile.model,
        reasoning=profile.reasoning,
        preset=args.preset,
    )

    metadata = {
        "profile_options": dict(profile.options),
        "billing_class": profile.billing_class,
        "session_id": session_id,
    }
    contract = _orchestrator_contract(config, preset=args.preset)
    metadata["host_prompt"] = (
        contract + "\n\nInitial user task:\n" + args.prompt
        if args.prompt else contract
    )

    host_task = TaskSpec(
        objective="Interactive CortexRelay orchestration session",
        role=args.role,
        profile=profile.name,
        preset=args.preset,
        provider=profile.provider,
        workspace=args.workspace,
        access=profile.access,
        reasoning=profile.reasoning,
        model=profile.model,
        metadata=metadata,
    )
    adapter = OpenCodeAdapter()
    exit_code = 2
    try:
        print(f"CortexRelay session: {session_id}")
        print("Live dashboard: open another terminal and run 'cortex-relay status --watch'")
        exit_code = adapter.launch_host(host_task)
        return exit_code
    except KeyboardInterrupt:
        exit_code = 130
        print("\nCortexRelay session interrupted.")
        return exit_code
    except (RuntimeError, ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        run_store.end_session(
            args.workspace,
            session_id,
            exit_code=exit_code,
        )


def _delegate(args: argparse.Namespace) -> int:
    task = TaskSpec(
        objective=args.objective,
        role=args.role,
        profile=args.profile,
        preset=args.preset,
        provider=args.provider,
        workspace=args.workspace,
        access=args.access,
        reasoning=args.reasoning,
        model=args.model,
        acceptance_criteria=tuple(args.acceptance_criteria),
        timeout_seconds=args.timeout_seconds,
        isolate_write=args.access == "workspace_write" and args.isolate_write,
        budget=TaskBudget(
            max_tokens=args.max_tokens,
            max_cost=args.max_cost,
        ),
        quality_gates=QualityGates(
            require_changed_files=args.require_changed_files,
            require_tests=args.require_tests,
            allowed_paths=tuple(args.allowed_paths),
            max_failed_tests=args.max_failed_tests,
        ),
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
        observability = result.metadata.get("observability")
        if isinstance(observability, dict):
            print(f"  task id:  {observability.get('task_id')}")
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
                profile=args.a2a_profile,
                preset=args.a2a_preset,
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
    if args.command == "setup":
        return _setup_runtime(args)
    if args.command == "config":
        return _config_runtime(args)
    if args.command == "profiles":
        return _profiles(args)
    if args.command == "status":
        return _status(args)
    if args.command == "history":
        return _status(args, completed_only=True)
    if args.command == "group":
        return _group(args)
    if args.command == "analyze-routing":
        return _analyze_routing(args)
    if args.command == "gc":
        return _gc(args)
    if args.command == "models":
        return _models(args)
    if args.command == "launch":
        return _launch(args)
    if args.command == "delegate":
        return _delegate(args)
    if args.command == "serve":
        return _serve(args)
    return _init(args, parser)


if __name__ == "__main__":
    sys.exit(main())
