import tempfile
import unittest

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cortex_relay.core.agents import AgentEvent
from cortex_relay.runtime.agent_backend import AgentStoreBackend
from cortex_relay.runtime.agent_store import AgentStore


class AgentStoreTests(unittest.TestCase):
    def _session(self, store: AgentStore, workspace: Path):
        return store.create(
            provider="fake",
            workspace=workspace,
            task_id=None,
            model="model",
            reasoning="high",
            access="read_only",
            role="explorer",
            objective="inspect",
        )

    def test_sqlite_store_satisfies_backend_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStore(Path(tmp) / "state")
            self.assertIsInstance(store, AgentStoreBackend)

    def test_schema_version_and_integrity_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AgentStore(Path(tmp) / "state")
            self.assertEqual(store.schema_version(), 1)
            self.assertEqual(store.integrity_check(), ("ok",))
            health = store.health()
            self.assertTrue(health["ok"])
            self.assertEqual(health["schema_version"], 1)
            self.assertEqual(health["expected_schema_version"], 1)

    def test_session_events_messages_and_children_are_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AgentStore(root)
            parent = store.create(
                provider="fake",
                workspace=root,
                task_id="task-1",
                model="m1",
                reasoning="high",
                access="read_only",
                role="reviewer",
                objective="review",
            )
            store.bind_provider_session(parent.session_id, "provider-parent")
            store.record_event(
                AgentEvent(
                    session_id=parent.session_id,
                    kind="text_delta",
                    data={"text": "working"},
                    provider_event="delta",
                )
            )
            store.add_message(
                parent.session_id,
                direction="agent_to_host",
                content="done",
            )
            child = store.upsert_child(
                parent_session_id=parent.session_id,
                provider_session_id="provider-child",
                provider="fake",
                state="running",
                role="explorer",
                metadata={"log_uri": "file:///tmp/child.log"},
            )

            reloaded = AgentStore(root)
            self.assertEqual(
                reloaded.get(parent.session_id).provider_session_id,
                "provider-parent",
            )
            events = reloaded.events(parent.session_id)
            self.assertEqual(events["events"][0]["kind"], "text_delta")
            self.assertEqual(
                reloaded.messages(parent.session_id)[0]["content"],
                "done",
            )
            children = reloaded.children(parent.session_id)
            self.assertEqual(children[0]["session_id"], child.session_id)
            self.assertEqual(children[0]["provider_session_id"], "provider-child")
            self.assertEqual(children[0]["parent_session_id"], parent.session_id)
            self.assertEqual(children[0]["root_session_id"], parent.session_id)

    def test_concurrent_event_writers_get_atomic_monotonic_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            store = AgentStore(root / "state")
            session = self._session(store, workspace)

            def write(index: int) -> int:
                saved = store.record_event(
                    AgentEvent(
                        session_id=session.session_id,
                        kind="provider_event",
                        data={"index": index},
                        provider_event="test",
                    )
                )
                return int(saved["sequence"])

            with ThreadPoolExecutor(max_workers=8) as pool:
                sequences = list(pool.map(write, range(64)))

            self.assertEqual(sorted(sequences), list(range(1, 65)))
            page = store.events(session.session_id, after_sequence=32, limit=100)
            self.assertEqual(
                [item["sequence"] for item in page["events"]],
                list(range(33, 65)),
            )
            self.assertEqual(page["next_sequence"], 64)

    def test_messages_have_incremental_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            store = AgentStore(root / "state")
            session = self._session(store, workspace)

            first = store.add_message(
                session.session_id,
                direction="host_to_agent",
                content="one",
            )
            second = store.add_message(
                session.session_id,
                direction="agent_to_host",
                content="two",
            )

            self.assertEqual(first["sequence"], 1)
            self.assertEqual(second["sequence"], 2)
            page = store.message_page(
                session.session_id,
                after_sequence=1,
                limit=10,
            )
            self.assertEqual(len(page["messages"]), 1)
            self.assertEqual(page["messages"][0]["content"], "two")
            self.assertEqual(page["next_sequence"], 2)

    def test_provider_session_lookup_is_indexed_and_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            store = AgentStore(root / "state")
            first = self._session(store, workspace)
            second = self._session(store, workspace)

            store.bind_provider_session(first.session_id, "provider-1")
            found = store.find_by_provider_session("fake", "provider-1")
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.session_id, first.session_id)

            with self.assertRaises(ValueError):
                store.bind_provider_session(second.session_id, "provider-1")

    def test_lease_exclusion_heartbeat_and_expiry_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            store = AgentStore(root / "state")
            session = self._session(store, workspace)
            store.update(session.session_id, state="running")

            self.assertTrue(
                store.acquire_lease(
                    session.session_id,
                    "owner-a",
                    ttl_seconds=10,
                    owner_pid=123,
                )
            )
            self.assertFalse(
                store.acquire_lease(
                    session.session_id,
                    "owner-b",
                    ttl_seconds=10,
                    owner_pid=456,
                )
            )
            self.assertTrue(
                store.heartbeat(
                    session.session_id,
                    "owner-a",
                    ttl_seconds=10,
                )
            )
            lease = store.lease(session.session_id)
            self.assertIsNotNone(lease)
            assert lease is not None
            self.assertEqual(lease["owner_id"], "owner-a")

            expired = store.reconcile_expired(
                workspace=workspace,
                now=float(lease["expires_at"]) + 1,
            )
            self.assertEqual(expired, [session.session_id])
            reconciled = store.get(session.session_id)
            self.assertEqual(reconciled.state, "interrupted")
            self.assertIn("lease expired", reconciled.metadata["interrupted_reason"])
            events = store.events(session.session_id)["events"]
            self.assertEqual(events[-1]["provider_event"], "lease_expired")

    def test_gc_prunes_terminal_agent_tree_without_orphaning_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            store = AgentStore(root / "state")

            parent = self._session(store, workspace)
            child = store.upsert_child(
                parent_session_id=parent.session_id,
                provider_session_id="child-provider-session",
                provider="fake",
                state="running",
                role="explorer",
            )
            store.update(parent.session_id, state="idle")
            store.update(child.session_id, state="idle")
            active = self._session(store, workspace)

            preview = store.gc(
                workspace=workspace,
                retention_days=0,
                max_completed_roots=100,
                dry_run=True,
            )
            self.assertEqual(preview["count"], 1)
            self.assertEqual(
                set(preview["agents"][0]["sessions"]),
                {parent.session_id, child.session_id},
            )

            result = store.gc(
                workspace=workspace,
                retention_days=0,
                max_completed_roots=100,
            )
            self.assertEqual(result["count"], 1)
            with self.assertRaises(ValueError):
                store.get(parent.session_id)
            with self.assertRaises(ValueError):
                store.get(child.session_id)
            self.assertEqual(store.get(active.session_id).state, "starting")

    def test_workspace_filtering_uses_indexed_session_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            other = root / "other"
            workspace.mkdir()
            other.mkdir()
            store = AgentStore(root / "state")
            local = self._session(store, workspace)
            self._session(store, other)

            rows = store.list(workspace=workspace)
            self.assertEqual([row["session_id"] for row in rows], [local.session_id])


if __name__ == "__main__":
    unittest.main()
