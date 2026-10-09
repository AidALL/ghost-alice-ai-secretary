"""Verified collection uses actual files and the existing authority SQLite file."""

from dataclasses import replace
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from ai_secretary.authority import AuthorityStore
from ai_secretary.authority_contracts import GateEvidence, JobGrant, RunContext, SourceSpec
from ai_secretary.coordinator import AuthorityRepository, LifecycleTx, RunCoordinator
from ai_secretary.contracts import BindingConflict, EffectSpec, canonical_json
from ai_secretary.execution import DesktopExecutor, make_effect_spec
from ai_secretary.ledger import LedgerTx

try:
    from ai_secretary.collection import (
        CollectionRepository, CollectionService, FileCandidate, SourceItem,
    )
except ImportError:
    CollectionRepository = CollectionService = FileCandidate = SourceItem = None


class Gate:
    """External live gate is the only simulated authority boundary."""

    def __init__(self, store, source):
        self.store, self.spec = store, source
        self.revoke_on_publish = False
        self.reject_publish = False
        self.phases = []

    async def source(self, context):
        return self.spec

    async def checkpoint(self, context, phase):
        self.phases.append(phase)
        if phase == "collection_publish":
            if self.reject_publish:
                raise PermissionError("fresh gate closed")
            if self.revoke_on_publish:
                with self.store.transaction() as unit:
                    unit.connection.execute("UPDATE source_guard SET revoked=1")
        return GateEvidence(context.session, context.admission_id, context.task_id,
                            context.input_event_id, time.monotonic())

    def validate_local(self, unit, context, phase, *, now):
        row = unit.connection.execute("SELECT * FROM source_guard WHERE source_id=?",
                                      (context.source_id,)).fetchone()
        if row["revoked"] or row["current_revision"] != context.source_revision:
            raise PermissionError("local authority revoked")
        if row["active_run_id"] != context.run_id or row["run_fence"] != context.run_fence:
            raise PermissionError("stale run")


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(CollectionService, "The verified collection service is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "source"
        self.root.mkdir()
        self.blobs = Path(self.tmp.name).resolve() / "artifacts"
        self.store = AuthorityStore(Path(self.tmp.name) / "operations.sqlite")
        self.source = SourceSpec.create("source", 1, "local_files", {"root": str(self.root)})
        self.context = RunContext(run_id="run", session="session", admission_id="admission",
            task_id="task", input_event_id="input", lifecycle_id="scheduler", epoch=1,
            run_fence=1, grant_id="grant", source_id="source", source_revision=1)
        with self.store.transaction() as unit:
            LedgerTx(unit).create_task("task", "operator", "Collect configured source")
            unit.connection.execute("UPDATE execution_lifecycle SET epoch=1,enabled=1,status='Running'")
            unit.connection.execute("INSERT INTO source_guard VALUES('source',0,1,'run',1,NULL)")
            unit.connection.execute("INSERT INTO authority_sources VALUES(?,?,?,?,?)",
                (self.source.source_id, 1, self.source.kind, self.source.scope_json, self.source.scope_digest))
            unit.connection.execute("INSERT INTO collection_grants VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                ("grant", "source", 1, "session", "admission", "task", "input",
                 self.source.scope_digest, "local-snapshot", time.time() + 1000))
            unit.connection.execute("INSERT INTO execution_contexts VALUES(?,?,?,?,?,?,?,?,?,'Active')",
                ("run", "session", "admission", "task", "input", "scheduler", 1, 1, None))
            unit.connection.execute("INSERT INTO authority_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("run", "grant", "source", 1, None, None, None, "scheduler", 1, 1, "Active"))
            CollectionRepository.initialize(unit)
        self.gate = Gate(self.store, self.source)
        self.service = CollectionService(self.store, self.blobs, self.gate)

    def rows(self, table):
        with self.store.read() as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def desktop_source(self):
        self.source = SourceSpec.create("source", 1, "desktop_attachments", {
            "root": str(self.root), "app": "app", "account": "account", "conversation": "chat"})
        self.gate.spec = self.source
        with self.store.transaction() as unit:
            unit.connection.execute("UPDATE authority_sources SET kind=?,scope_json=?,scope_digest=?",
                (self.source.kind, self.source.scope_json, self.source.scope_digest))
            unit.connection.execute("UPDATE collection_grants SET scope_digest=?", (self.source.scope_digest,))
        return SourceItem("message-attachment", canonical_json({
            "app": "app", "account": "account", "conversation": "chat", "source_item": "message-attachment"}))

    def prepare_desktop(self, item, retrieval_id="retrieval", operation_id="operation"):
        with self.store.transaction() as unit:
            repo = CollectionRepository(unit)
            repo.prepare_retrieval(self.context, self.source, item, retrieval_id)
            ledger = LedgerTx(unit)
            ledger.prepare(operation_id, EffectSpec("task", "collection_download", item.provenance, {}))
            attempt = ledger.claim(operation_id)["attempt_id"]
            unit.connection.execute("INSERT INTO execution_operations VALUES(?,?,?)",
                                   (operation_id, "run", attempt))
            repo.bind_operation(self.context, self.source, retrieval_id, operation_id)
        return FileCandidate(self.root / "saved.bin", item.item_id, canonical_json({
            **item.provenance, "run_id": "run", "retrieval_id": retrieval_id,
            "operation_id": operation_id}), source_complete=True, correlated=True)

    async def test_stable_snapshot_publishes_exact_bytes_and_folder_provenance(self):
        data = b"received content is data\x00\xff"
        (self.root / "report.bin").write_bytes(data)
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Completed")
        self.assertEqual(len(result.artifacts), 1)
        artifact = self.rows("collection_artifacts")[0]
        self.assertEqual(artifact["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual((self.blobs / artifact["sha256"]).read_bytes(), data)
        self.assertEqual(json.loads(artifact["provenance_json"])["kind"], "local_files")
        self.assertEqual(json.loads(artifact["file_metadata_json"])["relative_path"], "report.bin")
        self.assertEqual(len(self.rows("collection_cursors")), 1)

    async def test_replay_keeps_original_provenance_and_cursor(self):
        (self.root / "file").write_bytes(b"same")
        first = await self.service.collect(self.context, self.source)
        second = await self.service.collect(self.context, self.source)
        self.assertEqual(first.artifacts, second.artifacts)
        self.assertEqual(len(self.rows("collection_artifacts")), 1)
        self.assertEqual(len(self.rows("collection_cursors")), 1)

    async def test_identical_bytes_in_distinct_source_items_have_distinct_artifacts(self):
        (self.root / "a").write_bytes(b"same")
        (self.root / "b").write_bytes(b"same")
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Completed")
        rows = self.rows("collection_artifacts")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["item_id"], rows[1]["item_id"])
        self.assertEqual(rows[0]["sha256"], rows[1]["sha256"])
        self.assertEqual(len(list(self.blobs.iterdir())), 1)

    async def test_same_filename_with_changed_bytes_gets_new_provenance(self):
        target = self.root / "same-name"
        target.write_bytes(b"first")
        await self.service.collect(self.context, self.source)
        target.write_bytes(b"other")
        await self.service.collect(self.context, self.source)
        rows = self.rows("collection_artifacts")
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["item_id"], rows[1]["item_id"])

    async def test_qualifying_partial_file_keeps_run_pending(self):
        (self.root / "a").write_bytes(b"ready")
        (self.root / "z.part").write_bytes(b"paused")
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(len(self.rows("collection_artifacts")), 1)
        self.assertEqual(len(self.rows("collection_cursors")), 1)

    async def test_empty_qualifying_set_completes_explicit_snapshot(self):
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Completed")
        self.assertEqual(result.artifacts, ())

    async def test_symlink_candidate_is_rejected_without_reading_target(self):
        outside = Path(self.tmp.name) / "outside"
        outside.write_bytes(b"private")
        (self.root / "link").symlink_to(outside)
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_artifacts"), [])

    async def test_traversal_candidate_is_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        outside.write_bytes(b"private")
        candidate = FileCandidate(self.root / ".." / "outside", "item", "{}")
        result = await self.service.ingest(self.context, self.source, "retrieval", candidate)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_artifacts"), [])

    async def test_file_mutation_during_actual_copy_is_not_published(self):
        target = self.root / "file"
        target.write_bytes(b"original")
        actual_read = os.read
        changed = False

        def mutate_after_read(fd, size):
            nonlocal changed
            data = actual_read(fd, size)
            if data and not changed:
                changed = True
                target.write_bytes(b"changed-longer")
            return data

        with patch("ai_secretary.collection.os.read", mutate_after_read):
            result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_artifacts"), [])
        self.assertEqual(self.rows("collection_cursors"), [])

    async def test_fresh_gate_failure_after_file_publication_commits_no_metadata(self):
        (self.root / "file").write_bytes(b"stable")
        self.gate.reject_publish = True
        with self.assertRaisesRegex(PermissionError, "fresh gate closed"):
            await self.service.collect(self.context, self.source)
        self.assertEqual(len(list(self.blobs.iterdir())), 1)
        for table in ("collection_artifacts", "collection_cursors", "collection_results"):
            self.assertEqual(self.rows(table), [])

    async def test_revocation_after_copy_prevents_database_publication(self):
        (self.root / "file").write_bytes(b"stable")
        self.gate.revoke_on_publish = True
        with self.assertRaisesRegex(PermissionError, "revoked"):
            await self.service.collect(self.context, self.source)
        self.assertEqual(len(list(self.blobs.iterdir())), 1)
        for table in ("collection_artifacts", "collection_cursors", "collection_results"):
            self.assertEqual(self.rows(table), [])

    async def test_cursor_order_includes_only_committed_verified_items(self):
        (self.root / "z").write_bytes(b"z")
        (self.root / "a.part").write_bytes(b"pending")
        (self.root / "b").write_bytes(b"b")
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        cursors = self.rows("collection_cursors")
        self.assertEqual([row["ordinal"] for row in cursors], [1, 2])
        artifacts = {row["artifact_id"]: row for row in self.rows("collection_artifacts")}
        self.assertEqual([json.loads(artifacts[row["artifact_id"]]["file_metadata_json"])["relative_path"]
                          for row in cursors], ["b", "z"])

    async def test_bounds_and_glob_are_enforced_before_publication(self):
        (self.root / "ignored.txt").write_bytes(b"ignored")
        (self.root / "large.bin").write_bytes(b"12345")
        self.source = SourceSpec.create("source", 1, "local_files", {
            "root": str(self.root), "filename_glob": "*.bin", "max_bytes": 4, "max_items": 1})
        self.gate.spec = self.source
        with self.store.transaction() as unit:
            unit.connection.execute("UPDATE authority_sources SET scope_json=?,scope_digest=?",
                (self.source.scope_json, self.source.scope_digest))
            unit.connection.execute("UPDATE collection_grants SET scope_digest=?", (self.source.scope_digest,))
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_artifacts"), [])

    async def test_desktop_requires_bound_operation_completion_and_correlation(self):
        item = self.desktop_source()
        candidate = self.prepare_desktop(item)
        candidate.path.write_bytes(b"download")
        for incomplete in (replace(candidate, source_complete=False), replace(candidate, correlated=False)):
            result = await self.service.ingest(self.context, self.source, "retrieval", incomplete)
            self.assertIn(result.status, ("Pending", "Unknown"))
            self.assertEqual(self.rows("collection_artifacts"), [])
            self.assertEqual(self.rows("source_guard")[0]["unresolved_retrieval_id"], "retrieval")
        result = await self.service.ingest(self.context, self.source, "retrieval", candidate)
        self.assertEqual(result.status, "Completed")
        self.assertEqual(self.rows("authority_retrievals")[0]["state"], "Verified")
        self.assertIsNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])

    async def test_unbound_desktop_file_and_mismatched_provenance_never_verify(self):
        item = self.desktop_source()
        path = self.root / "saved.bin"
        path.write_bytes(b"old same-name file")
        unbound = FileCandidate(path, item.item_id, item.provenance_json, True, True)
        result = await self.service.ingest(self.context, self.source, "absent", unbound)
        self.assertEqual(result.status, "Unknown")
        candidate = self.prepare_desktop(item)
        altered = replace(candidate, provenance_json=canonical_json({**candidate.provenance, "account": "other"}))
        result = await self.service.ingest(self.context, self.source, "retrieval", altered)
        self.assertEqual(result.status, "Unknown")
        self.assertEqual(self.rows("collection_artifacts"), [])

    async def test_source_hazard_blocks_new_item_after_source_revision(self):
        item = self.desktop_source()
        self.prepare_desktop(item)
        revised = SourceSpec.create("source", 2, "desktop_attachments", self.source.scope)
        with self.store.transaction() as unit:
            unit.connection.execute("UPDATE source_guard SET current_revision=2")
            unit.connection.execute("INSERT INTO authority_sources VALUES(?,?,?,?,?)",
                ("source", 2, revised.kind, revised.scope_json, revised.scope_digest))
            with self.assertRaisesRegex(BindingConflict, "unresolved"):
                CollectionRepository(unit).prepare_retrieval(
                    replace(self.context, source_revision=2), revised,
                    SourceItem("new-item", item.provenance_json), "new-retrieval")
        self.assertEqual(len(self.rows("authority_retrievals")), 1)

    async def test_exact_confirmed_no_effect_clears_hazard_without_artifact(self):
        item = self.desktop_source()
        self.prepare_desktop(item)
        with self.store.transaction() as unit:
            repo = CollectionRepository(unit)
            with self.assertRaises(BindingConflict):
                repo.confirm_no_effect(self.context, self.source, "retrieval", "wrong-operation", {"no_effect": True})
            repo.confirm_no_effect(self.context, self.source, "retrieval", "operation", {"no_effect": True})
        self.assertEqual(self.rows("authority_retrievals")[0]["state"], "NoEffect")
        self.assertIsNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])
        self.assertEqual(self.rows("collection_artifacts"), [])

    async def test_correlated_desktop_file_replacement_cannot_publish(self):
        from ai_secretary.collection import _identity
        item = self.desktop_source()
        candidate = self.prepare_desktop(item)
        candidate.path.write_bytes(b"observed download")
        identity = canonical_json(_identity(candidate.path.stat()))
        bound = FileCandidate(candidate.path, candidate.item_id, candidate.provenance_json, True, True,
                              observed_identity_json=identity)
        candidate.path.unlink()
        candidate.path.write_bytes(b"uncorrelated replacement")
        result = await self.service.ingest(self.context, self.source, "retrieval", bound)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_artifacts"), [])
        self.assertEqual(self.rows("source_guard")[0]["unresolved_retrieval_id"], "retrieval")

    async def test_exact_no_effect_candidate_returns_pending_without_uncertainty(self):
        item = self.desktop_source()
        candidate = self.prepare_desktop(item)
        with self.store.transaction() as unit:
            CollectionRepository(unit).confirm_no_effect(self.context, self.source, "retrieval", "operation", {"no_effect": True})
        result = await self.service.ingest(self.context, self.source, "retrieval", candidate)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("source_guard")[0]["unresolved_retrieval_id"], None)

    async def test_local_ingest_derives_snapshot_identity_instead_of_trusting_caller(self):
        target = self.root / "file"
        target.write_bytes(b"one")
        claimed = FileCandidate(target, "caller-item", canonical_json({"kind": "desktop_attachments"}))
        first = await self.service.ingest(self.context, self.source, "caller-retrieval", claimed)
        target.write_bytes(b"two")
        second = await self.service.ingest(self.context, self.source, "caller-retrieval", claimed)
        self.assertEqual(first.status, "Completed")
        self.assertEqual(second.status, "Completed")
        self.assertEqual(len(self.rows("collection_artifacts")), 2)
        for row in self.rows("collection_artifacts"):
            self.assertNotEqual(row["item_id"], "caller-item")
            self.assertEqual(json.loads(row["provenance_json"])["kind"], "local_files")

    async def test_descriptor_reads_and_gate_awaits_are_outside_unit(self):
        (self.root / "file").write_bytes(b"one")
        entered = False
        actual_transaction = self.store.transaction
        actual_read = os.read
        actual_gate = self.gate.checkpoint

        @contextmanager
        def transaction():
            nonlocal entered
            with actual_transaction() as unit:
                entered = True
                try:
                    yield unit
                finally:
                    entered = False

        def read(fd, size):
            self.assertFalse(entered, "File IO happened inside a SQLite unit")
            return actual_read(fd, size)

        async def checkpoint(context, phase):
            self.assertFalse(entered, "External gate awaited inside a SQLite unit")
            return await actual_gate(context, phase)

        with patch.object(self.store, "transaction", transaction), \
                patch("ai_secretary.collection.os.read", read), \
                patch.object(self.gate, "checkpoint", checkpoint):
            result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Completed")

    async def test_ignored_entry_traversal_is_bounded(self):
        for number in range(100):
            (self.root / f"{number:03}.ignore").write_bytes(b"ignored")
        self.source = SourceSpec.create("source", 1, "local_files", {
            "root": str(self.root), "filename_glob": "*.bin", "max_items": 1})
        self.gate.spec = self.source
        with self.store.transaction() as unit:
            unit.connection.execute("UPDATE authority_sources SET scope_json=?,scope_digest=?",
                (self.source.scope_json, self.source.scope_digest))
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_artifacts"), [])

    async def test_wrong_desktop_source_item_is_rejected_before_retrieve(self):
        item = self.desktop_source()
        wrong = SourceItem(item.item_id, canonical_json({**item.provenance, "account": "other"}))

        class Desktop:
            async def enumerate(port, context, source):
                return [wrong]

            async def retrieve(port, *args):
                self.fail("Wrong source item reached native retrieval")

        service = CollectionService(self.store, self.blobs, self.gate, Desktop())
        result = await service.collect(self.context, self.source)
        self.assertEqual(result.status, "Unknown")
        self.assertEqual(self.rows("authority_retrievals"), [])

    async def test_desktop_collect_port_binds_operation_before_returning_candidate(self):
        item = self.desktop_source()
        candidate_path = self.root / "saved.bin"

        class Desktop:
            async def enumerate(port, context, source):
                return [item]

            async def retrieve(port, context, source, retrieval_id, observed):
                with self.store.transaction() as unit:
                    ledger = LedgerTx(unit)
                    ledger.prepare("download", EffectSpec("task", "collection_download", observed.provenance, {}))
                    attempt = ledger.claim("download")["attempt_id"]
                    unit.connection.execute("INSERT INTO execution_operations VALUES('download','run',?)", (attempt,))
                    CollectionRepository(unit).bind_operation(context, source, retrieval_id, "download")
                candidate_path.write_bytes(b"retrieved")
                return FileCandidate(candidate_path, observed.item_id, canonical_json({**observed.provenance,
                    "run_id": context.run_id, "retrieval_id": retrieval_id, "operation_id": "download"}), True, True)

        service = CollectionService(self.store, self.blobs, self.gate, Desktop())
        result = await service.collect(self.context, self.source)
        self.assertEqual(result.status, "Completed")
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual(self.rows("operations")[0]["state"], "Applied")
        self.assertIsNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])

    async def test_ingest_verified_desktop_replay_returns_original_artifact(self):
        item = self.desktop_source()
        candidate = self.prepare_desktop(item)
        candidate.path.write_bytes(b"retrieved")
        first = await self.service.ingest(self.context, self.source, "retrieval", candidate)
        second = await self.service.ingest(self.context, self.source, "retrieval", candidate)
        self.assertEqual(second.status, "Completed")
        self.assertEqual(first.artifacts, second.artifacts)
        self.assertEqual(len(self.rows("collection_cursors")), 1)

    async def test_prepared_without_dispatch_can_be_resolved_as_no_effect(self):
        item = self.desktop_source()
        with self.store.transaction() as unit:
            repo = CollectionRepository(unit)
            repo.prepare_retrieval(self.context, self.source, item, "never-dispatched")
            try:
                repo.confirm_no_effect(self.context, self.source, "never-dispatched", None,
                                       {"no_effect": True, "reason": "native dispatch was never accepted"})
            except BindingConflict:
                self.fail("A durable Prepared retrieval with no operation must allow exact no-effect resolution")
        self.assertEqual(self.rows("authority_retrievals")[0]["state"], "NoEffect")
        self.assertIsNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])

    async def test_existing_nonregular_content_address_cannot_be_adopted(self):
        data = b"real file"
        (self.root / "file").write_bytes(data)
        address = hashlib.sha256(data).hexdigest()
        (self.blobs / address).symlink_to(self.root / "file")
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_artifacts"), [])

    async def test_additive_metadata_initialization_preserves_existing_records(self):
        item = self.desktop_source()
        self.prepare_desktop(item)
        before = {table: self.rows(table) for table in ("operations", "authority_retrievals", "source_guard")}
        with self.store.transaction() as unit:
            CollectionRepository.initialize(unit)
            CollectionRepository.initialize(unit)
        after = {table: self.rows(table) for table in before}
        self.assertEqual(before, after)

    async def test_desktop_collection_respects_total_run_byte_budget(self):
        first = self.desktop_source()
        self.source = SourceSpec.create("source", 1, "desktop_attachments", {**self.source.scope, "max_bytes": 5})
        self.gate.spec = self.source
        with self.store.transaction() as unit:
            unit.connection.execute("UPDATE authority_sources SET scope_json=?,scope_digest=?",
                (self.source.scope_json, self.source.scope_digest))
        second = SourceItem("second", canonical_json({**first.provenance, "source_item": "second"}))

        class Desktop:
            async def enumerate(port, context, source):
                return [first, second]

            async def retrieve(port, context, source, retrieval_id, item):
                with self.store.transaction() as unit:
                    ledger = LedgerTx(unit)
                    ledger.prepare(item.item_id, EffectSpec("task", "collection_download", item.provenance, {}))
                    attempt = ledger.claim(item.item_id)["attempt_id"]
                    unit.connection.execute("INSERT INTO execution_operations VALUES(?,?,?)", (item.item_id, "run", attempt))
                    CollectionRepository(unit).bind_operation(context, source, retrieval_id, item.item_id)
                target = self.root / item.item_id
                target.write_bytes(b"123")
                return FileCandidate(target, item.item_id, canonical_json({**item.provenance, "run_id": "run",
                    "retrieval_id": retrieval_id, "operation_id": item.item_id}), True, True)

        service = CollectionService(self.store, self.blobs, self.gate, Desktop())
        result = await service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(sum(row["size"] for row in self.rows("collection_artifacts")), 3)
        self.assertIsNotNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])

    async def test_confirmed_no_effect_reuses_original_retrieval_and_archives_binding(self):
        item = self.desktop_source()
        self.prepare_desktop(item)
        next_context = replace(self.context, run_id="run2", run_fence=2)
        with self.store.transaction() as unit:
            repo = CollectionRepository(unit)
            repo.confirm_no_effect(self.context, self.source, "retrieval", "operation", {"no_effect": True})
            unit.connection.execute("INSERT INTO execution_contexts SELECT 'run2',session,admission_id,task_id,input_event_id,lifecycle_id,epoch,2,desktop_fence,state FROM execution_contexts WHERE run_id='run'")
            unit.connection.execute("INSERT INTO authority_runs SELECT 'run2',grant_id,source_id,source_revision,job_id,job_revision,occurrence_id,lifecycle_id,epoch,2,state FROM authority_runs WHERE run_id='run'")
            unit.connection.execute("UPDATE source_guard SET active_run_id='run2',run_fence=2")
            try:
                prepared = repo.prepare_retrieval(next_context, self.source, item, "new-random-id")
            except BindingConflict:
                self.fail("Confirmed NoEffect must permit retry with the original durable retrieval identity")
            self.assertEqual(prepared["retrieval_id"], "retrieval")
            self.assertEqual(prepared["run_id"], "run2")
            self.assertEqual(prepared["state"], "Prepared")
            ledger = LedgerTx(unit)
            ledger.prepare("second-operation", EffectSpec("task", "collection_download", item.provenance, {}))
            attempt = ledger.claim("second-operation")["attempt_id"]
            unit.connection.execute("INSERT INTO execution_operations VALUES('second-operation','run2',?)", (attempt,))
            repo.bind_operation(next_context, self.source, "retrieval", "second-operation")
        self.assertEqual(self.rows("operations")[0]["operation_id"], "operation")
        self.assertEqual(self.rows("operations")[0]["state"], "Failed")
        history = self.rows("collection_retrieval_history")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["run_id"], "run")
        self.assertEqual(history[0]["operation_id"], "operation")
        self.assertEqual(history[0]["retrieval_id"], "retrieval")

    async def test_cached_desktop_item_with_changed_provenance_is_not_replayed(self):
        item = self.desktop_source()
        candidate = self.prepare_desktop(item)
        candidate.path.write_bytes(b"retrieved")
        await self.service.ingest(self.context, self.source, "retrieval", candidate)
        altered = SourceItem(item.item_id, canonical_json({**item.provenance, "size_hint": 999}))

        class Desktop:
            async def enumerate(port, context, source):
                return [altered]

            async def retrieve(port, *args):
                self.fail("Changed source-item provenance must not redownload as a new identity")

        service = CollectionService(self.store, self.blobs, self.gate, Desktop())
        result = await service.collect(self.context, self.source)
        self.assertEqual(result.status, "Unknown")
        self.assertEqual(result.artifacts, ())

    async def test_exact_failed_operation_proof_allows_no_effect_resolution(self):
        item = self.desktop_source()
        self.prepare_desktop(item)
        proof = {"no_effect": True, "effect_status": "not_started", "ok": False}
        with self.store.transaction() as unit:
            repo = CollectionRepository(unit)
            operation = unit.connection.execute("SELECT * FROM operations").fetchone()
            LedgerTx(unit).finish("operation", operation["attempt_id"], "Failed", proof)
            with self.assertRaises(BindingConflict):
                repo.confirm_no_effect(self.context, self.source, "retrieval", "operation", {"no_effect": True})
            try:
                repo.confirm_no_effect(self.context, self.source, "retrieval", "operation", proof)
            except BindingConflict:
                self.fail("Exact persisted Failed/no_effect receipt must resolve the matching retrieval")
        self.assertEqual(self.rows("authority_retrievals")[0]["state"], "NoEffect")
        self.assertIsNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])

    async def test_result_identifier_cannot_be_reassigned_to_another_run(self):
        from ai_secretary.collection import CollectionResult
        next_context = replace(self.context, run_id="run2")
        with self.store.transaction() as unit:
            unit.connection.execute("INSERT INTO execution_contexts SELECT 'run2',session,admission_id,task_id,input_event_id,lifecycle_id,epoch,run_fence,desktop_fence,state FROM execution_contexts WHERE run_id='run'")
            unit.connection.execute("INSERT INTO authority_runs SELECT 'run2',grant_id,source_id,source_revision,job_id,job_revision,occurrence_id,lifecycle_id,epoch,run_fence,state FROM authority_runs WHERE run_id='run'")
            repo = CollectionRepository(unit)
            repo.record_result(self.context, "fixed-result", CollectionResult("Pending"), now=time.time())
            with self.assertRaisesRegex(BindingConflict, "another run"):
                repo.record_result(next_context, "fixed-result", CollectionResult("Completed"), now=time.time())

    def limits(self, **values):
        self.source = SourceSpec.create("source", 1, "local_files", {**self.source.scope, **values})
        self.gate.spec = self.source
        with self.store.transaction() as unit:
            unit.connection.execute("UPDATE authority_sources SET scope_json=?,scope_digest=?",
                (self.source.scope_json, self.source.scope_digest))
            unit.connection.execute("UPDATE collection_grants SET scope_digest=?", (self.source.scope_digest,))

    async def test_local_pending_continuation_charges_earlier_artifacts_after_file_disappears(self):
        self.limits(max_bytes=1000)
        (self.root / "a").write_bytes(b"a" * 700)
        (self.root / "z.part").write_bytes(b"unfinished")
        self.assertEqual((await self.service.collect(self.context, self.source)).status, "Pending")
        (self.root / "a").unlink()
        (self.root / "z.part").unlink()
        (self.root / "b").write_bytes(b"b" * 700)
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(sum(row["size"] for row in self.rows("collection_artifacts")), 700)
        self.assertEqual(sum(item.size for item in result.artifacts), 700)

    async def test_local_pending_continuation_keeps_original_item_limit(self):
        self.limits(max_items=1)
        (self.root / "a").write_bytes(b"a")
        (self.root / "z.part").write_bytes(b"unfinished")
        self.assertEqual((await self.service.collect(self.context, self.source)).status, "Pending")
        (self.root / "a").unlink()
        (self.root / "z.part").unlink()
        (self.root / "b").write_bytes(b"b")
        result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(len(self.rows("collection_artifacts")), 1)

    async def test_unchanged_local_artifact_is_reused_without_another_copy_or_quota_charge(self):
        self.limits(max_bytes=1000)
        (self.root / "a").write_bytes(b"a" * 700)
        (self.root / "z.part").write_bytes(b"unfinished")
        self.assertEqual((await self.service.collect(self.context, self.source)).status, "Pending")
        (self.root / "z.part").unlink()
        (self.root / "b").write_bytes(b"b" * 300)
        copied = []
        actual = self.service._snapshot

        def snapshot(root, scope, path):
            copied.append(path.name)
            return actual(root, scope, path)

        with patch.object(self.service, "_snapshot", snapshot):
            result = await self.service.collect(self.context, self.source)
        self.assertEqual(result.status, "Completed")
        self.assertEqual(copied, ["b"])
        self.assertEqual(sum(item.size for item in result.artifacts), 1000)
        self.assertEqual(len(self.rows("collection_cursors")), 2)

    async def test_final_publication_unit_rechecks_quota_after_both_copies(self):
        self.limits(max_bytes=1000)
        (self.root / "a").write_bytes(b"a" * 700)
        (self.root / "b").write_bytes(b"b" * 700)
        barrier = asyncio.Barrier(2)
        actual = self.gate.checkpoint

        async def checkpoint(context, phase):
            if phase == "collection_publish":
                await barrier.wait()
            return await actual(context, phase)

        with patch.object(self.gate, "checkpoint", checkpoint):
            results = await asyncio.gather(*[
                self.service.ingest(self.context, self.source, name, FileCandidate(self.root / name, name, "{}"))
                for name in ("a", "b")])
        self.assertEqual(sorted(result.status for result in results), ["Completed", "Pending"])
        self.assertEqual(sum(row["size"] for row in self.rows("collection_artifacts")), 700)
        self.assertEqual(len(self.rows("collection_cursors")), 1)
        self.assertEqual(len(list(self.blobs.iterdir())), 2, "Rejected publication may leave an unreferenced CAS blob")

    async def test_local_ingest_replay_at_full_quota_reuses_unchanged_snapshot(self):
        self.limits(max_items=1, max_bytes=3)
        target = self.root / "a"
        target.write_bytes(b"abc")
        candidate = FileCandidate(target, "caller-item", "{}")
        first = await self.service.ingest(self.context, self.source, "caller-retrieval", candidate)
        self.assertEqual(first.status, "Completed")
        actual = self.service._snapshot
        copied = []

        def snapshot(root, scope, path):
            copied.append(path.name)
            return actual(root, scope, path)

        with patch.object(self.service, "_snapshot", snapshot):
            second = await self.service.ingest(self.context, self.source, "caller-retrieval", candidate)
        self.assertEqual(second.status, "Completed")
        self.assertEqual(second.artifacts, first.artifacts)
        self.assertEqual(copied, [])


_OBSERVATION = {"pid": 123, "process_start": "generation", "window_id": 45,
                "geometry": {"x": 1, "y": 1, "width": 100, "height": 100},
                "coordinate_space": "fixture", "backend": "builtin"}


class CollectionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.incoming = self.root / "incoming"
        self.incoming.mkdir()
        self.store = AuthorityStore(self.root / "operations.sqlite")

        async def gate(session):
            # The unavoidable external gate stands in for Ghost; the actual
            # coordinator, repositories, desktop lock and files are exercised.
            with self.store.transaction() as unit:
                self.assertEqual(unit.connection.execute("SELECT 1").fetchone()[0], 1)
            return {"decision": "allow", "admission_id": "admission", "task_id": "task", "inputEventId": "input"}

        self.coordinator = RunCoordinator(self.store, gate)
        with self.store.transaction() as unit:
            LedgerTx(unit).create_task("task", "operator", "Collect configured source")
            repo = AuthorityRepository(unit)
            repo.record_admission("session", "admission", "task", "input")
            self.epoch = LifecycleTx(unit).start("scheduler")
        self.service = CollectionService(self.store, self.root / "blobs", self.coordinator)

    async def source_run(self, kind):
        scope = {"root": str(self.incoming), "recipe": "local-snapshot" if kind == "local_files" else "native-attachment"}
        if kind == "desktop_attachments":
            scope.update(app="app", account="account", conversation="chat")
        source = SourceSpec.create("source", 1, kind, scope)
        with self.store.transaction() as unit:
            repo = AuthorityRepository(unit)
            repo.configure_source(source)
            repo.create_grant(JobGrant("grant", "source", 1, "session", "admission", "task", "input",
                                      source.scope_digest, scope["recipe"], time.time() + 1000))
        return source, await self.coordinator.begin_granted("grant", "scheduler", self.epoch)

    async def test_real_coordinator_async_source_reaches_verified_file_publication(self):
        source, context = await self.source_run("local_files")
        (self.incoming / "file").write_bytes(b"actual stable bytes")
        try:
            result = await self.service.collect(context, source)
        except BindingConflict:
            self.fail("Collection must await the real coordinator's async immutable source lookup")
        self.assertEqual(result.status, "Completed")
        self.assertEqual((self.root / "blobs" / result.artifacts[0].sha256).read_bytes(), b"actual stable bytes")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_cursors").fetchone()[0], 1)

    def executor(self, context, source, retrieval_id, *, no_effect=False, outcome=None):
        test = self

        class Native:
            timeout = 1

            async def call_authorized(port, command, payload, owner, **params):
                owner.validate()
                with test.store._read_unit() as unit:
                    row = CollectionRepository(unit).retrieval(retrieval_id)
                    test.assertEqual(row["state"], "Applying")
                    test.assertEqual(row["operation_id"], payload["operation_id"])
                    owned = replace(context, desktop_fence=payload["lease_fence"])
                    CollectionRepository(unit)._exact(owned, source, retrieval_id)
                if command == "observe":
                    return {"ok": True, "result": {"observation": {**_OBSERVATION, "token": "current"}}}
                if no_effect:
                    return {"ok": False, "effect_status": "not_started", "reason": "guard refused input"}
                if outcome is not None:
                    return outcome
                return {"ok": True, "effect_status": "dispatched", "result": {"dispatched": True}}

        return DesktopExecutor(self.store, self.coordinator, Native())

    async def test_real_executor_bound_download_stays_unknown_until_verified_ingest(self):
        source, context = await self.source_run("desktop_attachments")
        item = SourceItem("attachment", canonical_json({"app": "app", "account": "account",
                          "conversation": "chat", "source_item": "attachment"}))
        with self.store.transaction() as unit:
            CollectionRepository(unit).prepare_retrieval(context, source, item, "retrieval")
        executor = self.executor(context, source, "retrieval")

        def bind(unit, owned, operation, attempt):
            CollectionRepository(unit).bind_operation(owned, source, "retrieval", operation)

        params = {"expected": _OBSERVATION, "x": 5, "y": 5}
        spec = make_effect_spec(context.task_id, "click", params, kind="collection_download", intent=item.provenance)
        try:
            receipt = await executor.dispatch(context, "download", spec, "click", params, on_claim=bind)
        except BindingConflict:
            self.fail("Ephemeral desktop fence must not change the durable retrieval lineage")
        self.assertEqual(receipt["state"], "Unknown")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM operations").fetchone()[0], "Unknown")
            self.assertEqual(db.execute("SELECT count(*) FROM collection_artifacts").fetchone()[0], 0)
        target = self.incoming / "saved"
        target.write_bytes(b"verified downloaded bytes")
        candidate = FileCandidate(target, item.item_id, canonical_json({**item.provenance,
            "run_id": context.run_id, "retrieval_id": "retrieval", "operation_id": "download"}), True, True)
        result = await self.service.ingest(context, source, "retrieval", candidate)
        self.assertEqual(result.status, "Completed")
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM operations").fetchone()[0], "Applied")
            self.assertIsNone(db.execute("SELECT unresolved_retrieval_id FROM source_guard").fetchone()[0])

    async def test_real_executor_failed_receipt_resolves_exact_no_effect_retrieval(self):
        source, context = await self.source_run("desktop_attachments")
        item = SourceItem("attachment", canonical_json({"app": "app", "account": "account",
                          "conversation": "chat", "source_item": "attachment"}))
        with self.store.transaction() as unit:
            CollectionRepository(unit).prepare_retrieval(context, source, item, "retrieval")
        executor = self.executor(context, source, "retrieval", no_effect=True)

        def bind(unit, owned, operation, attempt):
            CollectionRepository(unit).bind_operation(owned, source, "retrieval", operation)

        params = {"expected": _OBSERVATION, "x": 5, "y": 5}
        spec = make_effect_spec(context.task_id, "click", params, kind="collection_download", intent=item.provenance)
        try:
            receipt = await executor.dispatch(context, "download", spec, "click", params, on_claim=bind)
        except BindingConflict:
            self.fail("Real executor must bind retrieval under its current desktop fence")
        self.assertEqual(receipt["state"], "Failed")
        with self.store.transaction() as unit:
            try:
                CollectionRepository(unit).confirm_no_effect(context, source, "retrieval", "download", receipt["result"])
            except BindingConflict:
                self.fail("Exact real executor Failed/no_effect receipt must clear the source hazard")
        with self.store.read() as db:
            self.assertIsNone(db.execute("SELECT unresolved_retrieval_id FROM source_guard").fetchone()[0])
            self.assertEqual(db.execute("SELECT state FROM authority_retrievals").fetchone()[0], "NoEffect")
            self.assertEqual(db.execute("SELECT count(*) FROM collection_artifacts").fetchone()[0], 0)

    async def test_real_no_effect_retry_keeps_historical_pending_result_in_original_run(self):
        source, first_context = await self.source_run("desktop_attachments")
        item = SourceItem("attachment", canonical_json({"app": "app", "account": "account",
                          "conversation": "chat", "source_item": "attachment"}))
        with self.store.transaction() as unit:
            CollectionRepository(unit).prepare_retrieval(first_context, source, item, "retrieval")
        first_executor = self.executor(first_context, source, "retrieval", outcome={
            "ok": False, "effect_status": "unknown", "no_effect": True,
            "result": {"reconciliation": "confirmed no native input"}})

        def first_bind(unit, owned, operation, attempt):
            CollectionRepository(unit).bind_operation(owned, source, "retrieval", operation)

        params = {"expected": _OBSERVATION, "x": 5, "y": 5}
        spec = make_effect_spec(first_context.task_id, "click", params, kind="collection_download", intent=item.provenance)
        receipt = await first_executor.dispatch(first_context, "first-download", spec, "click", params, on_claim=first_bind)
        self.assertEqual(receipt["state"], "Unknown")
        target = self.incoming / "saved"
        candidate = FileCandidate(target, item.item_id, canonical_json({**item.provenance,
            "run_id": first_context.run_id, "retrieval_id": "retrieval", "operation_id": "first-download"}), False, True)
        self.assertEqual((await self.service.ingest(first_context, source, "retrieval", candidate)).status, "Pending")
        with self.store.read() as db:
            historical = dict(db.execute("SELECT * FROM collection_results WHERE run_id=?", (first_context.run_id,)).fetchone())
        self.assertEqual(historical["status"], "Pending")
        with self.store.transaction() as unit:
            CollectionRepository(unit).confirm_no_effect(first_context, source, "retrieval", "first-download", receipt["result"])
        await self.coordinator.finish(first_context, "Failed")
        next_context = await self.coordinator.begin_granted("grant", "scheduler", self.epoch)
        test = self

        class Desktop:
            async def enumerate(port, context, stored_source):
                return [item]

            async def retrieve(port, context, stored_source, retrieval_id, observed):
                test.assertEqual(retrieval_id, "retrieval")
                executor = test.executor(context, stored_source, retrieval_id)

                def bind(unit, owned, operation, attempt):
                    CollectionRepository(unit).bind_operation(owned, stored_source, retrieval_id, operation)

                next_spec = make_effect_spec(context.task_id, "click", params, kind="collection_download", intent=observed.provenance)
                effect = await executor.dispatch(context, "next-download", next_spec, "click", params, on_claim=bind)
                test.assertEqual(effect["state"], "Unknown")
                target.write_bytes(b"verified retry bytes")
                return FileCandidate(target, observed.item_id, canonical_json({**observed.provenance,
                    "run_id": context.run_id, "retrieval_id": retrieval_id, "operation_id": "next-download"}), True, True)

        service = CollectionService(self.store, self.root / "blobs", self.coordinator, Desktop())
        result = await service.collect(next_context, source)
        self.assertEqual(result.status, "Completed")
        await self.coordinator.finish(next_context, result.status)
        with self.store.read() as db:
            self.assertEqual(dict(db.execute("SELECT * FROM collection_results WHERE result_id=?",
                                           (historical["result_id"],)).fetchone()), historical)
            item_results = [dict(row) for row in db.execute("SELECT * FROM collection_results WHERE result_id LIKE 'item:%'")]
            self.assertEqual(len(item_results), 2)
            newer = [row for row in item_results if row["run_id"] == next_context.run_id]
            self.assertEqual(len(newer), 1)
            self.assertEqual(newer[0]["status"], "Completed")
            self.assertEqual(json.loads(newer[0]["result_json"])["artifacts"][0]["run_id"], next_context.run_id)
            self.assertEqual(db.execute("SELECT state FROM authority_runs WHERE run_id=?", (first_context.run_id,)).fetchone()[0], "Failed")
            self.assertEqual(db.execute("SELECT state FROM authority_runs WHERE run_id=?", (next_context.run_id,)).fetchone()[0], "Completed")


if __name__ == "__main__":
    unittest.main()
