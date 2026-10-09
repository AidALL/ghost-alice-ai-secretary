import asyncio
from dataclasses import FrozenInstanceError
import hashlib
import importlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_secretary.authority import AuthorityStore
from ai_secretary.authority_contracts import JobGrant, SourceSpec
from ai_secretary.contracts import EffectSpec
from ai_secretary.coordinator import AuthorityRepository, LifecycleTx, RunCoordinator
from ai_secretary.desktop_owner import DesktopOwnership
from ai_secretary.jobs import JobRepository
from ai_secretary.ledger import LedgerTx


class WorkerSessionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AuthorityStore(self.root / "operations.sqlite")
        self.current = {"decision": "allow", "admission_id": "admission",
                        "task_id": "task", "inputEventId": "event"}
        self.gate_calls = []
        self.on_gate = None

        async def gate(session):
            self.gate_calls.append(session)
            # The external boundary must never inherit an open UnitOfWork.
            with self.store.transaction() as uow:
                self.assertEqual(uow.connection.execute("SELECT 1").fetchone()[0], 1)
            if self.on_gate:
                self.on_gate()
            return dict(self.current)

        self.coordinator = RunCoordinator(self.store, gate)
        with self.store.transaction() as uow:
            LedgerTx(uow).create_task("task", "operator", "bounded desktop task")
            AuthorityRepository(uow).record_admission("session", "admission", "task", "event")
        try:
            self.module = importlib.import_module("ai_secretary.worker_sessions")
        except ModuleNotFoundError:
            self.module = None

    def registry(self, **kwargs):
        self.assertIsNotNone(self.module, "durable worker capability registry must exist")
        return self.module.WorkerSessions(self.store, self.coordinator, **kwargs)

    async def open(self, **kwargs):
        return await self.registry().open("session", "trusted-host-transport", **kwargs)

    def row(self, query, params=()):
        with self.store.read() as db:
            return db.execute(query, params).fetchone()

    async def test_private_durable_binding_derives_context_and_stores_only_token_hash(self):
        binding = await self.open()
        self.assertEqual(binding.transport_id, "trusted-host-transport")
        self.assertEqual(binding.context.session, "session")
        self.assertEqual(binding.context.admission_id, "admission")
        self.assertGreater(binding.expires_at, time.time())
        self.assertNotIn(binding.token, repr(binding))
        with self.assertRaises(FrozenInstanceError):
            binding.transport_id = "rival"
        row = self.row("SELECT * FROM worker_sessions")
        self.assertEqual(row["token_hash"], hashlib.sha256(binding.token.encode()).hexdigest())
        self.assertNotIn(binding.token, tuple(row))
        resolved = await self.registry().resolve(binding.token, binding.transport_id, "session")
        self.assertEqual(resolved, binding.context)
        self.assertEqual(len(self.gate_calls), 2)

    async def test_host_session_and_token_mismatch_fail_before_external_gate(self):
        binding = await self.open()
        calls = len(self.gate_calls)
        for token, transport, session in (("invented", binding.transport_id, "session"),
                                           (binding.token, "rival", "session"),
                                           (binding.token, binding.transport_id, "other")):
            with self.assertRaises(PermissionError):
                await self.registry().resolve(token, transport, session)
            with self.assertRaises(PermissionError):
                await self.registry().close(token, transport, session)
        self.assertEqual(len(self.gate_calls), calls)
        self.assertEqual(self.row("SELECT state FROM worker_sessions")[0], "Active")
        self.assertEqual(self.row("SELECT status FROM execution_lifecycle WHERE lifecycle_id=?",
                                  (binding.context.lifecycle_id,))[0], "Running")

    async def test_expired_capability_cannot_resolve_but_can_be_closed(self):
        binding = await self.open()
        with self.store.transaction() as uow:
            uow.connection.execute("UPDATE worker_sessions SET expires_at=?", (time.time() - 1,))
        calls = len(self.gate_calls)
        with self.assertRaises(PermissionError):
            await self.registry().resolve(binding.token, binding.transport_id, "session")
        self.assertEqual(len(self.gate_calls), calls)
        result = await self.registry().close(binding.token, binding.transport_id, "session")
        self.assertEqual(result.state, "Stopped")

    async def test_invalid_ttl_has_no_gate_or_context_side_effect(self):
        registry = self.registry()
        for ttl in (0, -1, float("nan"), float("inf"), True, "3600"):
            with self.assertRaises(ValueError):
                await registry.open("session", "trusted-host-transport", ttl=ttl)
        self.assertEqual(self.gate_calls, [])
        self.assertEqual(self.row("SELECT count(*) FROM execution_contexts")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM worker_sessions")[0], 0)

    async def test_admission_change_after_gate_before_registration_leaves_no_orphan(self):
        registry = self.registry()
        original_token = self.module.secrets.token_urlsafe

        def raced_token(size):
            self.assertEqual(self.gate_calls, ["session"])
            with self.store.transaction() as uow:
                AuthorityRepository(uow).record_admission("session", "later", "task", "later-event")
            return original_token(size)

        with patch("ai_secretary.worker_sessions.secrets.token_urlsafe", side_effect=raced_token):
            with self.assertRaises(PermissionError):
                await registry.open("session", "trusted-host-transport")
        self.assertEqual(self.row("SELECT count(*) FROM worker_sessions")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM execution_contexts")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM execution_lifecycle WHERE enabled=1")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM execution_lifecycle WHERE lifecycle_id LIKE 'foreground:%'")[0], 0)

    async def test_capability_registration_insert_failure_rolls_back_context_and_lifecycle(self):
        registry = self.registry()
        with self.store.transaction() as uow:
            uow.connection.execute("""CREATE TRIGGER reject_worker_registration
                BEFORE INSERT ON worker_sessions BEGIN
                SELECT RAISE(ABORT,'registration failed'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "registration failed"):
            await registry.open("session", "trusted-host-transport")
        self.assertEqual(self.row("SELECT count(*) FROM worker_sessions")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM execution_contexts")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM execution_lifecycle WHERE enabled=1")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM execution_lifecycle WHERE lifecycle_id LIKE 'foreground:%'")[0], 0)

    async def test_fresh_gate_and_local_admission_both_reject_stale_capabilities(self):
        binding = await self.open()
        self.current["inputEventId"] = "later-event"
        with self.assertRaises(PermissionError):
            await self.registry().resolve(binding.token, binding.transport_id, "session")
        self.current["inputEventId"] = "event"
        with self.store.transaction() as uow:
            AuthorityRepository(uow).record_admission("session", "later", "task", "later-event")
        with self.assertRaises(PermissionError):
            await self.registry().resolve(binding.token, binding.transport_id, "session")

    async def test_capability_row_is_revalidated_after_asynchronous_gate(self):
        binding = await self.open()

        def revoke_during_gate():
            with self.store.transaction() as uow:
                uow.connection.execute("UPDATE worker_sessions SET state='Closed'")

        self.on_gate = revoke_during_gate
        with self.assertRaises(PermissionError):
            await self.registry().resolve(binding.token, binding.transport_id, "session")

    async def test_capability_expiry_is_rechecked_after_asynchronous_gate(self):
        binding = await self.open()

        def expire_during_gate():
            with self.store.transaction() as uow:
                uow.connection.execute("UPDATE worker_sessions SET expires_at=?", (time.time() - 1,))

        self.on_gate = expire_during_gate
        with self.assertRaises(PermissionError):
            await self.registry().resolve(binding.token, binding.transport_id, "session")

    async def test_close_revokes_and_disables_before_waiting_then_stops_idempotently(self):
        registry = self.registry()
        binding = await registry.open("session", "trusted-host-transport")
        owner = await DesktopOwnership(self.store.path).acquire()
        try:
            close = asyncio.create_task(registry.close(binding.token, binding.transport_id, "session"))
            await asyncio.sleep(.04)
            self.assertFalse(close.done())
            self.assertEqual(self.row("SELECT state FROM worker_sessions")[0], "Closed")
            lifecycle = self.row("SELECT * FROM execution_lifecycle WHERE lifecycle_id=?",
                                 (binding.context.lifecycle_id,))
            self.assertEqual((lifecycle["enabled"], lifecycle["status"]), (0, "Draining"))
            self.assertEqual(lifecycle["epoch"], binding.context.epoch + 1)
            with self.assertRaises(PermissionError):
                await registry.resolve(binding.token, binding.transport_id, "session")
            owner.validate()
        finally:
            owner.release()
        result = await close
        self.assertEqual(result.state, "Stopped")
        self.assertEqual(self.row("SELECT state FROM execution_contexts")[0], "Closed")
        self.assertEqual((await registry.close(binding.token, binding.transport_id, "session")).state,
                         "Stopped")
        self.assertEqual(self.row("SELECT epoch FROM execution_lifecycle WHERE lifecycle_id=?",
                                  (binding.context.lifecycle_id,))[0], binding.context.epoch + 1)
        replacement = await registry.open("session", "trusted-host-transport")
        self.assertNotEqual(replacement.token, binding.token)
        self.assertNotEqual(replacement.context.lifecycle_id, binding.context.lifecycle_id)

    async def test_drain_timeout_leaves_durable_draining_and_allows_safe_retry(self):
        registry = self.registry(drain_timeout=.03)
        binding = await registry.open("session", "trusted-host-transport")
        async with DesktopOwnership(self.store.path) as owner:
            result = await registry.close(binding.token, binding.transport_id, "session")
            self.assertEqual((result.state, result.disposition), ("Draining", "timeout"))
            self.assertEqual(self.row("SELECT state FROM worker_sessions")[0], "Closed")
            self.assertEqual(self.row("SELECT count(*) FROM lifecycle_drains")[0], 0)
            self.assertEqual(self.row("SELECT state FROM execution_contexts")[0], "Draining")
            owner.validate()
        self.assertEqual((await registry.close(binding.token, binding.transport_id, "session")).state,
                         "Stopped")

    async def test_cancelled_drain_wait_does_not_reactivate_capability(self):
        registry = self.registry()
        binding = await registry.open("session", "trusted-host-transport")
        async with DesktopOwnership(self.store.path):
            close = asyncio.create_task(registry.close(binding.token, binding.transport_id, "session"))
            await asyncio.sleep(.03)
            close.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await close
            self.assertEqual(self.row("SELECT state FROM worker_sessions")[0], "Closed")
            self.assertEqual(self.row("SELECT count(*) FROM lifecycle_drains")[0], 0)
        self.assertEqual((await registry.close(binding.token, binding.transport_id, "session")).state,
                         "Stopped")

    def applying(self, uow, context, operation_id):
        ledger = LedgerTx(uow)
        ledger.prepare(operation_id, EffectSpec("task", "click", {}, {"x": 1}))
        claim = ledger.claim(operation_id)
        uow.connection.execute("INSERT INTO execution_operations VALUES(?,?,?)",
                               (operation_id, context.run_id, claim["attempt_id"]))

    async def test_drain_accounts_generic_and_collection_actors_without_clearing_hazards(self):
        binding = await self.open()
        with self.store.transaction() as uow:
            repository = AuthorityRepository(uow)
            source = SourceSpec.create("source", 1, "local_files",
                                       {"root": str(self.root / "source"), "recipe": "snapshot"})
            repository.configure_source(source)
            repository.create_grant(JobGrant("grant", "source", 1, "session", "admission", "task",
                                            "event", source.scope_digest, "snapshot", time.time() + 60))
        child = await self.coordinator.begin_interactive(binding.context, "source")
        with self.store.transaction() as uow:
            self.applying(uow, binding.context, "generic")
            self.applying(uow, child, "download")
            uow.connection.execute("INSERT INTO authority_retrievals VALUES('retrieval','source','item',?,'download','Applying')",
                                   (child.run_id,))
            uow.connection.execute("UPDATE source_guard SET unresolved_retrieval_id='retrieval' WHERE source_id='source'")
        result = await self.registry().close(binding.token, binding.transport_id, "session")
        self.assertEqual(result.state, "Stopped")
        with self.store.read() as db:
            self.assertEqual({row[0] for row in db.execute("SELECT state FROM operations")}, {"Unknown"})
            self.assertEqual({row[0] for row in db.execute("SELECT state FROM execution_contexts")}, {"Unknown"})
            self.assertEqual(db.execute("SELECT state FROM authority_runs").fetchone()[0], "Unknown")
            self.assertEqual(db.execute("SELECT state FROM authority_retrievals").fetchone()[0], "Unknown")
            self.assertEqual(db.execute("SELECT unresolved_retrieval_id FROM source_guard").fetchone()[0], "retrieval")
            self.assertIsNone(db.execute("SELECT active_run_id FROM source_guard").fetchone()[0])
        await self.coordinator.finish(child, "Unknown")
        self.assertEqual(self.row("SELECT state FROM execution_contexts WHERE run_id=?", (child.run_id,))[0], "Unknown")
        with self.assertRaises(PermissionError):
            await self.coordinator.checkpoint(child, "retry")

    async def test_unresolved_upstream_projection_prevents_stopped_claim(self):
        binding = await self.open()
        with self.store.transaction() as uow:
            JobRepository.initialize(uow)
            repository = AuthorityRepository(uow)
            source = SourceSpec.create("source", 1, "local_files",
                                       {"root": str(self.root / "source"), "recipe": "snapshot"})
            repository.configure_source(source)
            repository.create_grant(JobGrant("grant", "source", 1, "session", "admission", "task",
                                            "event", source.scope_digest, "snapshot", time.time() + 60))
            uow.connection.execute("INSERT INTO scheduled_jobs VALUES('job',1,'grant','source',1,?,'{}',1,0,?,?,?,0)",
                                   (source.scope_digest, time.time() + 60, binding.context.lifecycle_id, binding.context.epoch))
            uow.connection.execute("INSERT INTO scheduled_occurrences VALUES('occurrence','job',1,?,?,?,'Scheduled','',NULL)",
                                   (binding.context.lifecycle_id, binding.context.epoch, time.time() + 5))
            uow.connection.execute("INSERT INTO scheduler_outbox VALUES('declaration','occurrence','Unknown',NULL,0,'')")
        result = await self.registry().close(binding.token, binding.transport_id, "session")
        self.assertEqual((result.state, result.disposition), ("Draining", "unresolved_projection"))
        self.assertEqual(self.row("SELECT count(*) FROM lifecycle_drains")[0], 0)
        with self.store.transaction() as uow:
            uow.connection.execute("UPDATE scheduler_outbox SET phase='Removed'")
        self.assertEqual((await self.registry().close(binding.token, binding.transport_id, "session")).state,
                         "Stopped")

    async def worker(self, binding, *, tools=frozenset({"secretary_status", "secretary_act", "secretary_draft"})):
        parent = self

        class SecretaryBoundary:
            calls = []

            async def call_bound(self, tool, args, context):
                await parent.coordinator.checkpoint(context, "worker-tool")
                self.calls.append((tool, args, context))
                return {"tool": tool, "args": args}

        secretary = SecretaryBoundary()
        return self.module.WorkerSession(secretary, binding, allowed_tools=tools), secretary

    async def test_worker_rejects_control_names_and_raw_authority_before_any_gate_or_storage(self):
        binding = await self.open()
        worker, secretary = await self.worker(binding, tools=frozenset({"secretary_status", "control.intake", "nativecommand"}))
        calls = len(self.gate_calls)
        for tool in ("control.intake", "control.approve", "control.configure_source", "control.create_job",
                     "nativecommand", "click", "secretary_invented"):
            with self.assertRaises(PermissionError):
                await worker.call(tool, {})
        reserved = ("session", "admission_id", "task_id", "input_event_id", "inputEventId", "lifecycle_id",
                    "epoch", "run_id", "run_fence", "desktop_fence", "grant_id", "job_id", "job_revision",
                    "occurrence_id", "owner", "ownership", "_authorization", "key_path", "backend", "kind",
                    "origin", "actor", "source_revision", "token", "worker_token", "transport_id")
        for field in reserved:
            with self.assertRaises(PermissionError):
                await worker.call("secretary_status", {field: "forged"})
        self.assertEqual(len(self.gate_calls), calls)
        self.assertEqual(secretary.calls, [])
        self.assertEqual(self.row("SELECT count(*) FROM operations")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM drafts")[0], 0)
        self.assertEqual(self.row("SELECT count(*) FROM collection_grants")[0], 0)

    async def test_worker_allows_signed_observation_fields_and_logical_ids_as_data(self):
        binding = await self.open()
        worker, secretary = await self.worker(binding)
        args = {"operation_id": "logical-operation", "source_id": "logical-source",
                "expected": {"backend": "builtin", "ownership": {"run_id": "signed-data"}, "token": "signed"},
                "content": {"kind": "draft", "owner": "recipient"}}
        result = await worker.call("secretary_act", args)
        self.assertEqual(result["args"], args)
        self.assertEqual(secretary.calls[0][2], binding.context)
        self.assertNotIn(binding.token, str(result))

    async def test_worker_snapshots_arguments_before_external_gate(self):
        binding = await self.open()
        worker, secretary = await self.worker(binding)
        args = {"content": {"body": "original"}}
        self.on_gate = lambda: args["content"].update(body="mutated")
        result = await worker.call("secretary_draft", args)
        self.assertEqual(result["args"]["content"]["body"], "original")

    async def test_cached_worker_refuses_expired_binding_before_delegate(self):
        binding = await self.open(ttl=.01)
        worker, secretary = await self.worker(binding)
        await asyncio.sleep(.015)
        calls = len(self.gate_calls)
        with self.assertRaises(PermissionError):
            await worker.call("secretary_status", {})
        self.assertEqual(secretary.calls, [])
        self.assertEqual(len(self.gate_calls), calls)


if __name__ == "__main__":
    unittest.main()
