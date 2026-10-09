import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from ai_secretary.authority import AuthorityStore
from ai_secretary.authority_contracts import JobGrant, SourceSpec
from ai_secretary.ledger import LedgerTx


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from ai_secretary.coordinator import AuthorityRepository, LifecycleTx, RunCoordinator
        self.Repository, self.Lifecycle = AuthorityRepository, LifecycleTx
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AuthorityStore(self.root / "operations.sqlite")
        self.binding = {"decision": "allow", "task_id": "task", "inputEventId": "event", "admission_id": "admission"}
        self.gate_calls = []

        async def gate(session):
            self.gate_calls.append(session)
            # No coordinator transaction may span the external gate.
            with self.store.transaction() as uow:
                self.assertEqual(uow.connection.execute("SELECT 1").fetchone()[0], 1)
            return dict(self.binding)

        self.coordinator = RunCoordinator(self.store, gate)
        with self.store.transaction() as uow:
            self.Repository.initialize(uow)
            LedgerTx(uow).create_task("task", "user", "collect local files")
            self.Repository(uow).record_admission("session", "admission", "task", "event")
            self.source = SourceSpec.create("source", 1, "local_files", {"root": str(self.root / "incoming"), "recipe": "local-snapshot"})
            repo = self.Repository(uow)
            repo.configure_source(self.source)
            repo.create_grant(JobGrant("grant", "source", 1, "session", "admission", "task", "event", self.source.scope_digest, "local-snapshot", time.time() + 100))
            self.epoch = self.Lifecycle(uow).start("scheduler")

    async def begin(self):
        return await self.coordinator.begin_granted("grant", "scheduler", self.epoch)

    async def test_fresh_gate_and_atomic_local_validation(self):
        context = await self.begin()
        self.assertEqual(await self.coordinator.source(context), self.source)
        evidence = await self.coordinator.checkpoint(context, "publish")
        self.assertEqual(evidence.admission_id, "admission")
        self.assertGreaterEqual(len(self.gate_calls), 3)
        self.binding["inputEventId"] = "new-event"
        with self.assertRaises(PermissionError):
            await self.coordinator.checkpoint(context, "read")

    async def test_old_admission_in_same_task_cannot_authorize(self):
        self.binding["admission_id"] = "different-admission"
        with self.assertRaises(PermissionError):
            await self.begin()
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM execution_contexts").fetchone()[0], 0)

    async def test_foreground_does_not_require_collection_source_or_scheduler(self):
        foreground = await self.coordinator.begin_foreground("session")
        with self.store.transaction() as uow:
            self.Lifecycle(uow).disable("scheduler")
        await self.coordinator.checkpoint(foreground, "read")
        self.assertTrue(foreground.lifecycle_id.startswith("foreground:"))
        with self.assertRaises(PermissionError):
            await self.begin()

    async def test_concurrent_source_claims_have_one_winner(self):
        evidence = await self.coordinator.authorize_grant("grant")

        def claim():
            try:
                with self.store.transaction() as uow:
                    return self.coordinator.claim_in(uow, "grant", evidence, "scheduler", self.epoch, now=time.time())
            except PermissionError:
                return None

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sum(value is not None for value in results), 1)
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM authority_runs").fetchone()[0], 1)

    async def test_claim_rollback_does_not_leave_reservation(self):
        evidence = await self.coordinator.authorize_grant("grant")
        with self.assertRaisesRegex(RuntimeError, "after claim"):
            with self.store.transaction() as uow:
                self.coordinator.claim_in(uow, "grant", evidence, "scheduler", self.epoch, now=time.time())
                raise RuntimeError("after claim")
        context = await self.begin()
        self.assertEqual(context.run_fence, 1)

    async def test_revoke_invalidates_native_context_in_same_transaction(self):
        context = await self.begin()
        with self.store.transaction() as uow:
            self.Repository(uow).revoke_grant("grant")
            self.assertEqual(uow.connection.execute("SELECT state FROM execution_contexts WHERE run_id=?", (context.run_id,)).fetchone()[0], "Draining")
        with self.assertRaises(PermissionError):
            await self.coordinator.checkpoint(context, "publish")

    async def test_source_hazard_survives_revision_and_run_finish(self):
        context = await self.begin()
        with self.store.transaction() as uow:
            uow.connection.execute("UPDATE source_guard SET unresolved_retrieval_id='uncertain' WHERE source_id='source'")
        await self.coordinator.finish(context, "Unknown")
        changed = SourceSpec.create("source", 2, "local_files", self.source.scope)
        with self.store.transaction() as uow:
            self.Repository(uow).configure_source(changed)
            self.Repository(uow).create_grant(JobGrant("new-grant", "source", 2, "session", "admission", "task", "event", changed.scope_digest, "local-snapshot", time.time() + 100))
        with self.assertRaises(PermissionError):
            await self.coordinator.begin_granted("new-grant", "scheduler", self.epoch)

    async def test_alias_and_nested_root_are_rejected(self):
        for path in (self.source.scope["root"], str(Path(self.source.scope["root"]) / "nested")):
            with self.assertRaises(ValueError):
                with self.store.transaction() as uow:
                    self.Repository(uow).configure_source(SourceSpec.create("alias", 1, "local_files", {"root": path, "recipe": "local-snapshot"}))

    async def test_scope_cannot_silently_change_under_same_revision(self):
        with self.assertRaises(ValueError):
            with self.store.transaction() as uow:
                self.Repository(uow).configure_source(SourceSpec.create("source", 1, "local_files", {**self.source.scope, "max_items": 5}))

    async def test_forged_context_counters_fail(self):
        context = await self.begin()
        for field in ("run_fence", "epoch", "source_revision"):
            forged = replace(context, **{field: getattr(context, field) + 1})
            with self.assertRaises(PermissionError):
                await self.coordinator.checkpoint(forged, "read")

    async def test_completed_release_and_duplicate_finish(self):
        context = await self.begin()
        await self.coordinator.finish(context, "Completed")
        await self.coordinator.finish(context, "Completed")
        with self.store.read() as db:
            self.assertIsNone(db.execute("SELECT active_run_id FROM source_guard").fetchone()[0])
        next_context = await self.begin()
        self.assertEqual(next_context.run_fence, context.run_fence + 1)

    async def test_lifecycle_drain_cannot_be_restart_relabelled(self):
        context = await self.begin()
        with self.store.transaction() as uow:
            lifecycle = self.Lifecycle(uow)
            disabled = lifecycle.disable("scheduler")
            self.assertEqual(lifecycle.disable("scheduler"), disabled)
            with self.assertRaises(PermissionError):
                lifecycle.start("scheduler")
            with self.assertRaises(PermissionError):
                lifecycle.mark_stopped("scheduler", disabled)
        with self.assertRaises(PermissionError):
            await self.coordinator.checkpoint(context, "read")

    async def test_pending_accounting_cannot_reactivate_draining_context(self):
        context = await self.begin()
        with self.store.transaction() as uow:
            self.Lifecycle(uow).disable("scheduler")
        await self.coordinator.finish(context, "Pending")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM execution_contexts WHERE run_id=?", (context.run_id,)).fetchone()[0], "Draining")

    async def test_source_physical_identity_cannot_move_away_from_old_hazard(self):
        context = await self.begin()
        with self.store.transaction() as uow:
            uow.connection.execute("UPDATE source_guard SET unresolved_retrieval_id='old-download' WHERE source_id='source'")
        await self.coordinator.finish(context, "Unknown")
        with self.assertRaises(ValueError):
            with self.store.transaction() as uow:
                self.Repository(uow).configure_source(SourceSpec.create("source", 2, "local_files", {**self.source.scope, "root": str(self.root / "another")}))

    async def test_delayed_gate_cannot_create_context_after_new_admission(self):
        async def delayed(session):
            with self.store.transaction() as uow:
                self.Repository(uow).record_admission(session, "new-admission", "task", "later-input")
            return dict(self.binding)
        self.coordinator.gate = delayed
        with self.assertRaises(PermissionError):
            await self.coordinator.begin_foreground("session")

    async def test_delayed_gate_cannot_claim_grant_after_new_admission(self):
        evidence = await self.coordinator.authorize_grant("grant")
        with self.store.transaction() as uow:
            self.Repository(uow).record_admission("session", "new-admission", "task", "later-input")
        with self.assertRaises(PermissionError):
            with self.store.transaction() as uow:
                self.coordinator.claim_in(uow, "grant", evidence, "scheduler", self.epoch, now=time.time())

    async def test_foreground_creation_rolls_back_with_capability_registration(self):
        evidence = await self.coordinator.authorize_foreground("session")
        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            with self.store.transaction() as uow:
                self.coordinator.begin_foreground_in(uow, evidence)
                raise RuntimeError("registration failed")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM execution_contexts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM execution_lifecycle WHERE lifecycle_id LIKE 'foreground:%'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
