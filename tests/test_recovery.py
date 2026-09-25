import tempfile
import time
import unittest

from dataclasses import replace
from pathlib import Path

from cortex_relay.core.models import TaskResult, TaskSpec
from cortex_relay.core.profiles import runtime_config_from_mapping
from cortex_relay.core.registry import ProviderRegistry
from cortex_relay.observability import RunStore
from cortex_relay.providers.base import ProviderAdapter, ProviderCapabilities
from cortex_relay.runtime.agent_store import AgentStore
from cortex_relay.runtime.recovery import (
    RECOVERY_CONTINUATION_PROMPT,
    RecoveryPolicy,
    build_recovery_prompt,
    print_timeout_reserve_seconds,
    recovery_attempt,
    recovery_decision,
    remaining_runtime_seconds,
    stamp_logical_deadline,
)


def make_task(**metadata):
    return TaskSpec(
        objective="ship the parser",
        timeout_seconds=300,
        metadata=dict(metadata),
    )


def make_capabilities(*, session_mode="resumable", persistent_sessions=True):
    return ProviderCapabilities(
        name="fake",
        binary="fake",
        available=True,
        structured_output=True,
        model_selection=True,
        reasoning_control=True,
        read_only_policy=True,
        workspace_write=True,
        detail="fake",
        session_mode=session_mode,
        persistent_sessions=persistent_sessions,
    )


def print_timeout_result(**overrides):
    payload = {
        "status": "timeout",
        "provider": "fake",
        "model": "model",
        "summary": "print timeout cut the turn off",
        "termination_reason": "provider_print_timeout",
        "conversation_id": "provider-session-1",
    }
    payload.update(overrides)
    return TaskResult(**payload)


def success_result(**overrides):
    payload = {
        "status": "success",
        "provider": "fake",
        "model": "model",
        "summary": "finished after resuming",
        "conversation_id": "provider-session-1",
    }
    payload.update(overrides)
    return TaskResult(**payload)


class StaticResolver:
    def __init__(self, mapping):
        self.config = runtime_config_from_mapping(mapping)

    def load(self, _workspace):
        return self.config


class ScriptedSessionProvider(ProviderAdapter):
    """Resumable fake provider replaying a scripted list of turn results."""

    def __init__(
        self,
        results,
        *,
        name="fake",
        session_mode="resumable",
        persistent_sessions=True,
    ):
        self.name = name
        self._results = list(results)
        self._session_mode = session_mode
        self._persistent_sessions = persistent_sessions
        self.calls = []

    def capabilities(self):
        return make_capabilities(
            session_mode=self._session_mode,
            persistent_sessions=self._persistent_sessions,
        )

    def execute(self, task):
        self.calls.append(("execute", None, task))
        return self._next(task)

    def continue_session(self, task, provider_session_id):
        self.calls.append(("continue", provider_session_id, task))
        return self._next(task)

    def _next(self, task):
        if not self._results:
            raise AssertionError("scripted provider ran out of turns")
        result = self._results.pop(0)
        if result.model is None:
            result = replace(result, model=task.model)
        return result


class RecoveryPolicyTests(unittest.TestCase):
    def test_recovery_attempt_defaults_to_zero(self):
        self.assertEqual(recovery_attempt(make_task()), 0)
        self.assertEqual(recovery_attempt(make_task(_recovery_attempt=True)), 0)
        self.assertEqual(recovery_attempt(make_task(_recovery_attempt="2")), 0)
        self.assertEqual(recovery_attempt(make_task(_recovery_attempt=2.9)), 2)
        self.assertEqual(recovery_attempt(make_task(_recovery_attempt=-4)), 0)

    def test_logical_deadline_is_stamped_once(self):
        metadata = {}
        stamp_logical_deadline(metadata, 300, now=1000.0)
        self.assertEqual(metadata["_logical_deadline"], 1300.0)
        stamp_logical_deadline(metadata, 1, now=2000.0)
        self.assertEqual(metadata["_logical_deadline"], 1300.0)

        forced = {"_logical_deadline": "not-a-number"}
        stamp_logical_deadline(forced, 10, now=5.0)
        self.assertEqual(forced["_logical_deadline"], 15.0)

        carried = {"_logical_deadline": 42.5}
        stamp_logical_deadline(carried, 300, now=1000.0)
        self.assertEqual(carried["_logical_deadline"], 42.5)

    def test_remaining_runtime_prefers_the_logical_deadline(self):
        task = make_task()
        self.assertEqual(remaining_runtime_seconds(task), 300.0)

        stamped = make_task(_logical_deadline=1000.0)
        self.assertEqual(remaining_runtime_seconds(stamped, now=950.0), 50.0)
        self.assertEqual(remaining_runtime_seconds(stamped, now=1500.0), 0.0)

    def test_print_timeout_reserve_leaves_runtime_to_resume_into(self):
        self.assertEqual(print_timeout_reserve_seconds(300.0), 60.0)
        self.assertEqual(print_timeout_reserve_seconds(1000.0), 90.0)
        self.assertEqual(print_timeout_reserve_seconds(100.0), 30.0)
        self.assertEqual(print_timeout_reserve_seconds(10.0), 9.0)
        self.assertEqual(print_timeout_reserve_seconds(300.0, attempt=1), 5.0)
        self.assertEqual(print_timeout_reserve_seconds(3.0, attempt=1), 2.0)

        for timeout in (1, 5, 30, 300, 900):
            for attempt in (0, 1, 4):
                reserve = print_timeout_reserve_seconds(float(timeout), attempt=attempt)
                self.assertGreaterEqual(reserve, 0.0)
                self.assertLess(reserve, timeout)

    def test_recovery_prompt_keeps_the_original_objective_authoritative(self):
        task = TaskSpec(objective="Add retry logic", timeout_seconds=60)
        prompt = build_recovery_prompt(task)
        self.assertTrue(prompt.startswith(RECOVERY_CONTINUATION_PROMPT))
        self.assertIn("Original objective (authoritative):\nAdd retry logic", prompt)
        self.assertGreater(len(prompt), len(RECOVERY_CONTINUATION_PROMPT))

    def test_policy_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            RecoveryPolicy(max_attempts=-1)
        with self.assertRaises(ValueError):
            RecoveryPolicy(min_remaining_seconds=-1)


class RecoveryDecisionTests(unittest.TestCase):
    def _decide(self, result, *, task=None, capabilities=None, remaining=200.0):
        return recovery_decision(
            task if task is not None else make_task(),
            result,
            provider_capabilities=capabilities or make_capabilities(),
            remaining_runtime_seconds=remaining,
        )

    def test_resumes_a_print_timeout_on_a_resumable_provider(self):
        decision = self._decide(print_timeout_result())
        self.assertTrue(decision.resume)
        self.assertEqual(decision.reason, "resumable_print_timeout")
        self.assertEqual(decision.attempt, 0)
        self.assertEqual(decision.next_attempt, 1)
        self.assertIn(RECOVERY_CONTINUATION_PROMPT, decision.prompt or "")
        self.assertEqual(decision.remaining_runtime_seconds, 200.0)

    def test_refuses_terminal_statuses(self):
        cancelled = self._decide(print_timeout_result(status="cancelled"))
        self.assertFalse(cancelled.resume)
        self.assertEqual(cancelled.reason, "terminal_status:cancelled")

        budgeted = self._decide(print_timeout_result(status="budget_exceeded"))
        self.assertFalse(budgeted.resume)
        self.assertEqual(budgeted.reason, "terminal_status:budget_exceeded")

    def test_refuses_non_recoverable_termination_reasons(self):
        for reason in (
            "execution_timeout",
            "idle_timeout",
            "owner_lost",
            "lease_expired",
            "budget_exceeded",
            "cancelled",
            "provider_exception",
        ):
            decision = self._decide(print_timeout_result(termination_reason=reason))
            self.assertFalse(decision.resume, reason)
            self.assertEqual(decision.reason, f"non_recoverable_termination:{reason}")

    def test_refuses_results_that_are_not_recoverable_interruptions(self):
        unknown = self._decide(print_timeout_result(termination_reason="oom"))
        self.assertFalse(unknown.resume)
        self.assertEqual(unknown.reason, "not_a_recoverable_interruption")

        missing = self._decide(print_timeout_result(termination_reason=None))
        self.assertFalse(missing.resume)

        wrong_status = self._decide(
            print_timeout_result(status="success", termination_reason=None)
        )
        self.assertFalse(wrong_status.resume)
        self.assertEqual(wrong_status.reason, "not_a_recoverable_interruption")

        timeout_status = self._decide(
            print_timeout_result(termination_reason="execution_timeout")
        )
        self.assertFalse(timeout_status.resume)
        self.assertEqual(timeout_status.reason, "non_recoverable_termination:execution_timeout")

    def test_refuses_providers_without_a_resumable_session(self):
        closed = self._decide(
            print_timeout_result(),
            capabilities=make_capabilities(
                session_mode="closed_end", persistent_sessions=False
            ),
        )
        self.assertFalse(closed.resume)
        self.assertEqual(closed.reason, "provider_has_no_resumable_session")

        native_only = self._decide(
            print_timeout_result(),
            capabilities=make_capabilities(session_mode="native", persistent_sessions=False),
        )
        self.assertFalse(native_only.resume)
        self.assertEqual(native_only.reason, "provider_has_no_resumable_session")

        mode_only = self._decide(
            print_timeout_result(),
            capabilities=make_capabilities(
                session_mode="ephemeral", persistent_sessions=True
            ),
        )
        self.assertFalse(mode_only.resume)
        self.assertEqual(mode_only.reason, "session_mode:ephemeral")

    def test_refuses_when_the_attempt_budget_is_exhausted(self):
        decision = self._decide(
            print_timeout_result(),
            task=make_task(_recovery_attempt=3),
        )
        self.assertFalse(decision.resume)
        self.assertEqual(decision.reason, "attempt_budget_exhausted")

        unlimited = recovery_decision(
            make_task(_recovery_attempt=9),
            print_timeout_result(),
            provider_capabilities=make_capabilities(),
            remaining_runtime_seconds=200.0,
            policy=RecoveryPolicy(max_attempts=10),
        )
        self.assertTrue(unlimited.resume)

    def test_refuses_without_enough_remaining_runtime(self):
        decision = self._decide(print_timeout_result(), remaining=14.9)
        self.assertFalse(decision.resume)
        self.assertEqual(decision.reason, "insufficient_remaining_runtime")

        enough = self._decide(print_timeout_result(), remaining=15.0)
        self.assertTrue(enough.resume)

    def test_supports_expanding_the_recoverable_reasons(self):
        policy = RecoveryPolicy(
            recoverable_termination_reasons=frozenset({"provider_idle_cutoff"})
        )
        decision = recovery_decision(
            make_task(),
            print_timeout_result(termination_reason="provider_idle_cutoff"),
            provider_capabilities=make_capabilities(),
            remaining_runtime_seconds=200.0,
            policy=policy,
        )
        self.assertTrue(decision.resume)
        self.assertEqual(decision.reason, "resumable_print_timeout")

        default = recovery_decision(
            make_task(),
            print_timeout_result(termination_reason="provider_idle_cutoff"),
            provider_capabilities=make_capabilities(),
            remaining_runtime_seconds=200.0,
        )
        self.assertFalse(default.resume)


class RegistryRecoveryTests(unittest.TestCase):
    def _registry(self, provider, *, run_store=None, agent_store=None):
        return ProviderRegistry(
            [provider],
            run_store=run_store,
            agent_store=agent_store,
        )

    def test_print_timeout_resumes_the_same_provider_session(self):
        provider = ScriptedSessionProvider(
            [print_timeout_result(), success_result(summary="done")]
        )
        registry = self._registry(provider)
        task = make_task(_logical_deadline=time.monotonic() + 300)

        result = registry._run_provider_turns(provider, task)

        self.assertEqual([call[:2] for call in provider.calls], [
            ("execute", None),
            ("continue", "provider-session-1"),
        ])
        resumed = provider.calls[1][2]
        self.assertEqual(resumed.metadata["_recovery_attempt"], 1)
        self.assertTrue(resumed.objective.startswith(RECOVERY_CONTINUATION_PROMPT))
        self.assertIn("ship the parser", resumed.objective)
        self.assertLessEqual(resumed.timeout_seconds, task.timeout_seconds)

        self.assertEqual(result.status, "success")
        self.assertTrue(result.metadata["recovered"])
        self.assertEqual(result.metadata["recovery_attempts"], 1)

    def test_recovery_attempts_stop_at_the_policy_limit(self):
        provider = ScriptedSessionProvider(
            [
                print_timeout_result(),
                print_timeout_result(),
                print_timeout_result(),
                success_result(summary="done"),
            ]
        )
        registry = self._registry(provider)
        task = make_task(_logical_deadline=time.monotonic() + 600)

        result = registry._run_provider_turns(provider, task)

        self.assertEqual(len(provider.calls), 4)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.metadata["recovery_attempts"], 3)

    def test_non_recoverable_timeouts_are_returned_untouched(self):
        provider = ScriptedSessionProvider(
            [print_timeout_result(termination_reason="execution_timeout")]
        )
        registry = self._registry(provider)
        task = make_task(_logical_deadline=time.monotonic() + 300)

        result = registry._run_provider_turns(provider, task)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.termination_reason, "execution_timeout")
        self.assertNotIn("recovery_attempts", result.metadata)

    def test_closed_end_providers_are_never_resumed(self):
        provider = ScriptedSessionProvider(
            [print_timeout_result()],
            session_mode="closed_end",
            persistent_sessions=False,
        )
        registry = self._registry(provider)
        task = make_task(_logical_deadline=time.monotonic() + 300)

        result = registry._run_provider_turns(provider, task)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.status, "timeout")
        self.assertNotIn("recovery_attempts", result.metadata)

    def test_recovery_stops_when_the_attempt_budget_is_spent(self):
        provider = ScriptedSessionProvider(
            [print_timeout_result(), success_result(summary="done")]
        )
        registry = self._registry(provider)
        task = make_task(
            _recovery_attempt=3,
            _logical_deadline=time.monotonic() + 300,
        )

        result = registry._run_provider_turns(provider, task)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.status, "timeout")
        self.assertNotIn("recovery_attempts", result.metadata)

    def test_recovery_stops_without_remaining_runtime(self):
        provider = ScriptedSessionProvider(
            [print_timeout_result(), success_result(summary="done")]
        )
        registry = self._registry(provider)
        task = make_task(_logical_deadline=time.monotonic() + 2)

        result = registry._run_provider_turns(provider, task)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.status, "timeout")

    def test_missing_session_identifier_refuses_resume_without_crashing(self):
        provider = ScriptedSessionProvider(
            [print_timeout_result(conversation_id=None)]
        )
        registry = self._registry(provider)
        task = make_task(_logical_deadline=time.monotonic() + 300)

        result = registry._run_provider_turns(provider, task)

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result.status, "timeout")
        self.assertIsNone(result.conversation_id)

    def test_continuation_turn_resumes_into_the_same_session(self):
        provider = ScriptedSessionProvider(
            [print_timeout_result(), success_result(summary="done")]
        )
        registry = self._registry(provider)
        task = make_task(_logical_deadline=time.monotonic() + 300)

        result = registry.continue_provider_session(
            provider, task, "provider-session-1"
        )

        self.assertEqual(
            [call[:2] for call in provider.calls],
            [
                ("continue", "provider-session-1"),
                ("continue", "provider-session-1"),
            ],
        )
        self.assertEqual(result.metadata["recovery_attempts"], 1)

    def test_bounded_provider_failures_still_propagate(self):
        class ExplodingProvider(ScriptedSessionProvider):
            def execute(self, task):
                self.calls.append(("execute", None, task))
                raise RuntimeError("provider crashed")

        provider = ExplodingProvider([])
        registry = self._registry(provider)
        task = make_task(_logical_deadline=time.monotonic() + 300)

        with self.assertRaises(RuntimeError):
            registry._run_provider_turns(provider, task)

    def test_recovery_is_recorded_on_the_agent_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_store = AgentStore(root / "agents")
            workspace = root / "workspace"
            workspace.mkdir()
            session = agent_store.create(
                provider="fake",
                workspace=workspace,
                task_id=None,
                model="model",
                reasoning="high",
                access="read_only",
                role="explorer",
                objective="inspect",
            )
            provider = ScriptedSessionProvider(
                [print_timeout_result(), success_result(summary="done")]
            )
            registry = self._registry(provider, agent_store=agent_store)
            task = make_task(
                _agent_session_id=session.session_id,
                _logical_deadline=time.monotonic() + 300,
            )

            registry._run_provider_turns(provider, task)

            stored = agent_store.get(session.session_id)
            self.assertFalse(stored.metadata["recovery_active"])
            self.assertEqual(stored.metadata["recovery_attempts"], 1)

            kinds = [
                event["kind"]
                for event in agent_store.events(session.session_id)["events"]
            ]
            self.assertIn("recovery", kinds)

    def test_workflow_execute_recovers_and_records_the_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_store = RunStore(root / "state")
            workspace = root / "workspace"
            workspace.mkdir()
            provider = ScriptedSessionProvider(
                [print_timeout_result(), success_result(summary="done")]
            )
            registry = ProviderRegistry(
                [provider],
                profiles=StaticResolver({}),
                run_store=run_store,
            )

            result = registry.execute(
                TaskSpec(
                    objective="ship the parser",
                    provider="fake",
                    workspace=workspace,
                    timeout_seconds=300,
                )
            )

            self.assertEqual(result.status, "success")
            self.assertTrue(result.metadata["recovered"])
            self.assertEqual(result.metadata["recovery_attempts"], 1)

            resumed = provider.calls[1][2]
            self.assertIn("_logical_deadline", resumed.metadata)
            self.assertEqual(resumed.metadata["_recovery_attempt"], 1)

            task_id = result.metadata["task_id"]
            record = run_store.get_task(workspace, task_id)
            self.assertIsNotNone(record)
            self.assertEqual(record["recovery_attempts"], 1)

            events = run_store.list_events(workspace, task_id)["events"]
            recoveries = [event for event in events if event.get("kind") == "recovery"]
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0]["recovery_attempt"], 1)

    def test_recovery_is_recorded_in_the_task_event_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_store = RunStore(root / "state")
            workspace = root / "workspace"
            workspace.mkdir()
            task_spec = make_task(
                _logical_deadline=time.monotonic() + 300,
            )
            task_spec = replace(task_spec, workspace=workspace)
            task_id = run_store.start_task(task_spec, task_id="task-recovery")
            provider = ScriptedSessionProvider(
                [print_timeout_result(), success_result(summary="done")]
            )
            registry = self._registry(provider, run_store=run_store)
            task = replace(
                task_spec,
                metadata={
                    **task_spec.metadata,
                    "_observability_workspace": str(task_spec.workspace),
                    "_task_id": task_id,
                },
            )

            registry._run_provider_turns(provider, task)

            record = run_store.get_task(workspace, task_id)
            self.assertIsNotNone(record)
            self.assertEqual(record["recovery_attempts"], 1)

            events = run_store.list_events(workspace, task_id)["events"]
            recoveries = [event for event in events if event.get("kind") == "recovery"]
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0]["reason"], "resumable_print_timeout")
            self.assertTrue(recoveries[0]["resume"])
            self.assertEqual(recoveries[0]["recovery_attempt"], 1)


if __name__ == "__main__":
    unittest.main()
