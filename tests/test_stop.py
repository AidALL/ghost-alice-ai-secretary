import test_service_transport as transport_fixture
from ai_secretary.worker_sessions import WorkerSessions
from ai_secretary import service
import unittest


class StopTests(unittest.IsolatedAsyncioTestCase):
    setUp = transport_fixture.BoundTransportTests.setUp
    tearDown = transport_fixture.BoundTransportTests.tearDown

    async def test_stop_revokes_all_host_capabilities_before_drain(self):
        registry = WorkerSessions(self.secretary.ledger.store, self.secretary.coordinator)
        binding = await registry.open("s", "host-run")
        result = await service.stop_all(self.secretary)
        self.assertEqual(result["state"], "Stopped")
        with self.assertRaises(PermissionError): await registry.resolve(binding.token, "host-run", "s")
        with self.secretary.ledger.store.read() as db:
            self.assertFalse(db.execute("SELECT 1 FROM execution_lifecycle WHERE status<>'Stopped'").fetchone())

    async def test_stop_before_any_worker_transport_exists(self):
        self.assertEqual((await service.stop_all(self.secretary))["state"], "Stopped")


if __name__ == "__main__": unittest.main()
