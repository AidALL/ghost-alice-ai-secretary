import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from ai_secretary.authority_contracts import SourceSpec
from ai_secretary.coordinator import AuthorityRepository, LifecycleTx, RunCoordinator
from ai_secretary.ledger import Ledger


class OperatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from ai_secretary.operator import Operator
        from ai_secretary.collection import CollectionService
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ledger = Ledger(self.root / "operations.sqlite")
        self.ledger.create_task("task", "user", "collect operator fixture")
        async def gate(session):
            return {"decision": "allow", "admission_id": "a", "task_id": "task", "inputEventId": "e"}
        self.coordinator = RunCoordinator(self.ledger.store, gate)
        with self.ledger.store.transaction() as unit:
            AuthorityRepository(unit).record_admission("standing", "a", "task", "e")
        self.incoming = self.root / "incoming"
        self.incoming.mkdir()
        self.collector = CollectionService(self.ledger.store, self.root / "artifacts", self.coordinator)
        secretary = SimpleNamespace(ledger=self.ledger, coordinator=self.coordinator, collection=self.collector)
        self.operator = Operator(secretary, SimpleNamespace(check=gate))
        self.source = SourceSpec.create("source", 1, "local_files", {"root": str(self.incoming), "recipe": "local-snapshot"})

    async def test_configure_grant_collect_actual_file_and_revoke(self):
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time() + 60)
        (self.incoming / "invoice.txt").write_text("local operator invoice")
        result = await self.operator.collect(grant.grant_id)
        self.assertEqual(result.status, "Completed")
        self.assertEqual(len(result.artifacts), 1)
        with self.ledger.store.read() as db:
            self.assertFalse(db.execute("SELECT 1 FROM execution_lifecycle WHERE lifecycle_id LIKE 'operator-collection:%' AND status<>'Stopped'").fetchone())
        self.operator.revoke("source")
        with self.assertRaises(PermissionError): await self.operator.collect(grant.grant_id)

    async def test_expired_grant_is_not_persisted(self):
        self.operator.configure(self.source)
        with self.assertRaises(ValueError): await self.operator.grant("source", "standing", time.time() - 1)
        with self.ledger.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_grants").fetchone()[0], 0)

    async def test_grant_is_bound_to_current_admission_not_operator_arguments(self):
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time() + 60)
        self.assertEqual((grant.admission_id, grant.task_id, grant.input_event_id), ("a", "task", "e"))

    async def _assert_denied_collection_leaves_authority_unchanged(self, grant_id):
        tables = ("execution_lifecycle", "execution_contexts", "authority_runs", "source_guard")
        def snapshot():
            with self.ledger.store.read() as db:
                return {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
                        for table in tables}
        before = snapshot()
        with self.assertRaises(PermissionError):
            await self.operator.collect(grant_id)
        self.assertEqual(snapshot(), before, "denied collection must not strand an enabled lifecycle")

    async def test_revoked_grant_does_not_start_operator_lifecycle(self):
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time() + 60)
        with self.ledger.store.transaction() as unit:
            AuthorityRepository(unit).revoke_grant(grant.grant_id)
        await self._assert_denied_collection_leaves_authority_unchanged(grant.grant_id)

    async def test_expired_grant_does_not_start_operator_lifecycle(self):
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time() + 60)
        with patch("ai_secretary.operator.time.time", return_value=grant.expires_at + 1):
            await self._assert_denied_collection_leaves_authority_unchanged(grant.grant_id)

    async def test_changed_source_revision_does_not_start_operator_lifecycle(self):
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time() + 60)
        self.operator.configure(SourceSpec.create("source", 2, "local_files", self.source.scope))
        await self._assert_denied_collection_leaves_authority_unchanged(grant.grant_id)

    async def test_changed_local_admission_does_not_start_operator_lifecycle(self):
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time() + 60)
        with self.ledger.store.transaction() as unit:
            AuthorityRepository(unit).record_admission("standing", "next-admission", "task", "next-event")
        await self._assert_denied_collection_leaves_authority_unchanged(grant.grant_id)

    async def test_overlapping_run_does_not_start_another_operator_lifecycle(self):
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time() + 60)
        with self.ledger.store.transaction() as unit:
            epoch = LifecycleTx(unit).start("existing-owner")
        await self.coordinator.begin_granted(grant.grant_id, "existing-owner", epoch)
        await self._assert_denied_collection_leaves_authority_unchanged(grant.grant_id)

    async def test_blocked_fresh_gate_does_not_start_operator_lifecycle(self):
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time() + 60)
        async def blocked(session):
            with self.ledger.store.transaction() as unit:
                self.assertEqual(unit.connection.execute("SELECT 1").fetchone()[0], 1)
            return {"decision": "block"}
        self.coordinator.gate = blocked
        await self._assert_denied_collection_leaves_authority_unchanged(grant.grant_id)

    async def test_standing_text_uses_a_separate_genuinely_admitted_session(self):
        self.operator.configure(self.source)
        intakes = []
        def intake(session, admission, text, origin, **kwargs):
            intakes.append((session, text, origin))
            with self.ledger.store.transaction() as unit:
                AuthorityRepository(unit).record_admission(session, "a", "task", "e")
        self.operator.governance.intake = intake
        grant = await self.operator.grant("source", "standing", time.time()+60, text="Collect this source once daily")
        self.assertNotEqual(grant.session, "standing")
        self.assertTrue(grant.session.startswith("standing:source:"))
        self.assertEqual(intakes, [(grant.session, "Collect this source once daily", "external_user")])

    async def test_live_unknown_retains_original_reconciliation_authority(self):
        from ai_secretary.collection import CollectionResult
        self.operator.configure(self.source)
        grant = await self.operator.grant("source", "standing", time.time()+60)
        context_seen = []
        async def unresolved(context, source):
            context_seen.append(context)
            with self.ledger.store.transaction() as unit:
                unit.connection.execute("INSERT INTO authority_retrievals VALUES('retrieval','source','item',?,NULL,'Prepared')", (context.run_id,))
                unit.connection.execute("UPDATE source_guard SET unresolved_retrieval_id='retrieval' WHERE source_id='source'")
            return CollectionResult("Unknown", reason="file has not appeared")
        with patch.object(self.collector, "collect", unresolved):
            self.assertEqual((await self.operator.collect(grant.grant_id)).status, "Unknown")
        await self.coordinator.checkpoint(context_seen[0], "later-correlation")
        with self.ledger.store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM authority_runs").fetchone()[0], "Pending")
            self.assertEqual(db.execute("SELECT active_run_id FROM source_guard").fetchone()[0], context_seen[0].run_id)


if __name__ == "__main__": unittest.main()
