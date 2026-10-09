import asyncio
import tempfile
import unittest
from pathlib import Path

from ai_secretary.ledger import Ledger
from ai_secretary.service import Secretary, TOOL_NAMES

OBSERVATION = {"pid": 123, "process_start": "generation", "window_id": 45,
               "geometry": {"x": 1, "y": 1, "width": 100, "height": 100},
               "coordinate_space": "fixture", "backend": "builtin"}


class Native:
    timeout = 1
    def __init__(self): self.calls = []
    async def call(self, command, **params):
        if command not in {"status", "apps", "windows", "observe"}:
            raise AssertionError("product effects require the authorized transport")
        self.calls.append((command, params))
        return {"ok": True, "value": "synthetic"}
    async def call_authorized(self, command, payload, owner, **params):
        owner.validate()
        if command == "observe":
            return {"ok": True, "result": {"observation": {**OBSERVATION, "token": "owned-fresh"}}}
        self.calls.append((command, params))
        return {"ok": True, "effect_status": "dispatched", "result": {"dispatched": True}}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.create_task("task", "owner", "synthetic local work")
        from ai_secretary.coordinator import AuthorityRepository
        with self.ledger.store.transaction() as unit:
            AuthorityRepository.initialize(unit)
            for session in ("s", "session"):
                AuthorityRepository(unit).record_admission(session, "admission", "task", "event")
        self.native = Native()
        async def gate(session):
            return {"decision": "allow", "admission_id": "admission", "task_id": "task", "inputEventId": "event"}
        self.secretary = Secretary(self.ledger, self.native, gate)
    def tearDown(self): self.tmp.cleanup()

    async def test_replay_does_not_repeat_effect(self):
        args = {"operation_id": "op", "action": "open", "path": "/tmp/synthetic", "expected": OBSERVATION}
        await self.secretary.call("secretary_act", args, "session")
        result = await self.secretary.call("secretary_act", args, "session")
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(result["state"], "cached")

    async def test_changed_replay_rejected(self):
        await self.secretary.call("secretary_act", {"operation_id": "op", "action": "open", "path": "/tmp/a", "expected": OBSERVATION}, "s")
        with self.assertRaises(Exception):
            await self.secretary.call("secretary_act", {"operation_id": "op", "action": "open", "path": "/tmp/b", "expected": OBSERVATION}, "s")
        self.assertEqual(len(self.native.calls), 1)

    async def test_unknown_not_retried(self):
        original = self.native.call_authorized
        async def uncertain(command, payload, owner, **params):
            if command == "observe": return await original(command, payload, owner, **params)
            raise TimeoutError("child effect may have happened")
        self.native.call_authorized = uncertain
        args = {"operation_id": "uncertain", "action": "open", "path": "/tmp/a", "expected": OBSERVATION}
        result = await self.secretary.call("secretary_act", args, "s")
        self.assertEqual(result["state"], "Unknown")
        self.native.call_authorized = Native().call_authorized
        result = await self.secretary.call("secretary_act", args, "s")
        self.assertEqual(result["state"], "reconcile")

    async def test_actual_current_gate_blocks_effect(self):
        async def blocked(session): return {"decision": "block", "task_id": "task"}
        self.secretary.gate = blocked
        with self.assertRaises(PermissionError):
            await self.secretary.call("secretary_act", {"operation_id": "x", "action": "open", "path": "/tmp/a"}, "s")
        self.assertEqual(self.native.calls, [])

    async def test_changed_input_lineage_blocks_old_effect_in_same_task(self):
        from ai_secretary.coordinator import AuthorityRepository
        with self.ledger.store.transaction() as unit:
            AuthorityRepository(unit).record_admission("s", "admission", "task", "A")
        replies = iter([{"decision": "allow", "admission_id": "admission", "task_id": "task", "inputEventId": "A"}, {"decision": "allow", "admission_id": "admission", "task_id": "task", "inputEventId": "B"}])
        async def changing(session): return next(replies)
        self.secretary.gate = changing
        with self.assertRaises(PermissionError):
            await self.secretary.call("secretary_act", {"operation_id": "old-input", "action": "open", "path": "/tmp/fixture"}, "s")
        self.assertEqual(self.native.calls, [])

    async def test_outbound_requires_exact_sender_approval(self):
        content = {"account": "local-test", "recipients": ["synthetic"], "body": "hello", "attachments": [], "intent": "reply", "composer": {k: OBSERVATION[k] for k in ("pid", "process_start", "window_id")}}
        await self.secretary.call("secretary_draft", {"draft_id": "draft", "content": content}, "s")
        args = {"operation_id": "send", "action": "click", "outbound": {"draft_id": "draft", "content": content}, "x": 1, "y": 2, "expected": OBSERVATION}
        with self.assertRaises(Exception): await self.secretary.call("secretary_act", args, "s")
        from ai_secretary.contracts import content_digest
        self.ledger.approve("draft", content_digest(content), "user:owner")
        result = await self.secretary.call("secretary_act", args, "s")
        self.assertEqual(result["state"], "Applied")
        self.assertNotIn("outbound", self.native.calls[0][1])
        edited = {**content, "body": "changed"}
        with self.assertRaises(Exception):
            await self.secretary.call("secretary_act", {**args, "operation_id": "send2", "outbound": {"draft_id": "draft", "content": edited}}, "s")

    async def test_model_authority_fields_are_rejected_before_gate(self):
        async def must_not_check(session):
            self.fail("invalid model role reached gate")
        self.secretary.gate = must_not_check
        with self.assertRaises(PermissionError):
            await self.secretary.call("secretary_status", {"session": "other", "origin": "external_user"}, "s")
        self.assertEqual(self.native.calls, [])

    async def test_draft_requires_exact_composer_and_intent(self):
        with self.assertRaises(ValueError):
            await self.secretary.call("secretary_draft", {"draft_id": "unbound", "content": {"account": "local-test", "recipients": ["fixture"], "body": "hello", "attachments": []}}, "s")

    async def test_operator_result_reports_incomplete_worker_cleanup(self):
        from unittest.mock import AsyncMock, patch
        from ai_secretary.worker_sessions import WorkerDrainResult, WorkerSessions
        with patch.object(WorkerSessions, "close", AsyncMock(return_value=WorkerDrainResult("Draining", "life", 2, "timeout"))):
            result = await self.secretary.call("secretary_status", {}, "s")
        self.assertEqual(result["worker_cleanup"]["state"], "Draining")

    def test_model_tool_catalog_has_no_approval_or_intake(self):
        self.assertNotIn("approve", " ".join(TOOL_NAMES))
        self.assertNotIn("intake", " ".join(TOOL_NAMES))

if __name__ == "__main__": unittest.main()
