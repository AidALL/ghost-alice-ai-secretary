"""A supplied runtime executes the production user admission path."""
from dataclasses import FrozenInstanceError
from contextlib import closing
import asyncio
import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import signal
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from ai_secretary import control
from ai_secretary.governance import Governance


class AlternativeRuntime:
    provider = "ollama"

    def __init__(self, root):
        self.root = root
        self.requests = []

    async def dispatch(self, request):
        # A real gate must already exist before a backend receives any text.
        gate = await Governance(self.root).check(request.binding)
        if gate["decision"] != "allow" or gate["task_id"] != request.task_id:
            raise AssertionError("runtime received request before admission")
        self.requests.append(request)
        return 0


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"SECRETARY_GHOST_ROOT": os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice")),
                                         "SECRETARY_GA_LEDGER_ROOT": str(self.root / "ga")})
        self.env.start()
        self.runner = getattr(control, "run_user_request", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    async def run_request(self, text, runtime, **kwargs):
        self.assertIsNotNone(self.runner, "production user run must accept an injected runtime")
        return await self.runner(text, runtime=runtime, root=self.root, route="desktop:general", **kwargs)

    async def test_injected_runtime_gets_exact_text_after_real_admission(self):
        runtime = AlternativeRuntime(self.root)
        text = "  Review this user's files\nwithout changing the request.  "
        self.assertEqual(await self.run_request(text, runtime), 0)
        request = runtime.requests[0]
        self.assertEqual(request.text, text)
        self.assertEqual(request.binding, request.route + "::" + request.conversation_id)
        self.assertEqual(str(uuid.UUID(request.conversation_id)), request.conversation_id)
        with closing(sqlite3.connect(self.root / "admissions.sqlite")) as db:
            self.assertEqual(db.execute("SELECT digest FROM admission WHERE session=?", (request.binding,)).fetchone()[0], hashlib.sha256(text.encode()).hexdigest())
        with self.assertRaises(FrozenInstanceError):
            request.text = "altered"

    async def test_persisted_continuation_and_new_task_survive_runtime_replacement(self):
        first = AlternativeRuntime(self.root)
        await self.run_request("Prepare local work", first)
        second = AlternativeRuntime(self.root)
        await self.run_request("Continue it", second)
        third = AlternativeRuntime(self.root)
        await self.run_request("Separate work", third, new_task=True)
        one, two, three = (runtime.requests[0] for runtime in (first, second, third))
        self.assertEqual(one.binding, two.binding)
        self.assertEqual(one.task_id, two.task_id)
        self.assertNotEqual(one.admission_id, two.admission_id)
        self.assertNotEqual(one.binding, three.binding)
        self.assertNotEqual(one.task_id, three.task_id)
        with closing(sqlite3.connect(self.root / "control-sessions.sqlite")) as db:
            self.assertEqual(db.execute("SELECT native_session FROM conversations WHERE route=?", (one.route,)).fetchone()[0], three.conversation_id)

    async def test_provider_policy_rejects_route_before_backend_dispatch(self):
        runtime = AlternativeRuntime(self.root)
        runtime.provider = "hosted-provider"
        with self.assertRaisesRegex(PermissionError, "on-prem"):
            await self.run_request("Local task", runtime)
        self.assertEqual(runtime.requests, [])
        self.assertFalse((self.root / "admissions.sqlite").exists())

    async def test_actual_current_ghost_block_prevents_backend_dispatch(self):
        original_intake = Governance.intake

        def block_after_intake(governance, session, *args, **kwargs):
            admission = original_intake(governance, session, *args, **kwargs)
            spec = importlib.util.spec_from_file_location("test_ga_runtime_block", governance.ghost / "session-intent-analyzer/scripts/session_intent_ledger.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.record_turn(root=governance.ledger_root, platform=governance.platform,
                               session_id=governance.identity(session), expected_input_event_id=admission["inputEventId"],
                               intent_delta={"model_security_decision": {"decision": "block", "input_event_id": admission["inputEventId"], "risk_flags": ["fixture-block"]}})
            return admission

        runtime = AlternativeRuntime(self.root)
        with patch.object(Governance, "intake", block_after_intake):
            with self.assertRaisesRegex(PermissionError, "gate blocks"):
                await self.run_request("Synthetic blocked request", runtime)
        self.assertEqual(runtime.requests, [])

    async def test_existing_conversation_and_exact_approved_draft_continue(self):
        from ai_secretary.contracts import content_digest
        from ai_secretary.ledger import Ledger
        conversation = str(uuid.uuid4())
        with closing(sqlite3.connect(self.root / "control-sessions.sqlite")) as db, db:
            db.execute("CREATE TABLE conversations(route TEXT PRIMARY KEY,native_session TEXT NOT NULL)")
            db.execute("INSERT INTO conversations VALUES(?,?)", ("desktop:general", conversation))
        binding = "desktop:general::" + conversation
        admission = Governance(self.root).intake(binding, "legacy-input", "Prepare a reply", "external_user")
        ledger = Ledger(self.root / "operations.sqlite")
        content = {"account": "fixture", "recipients": ["fixture"], "body": "reviewed", "attachments": []}
        ledger.create_draft(admission["task_id"], "legacy-draft", content)
        ledger.approve("legacy-draft", content_digest(content), "user:fixture")
        runtime = AlternativeRuntime(self.root)
        await self.run_request("Continue the reviewed draft", runtime)
        self.assertEqual(runtime.requests[0].binding, binding)
        self.assertEqual(runtime.requests[0].task_id, admission["task_id"])
        self.assertTrue(ledger.validate_approval("legacy-draft", content, task_id=runtime.requests[0].task_id))

    async def test_cancelled_adapter_from_worker_thread_reaps_client(self):
        from ai_secretary.adapters.openclaw import OpenClawRuntime
        from ai_secretary.runtime import RunRequest
        pid_file = self.root / "thread-client.pid"
        executable = self.root / "sleeping-client"
        executable.write_text(f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(30)\n")
        executable.chmod(0o700)
        request = RunRequest("fixture", "route", str(uuid.uuid4()), "admission", "task", "event", False)

        async def dispatch_and_cancel():
            task = asyncio.create_task(OpenClawRuntime(executable).dispatch(request))
            for _ in range(100):
                if pid_file.exists(): break
                await asyncio.sleep(.02)
            if not pid_file.exists(): raise AssertionError("fixture did not start")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return "cancelled"
            except Exception as error:
                return type(error).__name__

        try:
            result = await asyncio.to_thread(lambda: asyncio.run(dispatch_and_cancel()))
            self.assertEqual(result, "cancelled")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
        finally:
            if pid_file.exists():
                try: os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError: pass


if __name__ == "__main__":
    unittest.main()
