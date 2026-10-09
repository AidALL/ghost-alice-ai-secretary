import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ai_secretary.governance import Governance


class GovernanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"SECRETARY_GHOST_ROOT": os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice")), "SECRETARY_GA_LEDGER_ROOT": str(self.root / "ga"), "SECRETARY_NODE": os.environ.get("SECRETARY_NODE", "node")})
        self.env.start()
        self.g = Governance(self.root)
    def tearDown(self): self.env.stop(); self.tmp.cleanup()

    async def test_missing_actual_intake_denied(self):
        with self.assertRaises(PermissionError): await self.g.check("s")

    async def test_native_admission_is_deduplicated(self):
        one = self.g.intake("s", "native-1", "Synthetic local task", "external_user")
        two = self.g.intake("s", "native-1", "Synthetic local task", "external_user")
        self.assertEqual(one, two)
        gate = await self.g.check("s")
        self.assertEqual(gate["decision"], "allow")
        self.assertEqual(gate["task_id"], one["task_id"])
        self.assertEqual(gate["admission_id"], "native-1")

    async def test_classification_cannot_be_missing_or_delegated(self):
        for origin in ("unknown", "internal_system", "inter_session"):
            with self.assertRaises(PermissionError): self.g.intake("s", "id", "task", origin)

    async def test_admission_identity_cannot_change_payload(self):
        self.g.intake("s", "id", "task", "external_user")
        with self.assertRaises(ValueError): self.g.intake("s", "id", "changed", "external_user")

    async def test_old_admission_cannot_replace_current_task(self):
        self.g.intake("s", "a", "task a", "external_user")
        latest = self.g.intake("s", "b", "task b", "external_user")
        with self.assertRaises(PermissionError): self.g.intake("s", "a", "task a", "external_user")
        self.assertEqual((await self.g.check("s"))["task_id"], latest["task_id"])

    async def test_new_input_invalidates_old_native_context(self):
        from ai_secretary.authority import AuthorityStore
        from ai_secretary.coordinator import RunCoordinator
        self.g.intake("s", "a", "Prepare local work", "external_user")
        store = AuthorityStore(self.root / "operations.sqlite")
        context = await RunCoordinator(store, self.g.check).begin_foreground("s")
        self.g.intake("s", "b", "Use the updated instruction", "external_user")
        with store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM execution_contexts WHERE run_id=?", (context.run_id,)).fetchone()[0], "Draining")

    async def test_current_legacy_admission_is_migrated_without_new_intake(self):
        from ai_secretary.authority import AuthorityStore
        from ai_secretary.coordinator import RunCoordinator
        self.g.intake("s", "a", "Current admitted work", "external_user")
        store = AuthorityStore(self.root / "operations.sqlite")
        with store.transaction() as uow:
            uow.connection.execute("DROP TABLE execution_admissions")
        migrated = Governance(self.root)
        context = await RunCoordinator(store, migrated.check).begin_foreground("s")
        self.assertEqual(context.admission_id, "a")

    async def test_continuation_preserves_task_and_approved_draft(self):
        from ai_secretary.ledger import Ledger
        from ai_secretary.contracts import content_digest
        first = self.g.intake("s", "a", "Prepare a reply", "external_user")
        ledger = Ledger(self.root / "operations.sqlite")
        content = {"account": "test", "recipients": ["test"], "body": "received", "attachments": []}
        ledger.create_draft(first["task_id"], "reply", content)
        ledger.approve("reply", content_digest(content), "user:test")
        followup = self.g.intake("s", "b", "I reviewed that draft; continue", "external_user")
        self.assertEqual(first["task_id"], followup["task_id"])
        self.assertTrue(ledger.validate_approval("reply", content, task_id=followup["task_id"]))

    async def test_explicit_new_task_cannot_use_old_draft(self):
        from ai_secretary.ledger import Ledger
        from ai_secretary.contracts import content_digest, ApprovalRequired
        first = self.g.intake("s", "a", "Prepare a reply", "external_user")
        ledger = Ledger(self.root / "operations.sqlite")
        content = {"body": "received"}
        ledger.create_draft(first["task_id"], "reply", content)
        ledger.approve("reply", content_digest(content), "user:test")
        latest = self.g.intake("s", "b", "Begin separate work", "external_user", new_task=True)
        self.assertNotEqual(first["task_id"], latest["task_id"])
        with self.assertRaises(ApprovalRequired): ledger.validate_approval("reply", content, task_id=latest["task_id"])

if __name__ == "__main__": unittest.main()
