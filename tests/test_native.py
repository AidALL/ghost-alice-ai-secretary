import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid


class NativeClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "operations.sqlite"
        self.db.touch(mode=0o600)
        self.owner_module = importlib.import_module("ai_secretary.desktop_owner")
        try:
            self.module = importlib.import_module("ai_secretary.native")
        except ModuleNotFoundError:
            self.module = None

    def client(self, source, **kwargs):
        self.assertIsNotNone(self.module, "native subprocess adapter must exist")
        self.assertTrue(hasattr(self.module.NativeClient, "call_authorized"),
                        "server-only signed native entrypoint must exist")
        binary = self.root / "fake-native"
        binary.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        binary.chmod(0o700)
        return self.module.NativeClient(binary, state_dir=self.root, **kwargs)

    def payload(self, owner, **updates):
        return {"db_path": str(self.db), "run_id": "run", "session": "session",
                "admission_id": "admission", "task_id": "task", "input_event_id": "input",
                "lifecycle_id": "life", "epoch": 1, "run_fence": 2,
                "operation_id": "operation", "attempt_id": "attempt", "lease_owner": "owner",
                "lease_fence": 3, "lease_boot_id": "boot", "dispatch_lock_fd": owner.fd, **updates}

    async def authorized(self, client, command, **params):
        async with self.owner_module.DesktopOwnership(self.db) as owner:
            return await client.call_authorized(command, self.payload(owner), owner, **params)

    async def test_unsigned_effect_and_reserved_fields_never_start_child(self):
        marker = self.root / "started"
        client = self.client(f"open({str(marker)!r},'w').write('yes')\n")
        for command in self.module.EFFECT_COMMANDS:
            with self.subTest(command=command):
                with self.assertRaises(self.module.NativeError) as raised:
                    await client.call(command)
                self.assertEqual(raised.exception.code, "authorization_required")
                self.assertFalse(raised.exception.effect_uncertain)
        for field in ("key_path", "_authorization", "ownership", "backend", "run_id", "dispatch_lock_fd"):
            with self.subTest(field=field):
                with self.assertRaises(self.module.NativeError):
                    await client.call("observe", **{field: "forged"})
                async with self.owner_module.DesktopOwnership(self.db) as owner:
                    with self.assertRaises(self.module.NativeError):
                        await client.call_authorized("click", self.payload(owner), owner, **{field: "forged"})
        self.assertFalse(marker.exists())

    async def test_authorized_envelope_is_exact_raw_json_hmac_and_inherits_live_lock(self):
        client = self.client("""import base64, fcntl, hashlib, hmac, json, os, pathlib, sys
r = json.load(sys.stdin)
raw = base64.b64decode(r['_authorization']['payload'], validate=True)
p = json.loads(raw)
assert hmac.compare_digest(r['_authorization']['token'], hmac.new(pathlib.Path(r['key_path']).read_bytes(), raw, hashlib.sha256).hexdigest())
lock = pathlib.Path(p['db_path']).parent / 'desktop-dispatch.lock'
assert os.fstat(p['dispatch_lock_fd']).st_ino == lock.stat().st_ino
challenger = os.open(lock, os.O_RDWR)
try:
    fcntl.flock(challenger, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    pass
else:
    raise AssertionError('inherited ownership is not exclusive')
print(json.dumps({'ok': True, 'result': r}))
""", backend="peekaboo")
        async with self.owner_module.DesktopOwnership(self.db) as owner:
            payload = self.payload(owner, session="한글 世界 😀")
            response = await client.call_authorized("observe", payload, owner, pid=123)
            request = response["result"]
            raw = base64.b64decode(request["_authorization"]["payload"], validate=True)
            self.assertEqual(json.loads(raw), payload)
            self.assertEqual(raw, json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
            self.assertEqual(set(json.loads(raw)), set(payload))
            self.assertEqual(request["backend"], "peekaboo")
            key = self.root / "native-observation.key"
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(key.read_bytes()), 32)
            owner.validate()

    async def test_payload_fields_counters_and_actual_owner_are_checked_before_launch(self):
        marker = self.root / "started"
        client = self.client(f"open({str(marker)!r},'w').write('yes')\n")
        async with self.owner_module.DesktopOwnership(self.db) as owner:
            bad_values = [{"epoch": True}, {"run_fence": 0}, {"lease_fence": 2**53},
                          {"dispatch_lock_fd": owner.fd + 100}, {"db_path": str(self.root / "other.db")},
                          {"session": ""}, {"session": "\ud800"}, {"task_id": "x" * 4097},
                          {"backend": "builtin"},
                          {name: "x" * 4000 for name in ("session", "admission_id", "task_id", "input_event_id", "run_id")}]
            for values in bad_values:
                with self.subTest(values=list(values)):
                    with self.assertRaises(self.module.NativeError) as raised:
                        try:
                            await client.call_authorized("click", self.payload(owner, **values), owner)
                        except UnicodeError:
                            self.fail("invalid UTF-8 identities must become a preflight NativeError")
                    self.assertFalse(raised.exception.effect_uncertain)
            missing = self.payload(owner)
            del missing["attempt_id"]
            with self.assertRaises(self.module.NativeError):
                await client.call_authorized("click", missing, owner)
        with self.assertRaises(self.module.NativeError):
            await client.call_authorized("click", missing, owner)
        self.assertFalse(marker.exists())

    async def test_public_or_symlink_key_is_refused_before_launch(self):
        marker = self.root / "started"
        client = self.client(f"open({str(marker)!r},'w').write('yes')\n")
        key = self.root / "native-observation.key"
        key.write_bytes(b"k" * 32)
        key.chmod(0o644)
        with self.assertRaises(self.module.NativeError) as raised:
            await self.authorized(client, "click")
        self.assertEqual(raised.exception.code, "key_unavailable")
        key.unlink()
        key.symlink_to(self.db)
        with self.assertRaises(self.module.NativeError):
            await self.authorized(client, "click")
        self.assertFalse(marker.exists())

    async def test_request_round_trip_preserves_unicode_and_native_error(self):
        client = self.client("import json, sys\nr = json.load(sys.stdin)\nprint(json.dumps({'ok': True, 'result': r}))\n")
        response = await self.authorized(client, "type", text="한글 世界 😀", expected={"token": "sample"})
        self.assertEqual(response["result"]["text"], "한글 世界 😀")
        self.assertEqual(response["result"]["command"], "type")
        client = self.client("print('{\"ok\":false,\"error\":{\"code\":\"stale_observation\",\"message\":\"expired\"},\"effect_status\":\"not_started\"}')\n")
        self.assertEqual((await self.authorized(client, "click"))["error"]["code"], "stale_observation")

    async def test_nonzero_exit_is_bounded_transport_error(self):
        client = self.client("import sys\nsys.stderr.write('fixture failure')\nsys.exit(7)\n")
        with self.assertRaises(self.module.NativeError) as raised:
            await client.call("status")
        self.assertEqual(raised.exception.code, "native_exit")
        self.assertFalse(raised.exception.effect_uncertain)

    async def test_oversize_stdout_is_rejected_and_child_reaped(self):
        pid_file = self.root / "pid"
        client = self.client(f"import os, sys, time\nopen({str(pid_file)!r},'w').write(str(os.getpid()))\nsys.stdout.write('x' * 100000)\nsys.stdout.flush()\ntime.sleep(30)\n", max_output_bytes=1024)
        with self.assertRaises(self.module.NativeError) as raised:
            await self.authorized(client, "click")
        self.assertEqual(raised.exception.code, "output_limit")
        self.assertTrue(raised.exception.effect_uncertain)
        self.assert_reaped(pid_file)

    async def test_timeout_kills_ignoring_child_and_reports_unknown_effect(self):
        pid_file = self.root / "pid"
        client = self.client(f"import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nopen({str(pid_file)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n", timeout=1, terminate_grace=0.05)
        with self.assertRaises(self.module.NativeError) as raised:
            await self.authorized(client, "type", text="test")
        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(raised.exception.effect_uncertain)
        self.assert_reaped(pid_file)

    async def test_cancellation_waits_for_child_and_preserves_uncertainty(self):
        pid_file = self.root / "pid"
        client = self.client(f"import os, time\nopen({str(pid_file)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n", terminate_grace=0.05)
        task = asyncio.create_task(self.authorized(client, "hotkey", key="return"))
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(pid_file.exists())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as raised:
            await task
        self.assertTrue(raised.exception.effect_uncertain)
        self.assert_reaped(pid_file)

    async def test_invalid_json_response_is_unknown_for_effect_only(self):
        client = self.client("print('not JSON')\n")
        with self.assertRaises(self.module.NativeError) as raised:
            await self.authorized(client, "open", path="/tmp")
        self.assertEqual(raised.exception.code, "protocol")
        self.assertTrue(raised.exception.effect_uncertain)
        with self.assertRaises(self.module.NativeError) as raised:
            await client.call("apps")
        self.assertFalse(raised.exception.effect_uncertain)

    async def test_unsupported_command_never_starts_child(self):
        marker = self.root / "started"
        client = self.client(f"open({str(marker)!r},'w').write('yes')\n")
        with self.assertRaises(self.module.NativeError) as raised:
            await client.call("shell")
        self.assertEqual(raised.exception.code, "invalid_request")
        self.assertFalse(marker.exists())

    async def test_request_bounds_and_backend_refusal_are_preserved_without_retry(self):
        marker = self.root / "started"
        client = self.client(f"open({str(marker)!r},'a').write('1')\nprint('{{\"ok\":false,\"error\":{{\"code\":\"backend_mismatch\"}},\"effect_status\":\"not_started\"}}')\n", backend="peekaboo")
        with self.assertRaises(self.module.NativeError):
            await client.call("observe", value=float("nan"))
        with self.assertRaises(self.module.NativeError):
            await client.call("observe", value="x" * (1024 * 1024))
        self.assertFalse(marker.exists())
        result = await self.authorized(client, "click")
        self.assertEqual(result["error"]["code"], "backend_mismatch")
        self.assertEqual(marker.read_text(), "1")

    async def test_repeated_cancel_kills_descendants_before_allowing_lock_release(self):
        descendant = self.root / "descendant"
        parent_pid = self.root / "parent"
        client = self.client(f"""import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(parent_pid)!r}, 'w').write(str(os.getpid()))
child = os.fork()
if child == 0:
    open({str(descendant)!r}, 'w').write(str(os.getpid()))
time.sleep(30)
""", terminate_grace=0.1)
        async with self.owner_module.DesktopOwnership(self.db) as owner:
            task = asyncio.create_task(client.call_authorized("click", self.payload(owner), owner))
            for _ in range(100):
                if descendant.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(descendant.exists())
            with self.assertRaises(self.owner_module.DesktopOwnershipError):
                owner.release()
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as raised:
                await task
            self.assertTrue(raised.exception.effect_uncertain)
            self.assert_reaped(parent_pid)
            await self.assert_descendant_gone(descendant)
            owner.validate()
        async with self.owner_module.DesktopOwnership(self.db, timeout=0.1) as successor:
            successor.validate()

    async def test_cancellation_during_launch_recovers_handle_before_releasing_owner(self):
        client = self.client("import time\ntime.sleep(30)\n", terminate_grace=0.05)
        created, return_handle = asyncio.Event(), asyncio.Event()
        processes = []
        original_spawn = asyncio.create_subprocess_exec

        async def delayed_handle(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            created.set()
            await return_handle.wait()
            return process

        task = None
        try:
            async with self.owner_module.DesktopOwnership(self.db) as owner:
                with patch("ai_secretary.native.asyncio.create_subprocess_exec", delayed_handle):
                    task = asyncio.create_task(client.call_authorized("click", self.payload(owner), owner))
                    await asyncio.wait_for(created.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0.02)
                    self.assertFalse(task.done(), "a launched child must be recovered before cancellation completes")
                    with self.assertRaises(self.owner_module.DesktopOwnershipError):
                        owner.release()
                    return_handle.set()
                    with self.assertRaises(asyncio.CancelledError) as raised:
                        await task
                    self.assertTrue(raised.exception.effect_uncertain)
                    self.assertIsNotNone(processes[0].returncode)
                    owner.validate()
        finally:
            return_handle.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            for process in processes:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def test_normal_response_still_cleans_inherited_fd_descendant(self):
        descendant = self.root / "descendant"
        client = self.client(f"""import json, os, signal, sys, time
json.load(sys.stdin)
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    open({str(descendant)!r}, 'w').write(str(os.getpid()))
    os.close(0); os.close(1); os.close(2)
    time.sleep(30)
else:
    while not os.path.exists({str(descendant)!r}): time.sleep(.001)
    print('{{"ok":true,"effect_status":"dispatched","backend_outcome":{{"status":"unverified"}}}}')
""", terminate_grace=0.05)
        result = await self.authorized(client, "click")
        self.assertEqual(result["effect_status"], "dispatched")
        self.assertEqual(result["backend_outcome"]["status"], "unverified")
        await self.assert_descendant_gone(descendant)
        async with self.owner_module.DesktopOwnership(self.db, timeout=0.1) as owner:
            owner.validate()

    async def assert_descendant_gone(self, pid_file):
        pid = int(pid_file.read_text())
        for _ in range(200):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)
        os.kill(pid, signal.SIGKILL)
        self.fail("native descendant must be quiescent before transport returns")

    def assert_reaped(self, pid_file):
        self.assertTrue(pid_file.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)


@unittest.skipUnless(os.environ.get("AI_SECRETARY_NATIVE_BINARY"), "opt-in self-owned macOS fixture")
class NativeFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="secretary-ui-test-")
        self.state = Path(self.tmp.name)
        self.native = importlib.import_module("ai_secretary.native")
        self.binary = Path(os.environ["AI_SECRETARY_NATIVE_BINARY"]).resolve()
        self.client = self.native.NativeClient(self.binary, state_dir=self.state,
                                               backend=os.environ.get("AI_SECRETARY_NATIVE_BACKEND", "builtin"))
        status = await self.client.call("status")
        if not status["result"]["accessibility"] or not status["result"]["screen_recording"]:
            self.tmp.cleanup()
            self.skipTest("Accessibility and Screen Recording are required")
        from ai_secretary.authority import AuthorityStore
        from ai_secretary.coordinator import AuthorityRepository
        from ai_secretary.desktop_owner import DesktopOwnership
        from ai_secretary.ledger import Ledger, _boot_id
        self.db = self.state / "operations.sqlite"
        self.store = AuthorityStore(self.db)
        self.owner = await DesktopOwnership(self.db).acquire()
        self.ledger = Ledger(self.db)
        self.ledger.create_task("fixture-task", "fixture-owner", {})
        self.fence = self.ledger.acquire_lease("fixture-owner", ttl=600)
        self.boot_id = _boot_id()
        with self.store.transaction() as unit:
            AuthorityRepository.initialize(unit)
            AuthorityRepository(unit).record_admission("fixture-session", "fixture-admission", "fixture-task", "fixture-input")
            unit.connection.execute("INSERT INTO execution_lifecycle VALUES('fixture-life',1,1,'Running')")
            unit.connection.execute("""INSERT INTO execution_contexts
                VALUES('fixture-run','fixture-session','fixture-admission','fixture-task',
                       'fixture-input','fixture-life',1,1,?,'Active')""", (self.fence,))
        fixture = self.binary.parent / "TestWindow.app/Contents/MacOS/TestWindow"
        self.process = await asyncio.create_subprocess_exec(
            str(fixture), "--state-dir", str(self.state),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.sleep(1)
        self.readback = self.state / "readback.json"
        self.assertTrue(self.readback.exists(), "self-owned fixture must start")

    async def asyncTearDown(self):
        if self.process.returncode is None:
            self.process.terminate()
            await self.process.wait()
        self.ledger.release_lease("fixture-owner", self.fence)
        self.owner.release()
        self.tmp.cleanup()

    def authorization(self):
        operation, attempt = str(uuid.uuid4()), str(uuid.uuid4())
        with self.store.transaction() as unit:
            unit.connection.execute("""INSERT INTO operations
                (operation_id,task_id,owner,effect_json,fingerprint,state,attempt_id)
                VALUES(?,'fixture-task','fixture-owner','{}','fixture','Applying',?)""", (operation, attempt))
            unit.connection.execute("INSERT INTO execution_operations VALUES(?,'fixture-run',?)", (operation, attempt))
        return dict(db_path=str(self.db), run_id="fixture-run", session="fixture-session",
                    admission_id="fixture-admission", task_id="fixture-task", input_event_id="fixture-input",
                    lifecycle_id="fixture-life", epoch=1, run_fence=1, operation_id=operation,
                    attempt_id=attempt, lease_owner="fixture-owner", lease_fence=self.fence,
                    lease_boot_id=self.boot_id, dispatch_lock_fd=self.owner.fd)

    async def observation(self, payload):
        result = await self.client.call_authorized("observe", payload, self.owner, pid=self.process.pid)
        self.assertTrue(result["ok"], result)
        return result["result"]["observation"]

    async def effect(self, command, **params):
        payload = self.authorization()
        result = await self.client.call_authorized(command, payload, self.owner,
                                                  expected=await self.observation(payload), **params)
        if result.get("ok") is not True:
            # Peekaboo preserves uncertainty after posting OS events. This
            # fixture verifies the resulting app state below without retrying.
            self.assertEqual(result.get("effect_status"), "unknown", result)
            self.assertEqual(result.get("error", {}).get("code"), "backend_unconfirmed", result)
            self.assertEqual(result.get("backend_outcome", {}).get("state"), "dispatched_unverified", result)
        return result

    async def test_unicode_real_input_download_and_screenshot(self):
        await self.effect("focus")
        point = json.loads(self.readback.read_text())["textfield_point"]
        await self.effect("click", **point)
        await self.effect("type", text="한글 世界 😀")
        for _ in range(50):
            result = json.loads(self.readback.read_text())
            if result["text"] == "한글 世界 😀":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(result["text"], "한글 世界 😀", "real field readback must match Unicode input")
        await self.effect("click", **result["download_point"])
        for _ in range(50):
            result = json.loads(self.readback.read_text())
            if result["download_path"]:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(Path(result["download_path"]).read_text(),
                         "Synthetic local download created by AI Secretary fixture.\n")
        image = self.state / "fixture.png"
        captured = await self.client.call("observe", pid=self.process.pid, screenshot_path=str(image))
        self.assertTrue(captured["ok"], captured)
        self.assertTrue(image.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(image.stat().st_mode & 0o777, 0o600)

    async def test_tampered_observation_cannot_dispatch(self):
        payload = self.authorization()
        expected = await self.observation(payload)
        expected["window_id"] += 1
        result = await self.client.call_authorized("hotkey", payload, self.owner, expected=expected, key="a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "observation_invalid")
        self.assertEqual(result["effect_status"], "not_started")


if __name__ == "__main__":
    unittest.main()
