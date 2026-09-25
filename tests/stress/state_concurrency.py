#!/usr/bin/env python3
"""Concurrency and fault-injection stress harness for durable task state.

Milestone 4 of the production hardening plan. Several threads in several child
processes drive the public ``RunStore`` API against one shared state root, then
the parent verifies the durability invariants supervision depends on:

* required updates are never lost under contention - per-worker counters on
  shared records must be exact;
* required writes that fail do so loudly (``OSError`` / ``ValueError`` only)
  and leave the record untouched;
* every task record stays valid JSON, with no temporary publish files left
  behind;
* injected transient faults are absorbed by the bounded retry policy, while
  exhausted faults are reported through ``state_failure_report()``.

The module is deliberately not named ``test_*`` and ``tests/stress`` is not a
package, so ``unittest discover -s tests`` never collects it.

Profiles
--------

======= ========== ======= ============ =============
profile processes threads iterations  shared tasks
======= ========== ======= ============ =============
fast      1         4        25            4
windows   2         4       100            8
soak      4         4       500           16
======= ========== ======= ============ =============

Usage::

    python tests/stress/state_concurrency.py --profile windows
    python tests/stress/state_concurrency.py --profile windows \\
        --fault-rate 0.02 --fault-point mixed
    python tests/stress/state_concurrency.py --profile fast --iterations 5 \\
        --fault-rate 1.0 --fault-point before_publish
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cortex_relay.core.models import TaskSpec
from cortex_relay.observability import RunStore

RESULT_PREFIX = "RESULT "
ARTIFACT_DIR_ENV = "CORTEX_RELAY_ARTIFACT_DIR"
FAULT_POINTS = ("before_lock_open", "before_read", "before_publish")
MIXED_FAULT_POINT = "mixed"
SUMMARY_PATTERN = re.compile(r"^w\d+:\d+$")
SCRIPT = Path(__file__).resolve()


@dataclass(frozen=True)
class Profile:
    name: str
    processes: int
    threads: int
    iterations: int
    shared_tasks: int
    timeout_seconds: float


PROFILES: dict[str, Profile] = {
    "fast": Profile("fast", 1, 4, 25, 4, 180.0),
    "windows": Profile("windows", 2, 4, 100, 8, 300.0),
    "soak": Profile("soak", 4, 4, 500, 16, 900.0),
}

class TransientFaultInjector:
    """Raise retriable ``PermissionError``s at configured state-store points."""

    def __init__(self, rate: float, points: frozenset[str], seed: int) -> None:
        self.rate = rate
        self.points = points
        self.fired: dict[str, int] = {}
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def __call__(self, point: str, payload: Any) -> None:
        del payload
        if point not in self.points:
            return
        with self._lock:
            if self._rng.random() >= self.rate:
                return
            self.fired[point] = self.fired.get(point, 0) + 1
        raise PermissionError(f"injected transient state fault at {point}")


def _record(objective: str, workspace: Path) -> TaskSpec:
    return TaskSpec(objective=objective, provider="stress", workspace=workspace)


def _worker_main(
    *,
    store: RunStore,
    workspace: Path,
    worker_id: int,
    iterations: int,
    shared_tasks: list[str],
    private_task_id: str,
    seed: int,
) -> dict[str, Any]:
    """Run one worker's slice of the workload and report exact expectations."""
    rng = random.Random(seed)
    key = f"w{worker_id}"
    counts = {task_id: 0 for task_id in shared_tasks}
    ops = {
        "starts": 0,
        "shared_updates": 0,
        "private_updates": 0,
        "reads": 0,
        "heartbeats": 0,
        "recovery_events": 0,
    }
    failures = {"os": 0, "value": 0, "read_misses": 0}
    unexpected: list[str] = []
    private_started = False
    private_attempt = 0
    private_events = 0

    try:
        store.start_task(
            _record(f"stress private w{worker_id}", workspace),
            task_id=private_task_id,
        )
    except OSError:
        failures["os"] += 1
    except Exception:
        unexpected.append(traceback.format_exc())
    else:
        # Non-async records are published best-effort, so a hard fault can leave
        # the task absent; the expectation below records what we actually saw.
        private_started = True
        ops["starts"] += 1

    for iteration in range(iterations):
        if rng.random() < 0.5:
            time.sleep(rng.random() * 0.002)
        target = shared_tasks[(worker_id + iteration) % len(shared_tasks)]

        # Contended required write: a per-worker key plus a shared multi-key
        # field, so both lost updates and whole-record clobbers are visible.
        value = counts[target] + 1
        try:
            store.update_task(
                workspace,
                target,
                **{key: value, "summary": f"{key}:{iteration}"},
            )
        except OSError:
            failures["os"] += 1
        except ValueError:
            failures["value"] += 1
        except Exception:
            unexpected.append(traceback.format_exc())
        else:
            counts[target] = value
            ops["shared_updates"] += 1

        if private_started:
            current = store.get_task(workspace, private_task_id)
            if current is None:
                failures["read_misses"] += 1
            else:
                attempt_value = int(current.get("attempt") or 0) + 1
                try:
                    store.update_task(
                        workspace,
                        private_task_id,
                        attempt=attempt_value,
                        summary=f"{key}:{iteration}",
                    )
                except OSError:
                    failures["os"] += 1
                except ValueError:
                    failures["value"] += 1
                except Exception:
                    unexpected.append(traceback.format_exc())
                else:
                    private_attempt = attempt_value
                    ops["private_updates"] += 1

        if iteration % 4 == 0:
            try:
                seen = store.get_task(workspace, target)
            except OSError:
                failures["os"] += 1
            except Exception:
                unexpected.append(traceback.format_exc())
            else:
                ops["reads"] += 1
                if seen is None:
                    failures["read_misses"] += 1

        if iteration % 2 == 0 and private_started:
            try:
                store.record_heartbeat(workspace, private_task_id, os.getpid(), True)
            except OSError:
                failures["os"] += 1
            except Exception:
                unexpected.append(traceback.format_exc())
            else:
                ops["heartbeats"] += 1

        if iteration % 5 == 0 and private_started:
            try:
                store.record_recovery_event(
                    workspace,
                    private_task_id,
                    attempt=1,
                    reason="stress",
                    resume=True,
                )
            except OSError:
                failures["os"] += 1
            except ValueError:
                failures["value"] += 1
            except Exception:
                unexpected.append(traceback.format_exc())
            else:
                private_events += 1
                ops["recovery_events"] += 1

    return {
        "worker_id": worker_id,
        "key": key,
        "shared": counts,
        "private_task_id": private_task_id,
        "private_started": private_started,
        "private_attempt": private_attempt,
        "private_events": private_events,
        "ops": ops,
        "failures": failures,
        "unexpected": unexpected,
    }


def _run_worker_threads(
    *,
    store: RunStore,
    workspace: Path,
    child_index: int,
    threads: int,
    iterations: int,
    shared_tasks: list[str],
    seed: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any] | None] = [None] * threads

    def run(thread_index: int) -> None:
        worker_id = child_index * threads + thread_index
        results[thread_index] = _worker_main(
            store=store,
            workspace=workspace,
            worker_id=worker_id,
            iterations=iterations,
            shared_tasks=shared_tasks,
            private_task_id=f"stress-p{child_index}-t{thread_index}",
            seed=seed + worker_id,
        )

    handles = [
        threading.Thread(target=run, args=(index,), daemon=True)
        for index in range(threads)
    ]
    for handle in handles:
        handle.start()
    for handle in handles:
        handle.join()
    return [result for result in results if result is not None]


def _child_main(args: argparse.Namespace) -> int:
    state_root = Path(args.state_root)
    workspace = Path(args.workspace)
    injector = None
    if args.fault_rate > 0:
        points = (
            frozenset(FAULT_POINTS)
            if args.fault_point == MIXED_FAULT_POINT
            else frozenset({args.fault_point})
        )
        injector = TransientFaultInjector(
            args.fault_rate, points, seed=args.seed + args.child_index
        )
    store = RunStore(state_root, fault_injector=injector)
    started = time.monotonic()
    workers = _run_worker_threads(
        store=store,
        workspace=workspace,
        child_index=args.child_index,
        threads=args.threads,
        iterations=args.iterations,
        shared_tasks=[
            f"stress-shared-{index:02d}" for index in range(args.shared_tasks)
        ],
        seed=args.seed + args.child_index,
    )

    expected_shared: dict[str, dict[str, int]] = {}
    expected_private: dict[str, dict[str, Any]] = {}
    totals = {"ops": {}, "failures": {"os": 0, "value": 0, "read_misses": 0}}
    unexpected: list[str] = []
    for worker in workers:
        for task_id, count in worker["shared"].items():
            expected_shared.setdefault(task_id, {})[worker["key"]] = count
        expected_private[worker["private_task_id"]] = {
            "started": worker["private_started"],
            "attempt": worker["private_attempt"],
            "events": worker["private_events"],
        }
        for name, value in worker["ops"].items():
            totals["ops"][name] = totals["ops"].get(name, 0) + value
        for name, value in worker["failures"].items():
            totals["failures"][name] = totals["failures"].get(name, 0) + value
        unexpected.extend(worker["unexpected"])

    payload = {
        "child": args.child_index,
        "pid": os.getpid(),
        "profile": args.profile,
        "workers": len(workers),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "expected_shared": expected_shared,
        "expected_private": expected_private,
        "ops": totals["ops"],
        "failures": totals["failures"],
        "state_failure_total": store.state_failure_report()["total"],
        "faults_fired": dict(injector.fired) if injector else {},
        "unexpected": unexpected,
    }
    print(RESULT_PREFIX + json.dumps(payload), flush=True)
    return 0


def _spawn_child(
    args: argparse.Namespace,
    *,
    child_index: int,
    state_root: Path,
    workspace: Path,
    processes: int,
    threads: int,
    iterations: int,
    shared_tasks: int,
    seed: int,
) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--child",
        "--profile",
        args.profile,
        "--child-index",
        str(child_index),
        "--state-root",
        str(state_root),
        "--workspace",
        str(workspace),
        "--threads",
        str(threads),
        "--iterations",
        str(iterations),
        "--shared-tasks",
        str(shared_tasks),
        "--processes",
        str(processes),
        "--fault-rate",
        str(args.fault_rate),
        "--fault-point",
        args.fault_point,
        "--seed",
        str(seed),
    ]
    if args.root is not None:
        command.extend(["--root", args.root])
    return subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect_child(
    process: subprocess.Popen[str], timeout_seconds: float, child_index: int
) -> dict[str, Any]:
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise AssertionError(
            f"child {child_index} exceeded the {timeout_seconds:.0f}s profile timeout"
        ) from None
    line = next(
        (item for item in reversed(stdout.splitlines()) if item.startswith(RESULT_PREFIX)),
        None,
    )
    if line is None or process.returncode != 0:
        raise AssertionError(
            f"child {child_index} failed (exit {process.returncode})\n"
            f"stdout:\n{stdout[-4000:]}\nstderr:\n{stderr[-4000:]}"
        )
    return json.loads(line[len(RESULT_PREFIX):])


def _load_state(
    state_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Path], list[str]]:
    """Parse every task record and locate every event stream under the root."""
    records: dict[str, dict[str, Any]] = {}
    events: dict[str, Path] = {}
    problems: list[str] = []
    for path in sorted(state_root.glob("*/tasks/*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"unreadable task record {path.name}: {exc}")
            continue
        if not isinstance(record, dict) or record.get("task_id") != path.stem:
            problems.append(f"malformed task record {path.name}")
            continue
        records[path.stem] = record
    for path in sorted(state_root.glob("*/events/*.jsonl")):
        events[path.stem] = path
    if not records:
        problems.append("no task records were written at all")
    return records, events, problems


def _verify(
    *,
    state_root: Path,
    records: dict[str, dict[str, Any]],
    events: dict[str, Path],
    record_problems: list[str],
    expected_shared: dict[str, dict[str, int]],
    expected_private: dict[str, dict[str, Any]],
    ops: dict[str, int],
    failures: dict[str, int],
    state_failure_total: int,
    fault_rate: float,
    faults_fired: dict[str, int],
    unexpected: list[str],
) -> list[str]:
    problems = list(record_problems)
    fault_free = fault_rate <= 0

    for task_id, expectations in sorted(expected_shared.items()):
        record = records.get(task_id)
        if record is None:
            problems.append(f"shared task record missing: {task_id}")
            continue
        if record.get("objective") != f"stress shared task {task_id}":
            problems.append(f"shared record clobbered: {task_id}")
        if ops.get("shared_updates"):
            summary = record.get("summary")
            if summary is None or not SUMMARY_PATTERN.match(str(summary)):
                problems.append(
                    f"shared summary lost or invalid on {task_id}: {summary!r}"
                )
        for key, expected in sorted(expectations.items()):
            actual = record.get(key)
            if expected == 0:
                if actual is not None:
                    problems.append(
                        f"unexpected counter on {task_id}.{key}: {actual!r}"
                    )
            elif actual != expected:
                problems.append(
                    f"lost update on {task_id}.{key}: expected {expected}, got {actual!r}"
                )

    for task_id, expectation in sorted(expected_private.items()):
        record = records.get(task_id)
        if record is None:
            # A missing private record is only acceptable when nothing was
            # written to it, or when hard faults swallowed a best-effort start.
            if expectation["attempt"] or expectation["events"]:
                problems.append(f"private task record missing: {task_id}")
            elif fault_free and expectation["started"]:
                problems.append(f"private task record missing: {task_id}")
            continue
        if record.get("attempt") != expectation["attempt"]:
            problems.append(
                f"lost required write on {task_id}.attempt: "
                f"expected {expectation['attempt']}, got {record.get('attempt')!r}"
            )
        event_path = events.get(task_id)
        lines = (
            event_path.read_text(encoding="utf-8").splitlines()
            if event_path is not None
            else []
        )
        for line in lines:
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"torn event line in {task_id}.jsonl: {exc}")
        if len(lines) != expectation["events"] and (
            fault_free or len(lines) > expectation["events"]
        ):
            problems.append(
                f"event count mismatch on {task_id}: "
                f"expected {expectation['events']}, got {len(lines)}"
            )

    leftovers = sorted(
        str(path.relative_to(state_root))
        for path in state_root.rglob("*.tmp")
        if path.is_file()
    )
    if leftovers:
        problems.append(f"temporary publish files left behind: {leftovers[:10]}")

    if fault_free:
        if state_failure_total:
            problems.append(
                f"fault-free run reported {state_failure_total} state failure(s)"
            )
        for name, value in sorted(failures.items()):
            if value:
                problems.append(f"fault-free run recorded {name} failures: {value}")
    else:
        if not sum(faults_fired.values()):
            problems.append("no injected fault fired; the run proved nothing")
        if fault_rate >= 1.0:
            if not state_failure_total:
                problems.append("exhausted faults were not reported by the run store")
            if not (failures.get("os") or failures.get("value")):
                problems.append(
                    "exhausted faults never surfaced as a loud worker failure"
                )

    for trace in unexpected:
        problems.append(f"unexpected worker exception:\n{trace}")
    return problems


def _parent_main(args: argparse.Namespace) -> int:
    profile = PROFILES[args.profile]
    processes = args.processes or profile.processes
    threads = args.threads or profile.threads
    iterations = args.iterations or profile.iterations
    shared_tasks = args.shared_tasks or profile.shared_tasks
    seed = args.seed
    base = Path(args.root) if args.root else None
    if base is None:
        env_root = os.environ.get(ARTIFACT_DIR_ENV)
        base = Path(env_root) if env_root else None
    keep = base is not None or args.keep
    base = base or Path(tempfile.mkdtemp(prefix="cortex-relay-stress-"))
    run_dir = base / f"run-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    state_root = run_dir / "state"
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    print(
        f"stress: profile={profile.name} processes={processes} threads={threads} "
        f"workers={processes * threads} iterations={iterations} "
        f"shared_tasks={shared_tasks} fault_rate={args.fault_rate} "
        f"fault_point={args.fault_point}"
    )

    store = RunStore(state_root)
    for index in range(shared_tasks):
        task_id = f"stress-shared-{index:02d}"
        store.start_task(
            _record(f"stress shared task {task_id}", workspace),
            task_id=task_id,
        )

    started = time.monotonic()
    children = [
        _spawn_child(
            args,
            child_index=index,
            state_root=state_root,
            workspace=workspace,
            processes=processes,
            threads=threads,
            iterations=iterations,
            shared_tasks=shared_tasks,
            seed=seed,
        )
        for index in range(processes)
    ]
    reports = [
        _collect_child(process, profile.timeout_seconds, index)
        for index, process in enumerate(children)
    ]
    elapsed = time.monotonic() - started

    expected_shared: dict[str, dict[str, int]] = {}
    expected_private: dict[str, dict[str, Any]] = {}
    ops: dict[str, int] = {}
    failures: dict[str, int] = {}
    faults_fired: dict[str, int] = {}
    unexpected: list[str] = []
    state_failure_total = 0
    for report in reports:
        for task_id, expectations in report["expected_shared"].items():
            merged = expected_shared.setdefault(task_id, {})
            for key, value in expectations.items():
                if key in merged:
                    raise AssertionError(f"duplicate worker key {key} in report")
                merged[key] = value
        expected_private.update(report["expected_private"])
        for name, value in report["ops"].items():
            ops[name] = ops.get(name, 0) + value
        for name, value in report["failures"].items():
            failures[name] = failures.get(name, 0) + value
        for name, value in report["faults_fired"].items():
            faults_fired[name] = faults_fired.get(name, 0) + value
        unexpected.extend(report["unexpected"])
        state_failure_total += report["state_failure_total"]

    records, events, record_problems = _load_state(state_root)
    problems = _verify(
        state_root=state_root,
        records=records,
        events=events,
        record_problems=record_problems,
        expected_shared=expected_shared,
        expected_private=expected_private,
        ops=ops,
        failures=failures,
        state_failure_total=state_failure_total,
        fault_rate=args.fault_rate,
        faults_fired=faults_fired,
        unexpected=unexpected,
    )
    try:
        store.verify_state_paths()
    except OSError as exc:
        problems.append(f"state doctor probe failed: {exc}")

    total_ops = sum(ops.values())
    print(
        f"stress: {total_ops} operations in {elapsed:.1f}s "
        f"({total_ops / max(elapsed, 0.001):.0f} ops/s)"
    )
    print(f"stress: ops={json.dumps(ops, sort_keys=True)}")
    print(f"stress: worker failures={json.dumps(failures, sort_keys=True)}")
    print(
        f"stress: state failures={state_failure_total} "
        f"injected faults fired={json.dumps(faults_fired, sort_keys=True)}"
    )
    report = {
        "profile": profile.name,
        "processes": processes,
        "threads": threads,
        "iterations": iterations,
        "shared_tasks": shared_tasks,
        "fault_rate": args.fault_rate,
        "fault_point": args.fault_point,
        "elapsed_seconds": round(elapsed, 3),
        "ops": ops,
        "failures": failures,
        "state_failure_total": state_failure_total,
        "faults_fired": faults_fired,
        "problems": problems,
        "run_dir": str(run_dir),
    }
    (run_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )

    if problems:
        print(f"stress: FAILED ({len(problems)} problem(s))")
        for problem in problems:
            print(f"  - {problem}")
        print(f"stress: run directory kept at {run_dir}")
        return 1

    print("stress: PASS")
    if keep:
        print(f"stress: run directory kept at {run_dir}")
        return 0
    shutil.rmtree(run_dir, ignore_errors=True)
    if base.exists() and not any(base.iterdir()):
        base.rmdir()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", choices=sorted(PROFILES), default="fast")
    parser.add_argument("--processes", type=int, default=None)
    parser.add_argument("--threads", type=int, default=None)
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--shared-tasks", type=int, default=None)
    parser.add_argument("--fault-rate", type=float, default=0.0)
    parser.add_argument(
        "--fault-point",
        choices=[*FAULT_POINTS, MIXED_FAULT_POINT],
        default=MIXED_FAULT_POINT,
    )
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--root", default=None, help="directory that stores run dirs")
    parser.add_argument("--keep", action="store_true", help="keep the run directory")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--child-index", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--state-root", default="", help=argparse.SUPPRESS)
    parser.add_argument("--workspace", default="", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not 0.0 <= args.fault_rate <= 1.0:
        raise SystemExit("--fault-rate must be between 0 and 1")
    if args.child:
        return _child_main(args)
    return _parent_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
