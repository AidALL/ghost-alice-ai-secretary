"""Synthetic user-control transport tests; grants authorize no real message."""
import os
from pathlib import Path
import pty
import subprocess
import sys
import tempfile
import unittest
import json
import signal
import time
from ai_secretary.ledger import Ledger
from ai_secretary.contracts import ApprovalRequired


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "operations.sqlite")
        self.ledger.create_task("test", "test-user", "Synthetic approval transport fixture")
        self.content = {"account": "fixture", "recipients": ["fixture"], "body": "fixture", "attachments": []}
        self.digest = self.ledger.create_draft("test", "draft", self.content)
        self.env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"), "SECRETARY_STATE_DIR": str(self.root)}
        self.command = [sys.executable, "-m", "ai_secretary.control", "approve", "draft", "--digest", self.digest]
    def tearDown(self): self.tmp.cleanup()

    def test_noninteractive_approval_is_refused(self):
        result = subprocess.run(self.command, input=f"APPROVE {self.digest}\n", env=self.env, text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        with self.assertRaises(ApprovalRequired): self.ledger.validate_approval("draft", self.content, task_id="test")

    def test_interactive_fixture_approval_displays_stored_content(self):
        master, slave = pty.openpty()
        try:
            child = subprocess.Popen(self.command, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
            os.close(slave); slave = None
            os.write(master, f"APPROVE {self.digest}\n".encode())
            try: stdout, stderr = child.communicate(timeout=5)
            except BaseException:
                child.kill(); child.communicate(); raise
            self.assertEqual(child.returncode, 0, stderr.decode())
            self.assertIn(self.digest, stdout.decode())
            self.assertIn('content_json', stdout.decode())
            self.assertTrue(self.ledger.validate_approval("draft", self.content, task_id="test"))
        finally:
            os.close(master)
            if slave is not None: os.close(slave)

    def test_run_binds_exact_user_text_to_native_flags_and_continues_task(self):
        executable = self.root / "openclaw-fixture"
        executable.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        executable.chmod(0o700)
        env = {**self.env, "SECRETARY_GHOST_ROOT": os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice")), "SECRETARY_GA_LEDGER_ROOT": str(self.root / "ga")}
        command = [sys.executable, "-m", "ai_secretary.control", "--session", "agent:main:test", "run", "actual fixture request", "--openclaw", str(executable)]
        one = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(one.returncode, 0, one.stderr)
        flags = json.loads(one.stdout)
        sid = flags[flags.index("--session-id") + 1]
        self.assertEqual(flags[flags.index("--session-key") + 1], "agent:main:test")
        self.assertEqual(flags[flags.index("--message") + 1], "actual fixture request")
        two = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(two.returncode, 0, two.stderr)
        self.assertEqual(json.loads(two.stdout)[flags.index("--session-id") + 1], sid)
        three = subprocess.run([*command, "--new-task"], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(three.returncode, 0, three.stderr)
        self.assertNotEqual(json.loads(three.stdout)[flags.index("--session-id") + 1], sid)

    def test_named_runtime_accepts_generic_executable_option(self):
        executable = self.root / "runtime-fixture"
        executable.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
        executable.chmod(0o700)
        env = {**self.env, "SECRETARY_GHOST_ROOT": os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice")), "SECRETARY_GA_LEDGER_ROOT": str(self.root / "ga")}
        command = [sys.executable, "-m", "ai_secretary.control", "--session", "route:test", "run", "exact user request", "--runtime", "openclaw", "--runtime-executable", str(executable)]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        flags = json.loads(result.stdout)
        self.assertEqual(flags[flags.index("--message") + 1], "exact user request")
        self.assertEqual(flags[flags.index("--session-key") + 1], "route:test")

    def test_repeated_interrupt_during_cleanup_does_not_strand_client(self):
        pid_file = self.root / "client.pid"
        executable = self.root / "stubborn-client"
        executable.write_text(f"#!{sys.executable}\nimport os,signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nPath({str(pid_file)!r}).write_text(str(os.getpid()))\nwhile True: time.sleep(1)\n")
        executable.chmod(0o700)
        env = {**self.env, "SECRETARY_GHOST_ROOT": os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice")), "SECRETARY_GA_LEDGER_ROOT": str(self.root / "ga")}
        parent = subprocess.Popen([sys.executable, "-m", "ai_secretary.control", "run", "local fixture only", "--openclaw", str(executable)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        client_pid = None
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists():
                if time.monotonic() > deadline: self.fail("fixture client did not start")
                time.sleep(.02)
            client_pid = int(pid_file.read_text())
            parent.send_signal(signal.SIGINT); time.sleep(.7); parent.send_signal(signal.SIGINT)
            stdout, stderr = parent.communicate(timeout=7)
            self.assertEqual(parent.returncode, 130, stderr.decode())
            with self.assertRaises(ProcessLookupError): os.kill(client_pid, 0)
            self.assertIn("reconcile", stderr.decode())
        finally:
            if client_pid:
                try: os.kill(client_pid, signal.SIGKILL)
                except ProcessLookupError: pass
            if parent.poll() is None: parent.kill()
            parent.communicate(timeout=3)

if __name__ == "__main__": unittest.main()
