"""Declarative attachment recipes against real authority, effects and files."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from ai_secretary.authority import AuthorityStore
from ai_secretary.authority_contracts import JobGrant, SourceSpec
from ai_secretary.collection import CollectionRepository, CollectionService, SourceItem
from ai_secretary.coordinator import AuthorityRepository, LifecycleTx, RunCoordinator
from ai_secretary.contracts import BindingConflict, canonical_json
from ai_secretary.execution import DesktopExecutor
from ai_secretary.ledger import LedgerTx

try:
    from ai_secretary.desktop_collection import DesktopAttachmentRecipe
except ImportError:
    DesktopAttachmentRecipe = None


def selector(role, identifier, text):
    return {"role": role, "identifier": identifier, "text": text}


def node(role, identifier, text, y, *, actionable=False):
    return {"ref": identifier + "-ref", "role": role, "identifier": identifier,
            "label": text, "geometry": {"x": 20, "y": y, "width": 200, "height": 20},
            "enabled": True, "visible": True, "actionable": actionable}


class NativeBoundary:
    """Only OS observations/input are simulated; effect ownership is real."""

    timeout = 1

    def __init__(self, root):
        self.root, self.downloads = root, 0
        self.complete, self.no_effect = True, False
        self.before_authorized = None
        self.nodes = [node("AXStaticText", "account", "account", 20),
                      node("AXStaticText", "conversation", "chat", 50),
                      node("AXStaticText", "item", "report.txt", 80),
                      node("AXButton", "download", "Download report.txt", 110, actionable=True),
                      node("AXStaticText", "completion", "Not downloaded", 140)]

    def observed(self):
        observation = {"pid": 123, "process_start": "start", "window_id": 45,
                       "geometry": {"x": 1, "y": 1, "width": 500, "height": 300},
                       "coordinate_space": "fixture", "backend": "peekaboo", "token": "signed",
                       "selectors": {"schema": "secretary-selectors-v1", "refs": deepcopy(self.nodes),
                                     "truncated": False}}
        return {"ok": True, "result": {"observation": observation,
                "window": {"pid": 123, "window_id": 45, "title": "Fixture"},
                "ax": {"available": True, "nodes": deepcopy(self.nodes), "truncated": False}}}

    async def call(self, command, **params):
        if command == "apps":
            return {"ok": True, "result": {"apps": [{"pid": 123, "name": "Fixture", "bundle_id": "app", "process_start": "start"}]}}
        if command == "windows":
            return {"ok": True, "result": {"windows": [{"pid": 123, "window_id": 45, "title": "Fixture"}]}}
        if command == "observe":
            return self.observed()
        raise AssertionError("Unsigned effect is forbidden")

    async def call_authorized(self, command, payload, owner, **params):
        owner.validate()
        if command == "observe":
            if self.before_authorized:
                self.before_authorized()
                self.before_authorized = None
            return self.observed()
        if self.no_effect:
            return {"ok": False, "effect_status": "not_started", "reason": "input refused"}
        self.downloads += 1
        (self.root / "report.txt").write_bytes(b"complete bytes" if self.complete else b"partial")
        if self.complete:
            self.nodes[-1]["label"] = "Downloaded report.txt"
        return {"ok": True, "effect_status": "dispatched"}


class DesktopRecipeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(DesktopAttachmentRecipe, "The trusted desktop attachment recipe is missing")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.incoming = self.base / "incoming"
        self.incoming.mkdir()
        self.store = AuthorityStore(self.base / "operations.sqlite")

        async def gate(session):
            return {"decision": "allow", "admission_id": "admission", "task_id": "task", "inputEventId": "input"}

        self.coordinator = RunCoordinator(self.store, gate)
        self.recipe = {"app_selector": {"field": "bundle_id", "equals": "app"}, "window_title": "Fixture",
                       "account_selector": selector("AXStaticText", "account", "account"),
                       "conversation_selector": selector("AXStaticText", "conversation", "chat"),
                       "items": [{"item_id": "report", "item_selector": selector("AXStaticText", "item", "report.txt"),
                                  "download_selector": selector("AXButton", "download", "Download report.txt"),
                                  "filename": "report.txt", "completion": {
                                      "before": selector("AXStaticText", "completion", "Not downloaded"),
                                      "after": selector("AXStaticText", "completion", "Downloaded report.txt")}}]}
        self.source = SourceSpec.create("source", 1, "desktop_attachments", {
            "root": str(self.incoming), "app": "app", "account": "account", "conversation": "chat",
            "recipe": "desktop-attachment-v1", "desktop": self.recipe, "max_items": 10, "max_bytes": 1000})
        with self.store.transaction() as unit:
            LedgerTx(unit).create_task("task", "operator", "Collect exact attachment")
            repo = AuthorityRepository(unit)
            repo.record_admission("session", "admission", "task", "input")
            self.epoch = LifecycleTx(unit).start("scheduler")
            repo.configure_source(self.source)
            repo.create_grant(JobGrant("grant", "source", 1, "session", "admission", "task", "input",
                                      self.source.scope_digest, "desktop-attachment-v1", time.time() + 1000))
        self.context = await self.coordinator.begin_granted("grant", "scheduler", self.epoch)
        self.native = NativeBoundary(self.incoming)
        self.executor = DesktopExecutor(self.store, self.coordinator, self.native)
        self.port = DesktopAttachmentRecipe(self.store, self.coordinator, self.native, executor=self.executor)
        self.collection = CollectionService(self.store, self.base / "blobs", self.coordinator, self.port)

    async def prepared(self):
        items = await self.port.enumerate(self.context, self.source)
        self.assertEqual(len(items), 1)
        with self.store.transaction() as unit:
            CollectionRepository(unit).prepare_retrieval(self.context, self.source, items[0], "retrieval")
        return items[0]

    def rows(self, table):
        with self.store.read() as db:
            return [dict(row) for row in db.execute("SELECT * FROM " + table)]

    async def test_exact_observed_item_then_transition_publishes_verified_artifact(self):
        result = await self.collection.collect(self.context, self.source)
        self.assertEqual(result.status, "Completed")
        self.assertEqual(len(result.artifacts), 1)
        self.assertEqual((self.base / "blobs" / result.artifacts[0].sha256).read_bytes(), b"complete bytes")
        self.assertEqual(self.rows("operations")[0]["state"], "Applied")
        self.assertIsNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])

    async def test_file_appearance_without_source_completion_stays_pending(self):
        self.native.complete = False
        result = await self.collection.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_cursors"), [])
        self.assertEqual(self.rows("operations")[0]["state"], "Unknown")

    async def test_resume_uses_original_operation_without_redownload(self):
        self.native.complete = False
        item = await self.prepared()
        first = await self.port.retrieve(self.context, self.source, "retrieval", item)
        self.assertFalse(first.source_complete)
        original = self.rows("operations")[0]["operation_id"]
        self.native.nodes[-1]["label"] = "Downloaded report.txt"
        (self.incoming / "report.txt").write_bytes(b"eventual completion")
        resumed = await self.port.correlate(self.context, self.source, "retrieval", str(self.incoming / "report.txt"))
        self.assertTrue(resumed.source_complete)
        self.assertTrue(resumed.correlated)
        self.assertEqual(resumed.provenance["operation_id"], original)
        self.assertEqual(self.native.downloads, 1)
        self.assertEqual((await self.collection.ingest(self.context, self.source, "retrieval", resumed)).status, "Completed")

    async def test_unknown_retrieve_again_only_correlates(self):
        self.native.complete = False
        item = await self.prepared()
        await self.port.retrieve(self.context, self.source, "retrieval", item)
        await self.port.retrieve(self.context, self.source, "retrieval", item)
        self.assertEqual(self.native.downloads, 1)
        self.assertEqual(len(self.rows("operations")), 1)

    async def test_preexisting_identical_file_without_identity_change_is_not_correlated(self):
        path = self.incoming / "report.txt"
        path.write_bytes(b"complete bytes")
        item = await self.prepared()
        self.native.complete = False
        self.native.no_effect = False
        # The real no-op OS boundary reports accepted input without a file write.
        async def accepted(command, payload, owner, **params):
            owner.validate()
            if command == "observe":
                return self.native.observed()
            self.native.nodes[-1]["label"] = "Downloaded report.txt"
            return {"ok": True, "effect_status": "dispatched"}
        self.native.call_authorized = accepted
        candidate = await self.port.retrieve(self.context, self.source, "retrieval", item)
        self.assertFalse(candidate.correlated)
        self.assertEqual((await self.collection.ingest(self.context, self.source, "retrieval", candidate)).status, "Unknown")

    async def test_confirmed_no_effect_receipt_clears_exact_retrieval_hazard(self):
        self.native.no_effect = True
        result = await self.collection.collect(self.context, self.source)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("authority_retrievals")[0]["state"], "NoEffect")
        self.assertIsNone(self.rows("source_guard")[0]["unresolved_retrieval_id"])

    async def test_guessed_candidate_path_is_rejected(self):
        item = await self.prepared()
        await self.port.retrieve(self.context, self.source, "retrieval", item)
        with self.assertRaises(BindingConflict):
            await self.port.correlate(self.context, self.source, "retrieval", str(self.incoming / "other.txt"))

    async def test_guessed_item_provenance_is_rejected_before_effect(self):
        item = await self.prepared()
        altered = SourceItem(item.item_id, canonical_json({**item.provenance, "conversation": "other"}))
        with self.assertRaises(BindingConflict):
            await self.port.retrieve(self.context, self.source, "retrieval", altered)
        self.assertEqual(self.native.downloads, 0)

    async def test_other_run_cannot_resume_original_retrieval(self):
        item = await self.prepared()
        await self.port.retrieve(self.context, self.source, "retrieval", item)
        with self.assertRaises((BindingConflict, PermissionError)):
            await self.port.correlate(replace(self.context, run_id="other"), self.source, "retrieval", self.incoming / "report.txt")

    async def test_symlink_file_is_never_correlated(self):
        self.native.complete = False
        item = await self.prepared()
        await self.port.retrieve(self.context, self.source, "retrieval", item)
        target = self.incoming / "report.txt"
        target.unlink()
        outside = self.base / "outside"
        outside.write_bytes(b"private")
        target.symlink_to(outside)
        self.native.nodes[-1]["label"] = "Downloaded report.txt"
        candidate = await self.port.correlate(self.context, self.source, "retrieval", target)
        self.assertFalse(candidate.correlated)

    async def test_duplicate_identity_selector_refuses_enumeration(self):
        self.native.nodes.append(deepcopy(self.native.nodes[0]))
        with self.assertRaises(BindingConflict):
            await self.port.enumerate(self.context, self.source)
        self.assertEqual(self.native.downloads, 0)

    async def test_account_change_during_claim_refuses_input_and_records_no_effect(self):
        item = await self.prepared()
        self.native.before_authorized = lambda: self.native.nodes[0].update(label="other account")
        candidate = await self.port.retrieve(self.context, self.source, "retrieval", item)
        self.assertFalse(candidate.correlated)
        self.assertEqual(self.native.downloads, 0)
        self.assertEqual(self.rows("authority_retrievals")[0]["state"], "NoEffect")

    async def test_completion_already_changed_before_dispatch_refuses_input(self):
        item = await self.prepared()
        self.native.before_authorized = lambda: self.native.nodes[-1].update(label="Downloaded report.txt")
        candidate = await self.port.retrieve(self.context, self.source, "retrieval", item)
        self.assertFalse(candidate.correlated)
        self.assertEqual(self.native.downloads, 0)

    async def test_completion_replacement_node_is_not_a_transition(self):
        self.native.complete = False
        item = await self.prepared()
        await self.port.retrieve(self.context, self.source, "retrieval", item)
        self.native.nodes[-1]["label"] = "Downloaded report.txt"
        self.native.nodes[-1]["geometry"]["y"] += 30
        candidate = await self.port.correlate(self.context, self.source, "retrieval", self.incoming / "report.txt")
        self.assertFalse(candidate.source_complete)

    async def test_correlate_requires_durable_pre_effect_baseline(self):
        await self.prepared()
        with self.assertRaises(BindingConflict):
            await self.port.correlate(self.context, self.source, "retrieval", self.incoming / "report.txt")

    async def test_recipe_restart_preserves_original_baseline_and_operation(self):
        self.native.complete = False
        item = await self.prepared()
        await self.port.retrieve(self.context, self.source, "retrieval", item)
        restarted = DesktopAttachmentRecipe(self.store, self.coordinator, self.native, executor=self.executor)
        self.native.nodes[-1]["label"] = "Downloaded report.txt"
        candidate = await restarted.correlate(self.context, self.source, "retrieval", self.incoming / "report.txt")
        self.assertTrue(candidate.source_complete)
        self.assertTrue(candidate.correlated)
        self.assertEqual(self.native.downloads, 1)

    async def test_correlated_identity_and_hash_are_carried_to_snapshot_publication(self):
        item = await self.prepared()
        candidate = await self.port.retrieve(self.context, self.source, "retrieval", item)
        self.assertIsNotNone(candidate.observed_identity_json)
        self.assertEqual(candidate.expected_sha256, hashlib.sha256(b"complete bytes").hexdigest())
        path = self.incoming / "report.txt"
        path.unlink()
        path.write_bytes(b"uncorrelated replacement")
        result = await self.collection.ingest(self.context, self.source, "retrieval", candidate)
        self.assertEqual(result.status, "Pending")
        self.assertEqual(self.rows("collection_cursors"), [])

    async def test_off_window_identity_is_not_observed_source_evidence(self):
        self.native.nodes[0]["geometry"]["x"] = 900
        with self.assertRaises(BindingConflict):
            await self.port.enumerate(self.context, self.source)

    async def test_window_title_changed_after_selection_is_rejected(self):
        original = self.native.observed
        def changed():
            response = original()
            response["result"]["window"]["title"] = "Other window"
            return response
        self.native.observed = changed
        with self.assertRaises(BindingConflict):
            await self.port.enumerate(self.context, self.source)

    async def test_invalid_recipe_limits_and_arbitrary_code_are_rejected_before_input(self):
        for mutate in (lambda config: config.update(callback="evil()"),
                       lambda config: config["items"][0].update(filename="../outside"),
                       lambda config: config["items"][0]["completion"].update(after=config["items"][0]["completion"]["before"]),
                       lambda config: config.update(items=config["items"] * 101)):
            config = deepcopy(self.recipe)
            mutate(config)
            changed = SourceSpec.create("source", 1, "desktop_attachments", {**self.source.scope, "desktop": config})
            with self.assertRaises((ValueError, BindingConflict)):
                await self.port.enumerate(self.context, changed)
        self.assertEqual(self.native.downloads, 0)


if __name__ == "__main__":
    unittest.main()
