import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_secretary import service
from ai_secretary.coordinator import AuthorityRepository
from ai_secretary.ledger import Ledger
from test_service import Native


class BoundTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        ledger = Ledger(self.root / "operations.sqlite")
        ledger.create_task("task", "owner", "fixture transport")
        with ledger.store.transaction() as unit:
            AuthorityRepository.initialize(unit)
            AuthorityRepository(unit).record_admission("s", "admission", "task", "event")
        async def gate(session):
            return {"decision": "allow", "admission_id": "admission", "task_id": "task", "inputEventId": "event"}
        self.secretary = service.Secretary(ledger, Native(), gate)
        self.factory = patch.object(service, "build_services", return_value=self.secretary)
        self.factory.start()

    def tearDown(self):
        self.factory.stop()
        self.tmp.cleanup()

    async def test_call_requires_host_private_capability(self):
        with self.assertRaises(PermissionError):
            await service.handle({"kind": "call", "session": "s", "tool": "secretary_status", "args": {}})

    async def test_persistent_capability_resolves_and_closed_transport_refuses(self):
        binding = await service.handle({"kind": "worker_open", "session": "s", "transport_id": "host-run"})
        request = {"kind": "call", "session": "s", "transport_id": "host-run", "capability": binding["token"], "tool": "secretary_status", "args": {}}
        self.assertTrue((await service.handle(request))["ok"])
        result = await service.handle({"kind": "worker_close", "session": "s", "transport_id": "host-run", "capability": binding["token"]})
        self.assertEqual(result["state"], "Stopped")
        with self.assertRaises(PermissionError): await service.handle(request)

    async def test_invalid_authority_argument_rejected_before_open_or_gate(self):
        async def bad_gate(session): self.fail("model control argument reached authority gate")
        self.secretary.gate = bad_gate
        with self.assertRaises(PermissionError):
            await service.handle({"kind": "call", "session": "s", "tool": "secretary_status", "args": {"origin": "external_user"}})

    async def test_wrong_transport_cannot_rebind_capability(self):
        binding = await service.handle({"kind": "worker_open", "session": "s", "transport_id": "one"})
        with self.assertRaises(PermissionError):
            await service.handle({"kind": "call", "session": "s", "transport_id": "two", "capability": binding["token"], "tool": "secretary_status", "args": {}})


if __name__ == "__main__": unittest.main()
