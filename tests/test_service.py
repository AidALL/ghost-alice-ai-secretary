import asyncio
import tempfile
import unittest
from pathlib import Path

from ai_secretary.ledger import Ledger
from ai_secretary.service import Secretary, TOOL_NAMES


class Native:
    def __init__(self): self.calls = []
    async def call(self, command, **params):
        self.calls.append((command, params))
        return {"ok": True, "value": "synthetic"}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / "ledger.sqlite")
        self.ledger.create_task("task", "owner", "synthetic local work")
        self.native = Native()
        async def gate(session):
            return {"decision": "allow", "task_id": "task", "inputEventId": "event"}
        self.secretary = Secretary(self.ledger, self.native, gate)
    def tearDown(self): self.tmp.cleanup()

    async def test_replay_does_not_repeat_effect(self):
        args = {"operation_id": "op", "action": "open", "path": "/tmp/synthetic"}
        await self.secretary.call("secretary_act", args, "session")
        result = await self.secretary.call("secretary_act", args, "session")
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(result["state"], "cached")

    async def test_changed_replay_rejected(self):
        await self.secretary.call("secretary_act", {"operation_id": "op", "action": "open", "path": "/tmp/a"}, "s")
        with self.assertRaises(Exception):
            await self.secretary.call("secretary_act", {"operation_id": "op", "action": "open", "path": "/tmp/b"}, "s")
        self.assertEqual(len(self.native.calls), 1)

    async def test_unknown_not_retried(self):
        async def uncertain(command, **params): raise TimeoutError("child effect may have happened")
        self.native.call = uncertain
        args = {"operation_id": "uncertain", "action": "open", "path": "/tmp/a"}
        result = await self.secretary.call("secretary_act", args, "s")
        self.assertEqual(result["state"], "Unknown")
        self.native.call = Native().call
        result = await self.secretary.call("secretary_act", args, "s")
        self.assertEqual(result["state"], "reconcile")

    async def test_actual_current_gate_blocks_effect(self):
        async def blocked(session): return {"decision": "block", "task_id": "task"}
        self.secretary.gate = blocked
        with self.assertRaises(PermissionError):
            await self.secretary.call("secretary_act", {"operation_id": "x", "action": "open", "path": "/tmp/a"}, "s")
        self.assertEqual(self.native.calls, [])

    async def test_changed_input_lineage_blocks_old_effect_in_same_task(self):
        replies = iter([{"decision": "allow", "task_id": "task", "inputEventId": "A"}, {"decision": "allow", "task_id": "task", "inputEventId": "B"}])
        async def changing(session): return next(replies)
        self.secretary.gate = changing
        with self.assertRaises(PermissionError):
            await self.secretary.call("secretary_act", {"operation_id": "old-input", "action": "open", "path": "/tmp/fixture"}, "s")
        self.assertEqual(self.native.calls, [])

    async def test_outbound_requires_exact_sender_approval(self):
        content = {"account": "local-test", "recipients": ["synthetic"], "body": "hello", "attachments": []}
        await self.secretary.call("secretary_draft", {"draft_id": "draft", "content": content}, "s")
        args = {"operation_id": "send", "action": "click", "outbound": {"draft_id": "draft", "content": content}, "x": 1, "y": 2}
        with self.assertRaises(Exception): await self.secretary.call("secretary_act", args, "s")
        from ai_secretary.contracts import content_digest
        self.ledger.approve("draft", content_digest(content), "user:owner")
        result = await self.secretary.call("secretary_act", args, "s")
        self.assertEqual(result["state"], "Applied")
        self.assertNotIn("outbound", self.native.calls[0][1])
        edited = {**content, "body": "changed"}
        with self.assertRaises(Exception):
            await self.secretary.call("secretary_act", {**args, "operation_id": "send2", "outbound": {"draft_id": "draft", "content": edited}}, "s")

    def test_model_tool_catalog_has_no_approval_or_intake(self):
        self.assertNotIn("approve", " ".join(TOOL_NAMES))
        self.assertNotIn("intake", " ".join(TOOL_NAMES))

if __name__ == "__main__": unittest.main()
