import json
from pathlib import Path
import tempfile
import unittest

from ai_secretary.service import build_scheduled_services


class ScheduledAssemblyTests(unittest.TestCase):
    def test_installed_layout_preserves_existing_state_and_configuration_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary).resolve() / "prefix"
            private_state = prefix / "state"
            secretary_state = private_state / "secretary"
            openclaw_state = private_state / "openclaw"
            secretary_state.mkdir(parents=True, mode=0o700)
            openclaw_state.mkdir(mode=0o700)
            host_config = private_state / "openclaw.json"
            host_bytes = b'{"mutable":"existing operator configuration"}\n'
            host_config.write_bytes(host_bytes)
            runtime_path = secretary_state / "runtime.json"
            runtime = {
                "native_backend": "peekaboo",
                "native_binary": str(prefix / "managed/app/build/desktop-secretary-peekaboo"),
                "python_executable": str(prefix / "managed/bin/ai-secretary-python"),
                "openclaw_executable": str(prefix / "managed/bin/openclaw"),
                "openclaw_state": str(openclaw_state),
                "openclaw_config": str(host_config),
            }
            runtime_bytes = (json.dumps(runtime) + "\n").encode()
            runtime_path.write_bytes(runtime_bytes)
            try:
                store, manager, coordinator, collection_factory = build_scheduled_services(secretary_state)
            except ValueError as error:
                self.fail(f"production assembly must accept the installer-owned state layout: {error}")
            self.assertEqual(manager.scheduler.state_dir, str(openclaw_state))
            self.assertEqual(manager.scheduler.config_path, str(host_config))
            self.assertEqual(manager.scheduler.isolation_root, str(private_state))
            self.assertEqual(manager.runner_config.command("fixture-occurrence")[0], runtime["python_executable"])
            self.assertIs(manager.store, store)
            self.assertIs(manager.coordinator, coordinator)
            self.assertIs(collection_factory().store, store)
            self.assertEqual(host_config.read_bytes(), host_bytes)
            self.assertEqual(runtime_path.read_bytes(), runtime_bytes)


if __name__ == "__main__":
    unittest.main()
