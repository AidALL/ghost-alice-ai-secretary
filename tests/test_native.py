import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


class NativeClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        try:
            self.module = importlib.import_module("ai_secretary.native")
        except ModuleNotFoundError:
            self.module = None

    def client(self, source, **kwargs):
        self.assertIsNotNone(self.module, "native subprocess adapter must exist")
        binary = self.root / "fake-native"
        binary.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
        binary.chmod(0o700)
        return self.module.NativeClient(binary, **kwargs)

    async def test_request_round_trip_preserves_unicode_and_native_error(self):
        client = self.client("import json, sys\nr = json.load(sys.stdin)\nprint(json.dumps({'ok': True, 'result': r}))\n")
        response = await client.call("type", text="한글 世界 😀", expected={"token": "sample"})
        self.assertEqual(response["result"]["text"], "한글 世界 😀")
        self.assertEqual(response["result"]["command"], "type")
        client = self.client("print('{\"ok\":false,\"error\":{\"code\":\"stale_observation\",\"message\":\"expired\"},\"effect_status\":\"not_started\"}')\n")
        self.assertEqual((await client.call("click"))["error"]["code"], "stale_observation")

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
            await client.call("click")
        self.assertEqual(raised.exception.code, "output_limit")
        self.assertTrue(raised.exception.effect_uncertain)
        self.assert_reaped(pid_file)

    async def test_timeout_kills_ignoring_child_and_reports_unknown_effect(self):
        pid_file = self.root / "pid"
        client = self.client(f"import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nopen({str(pid_file)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n", timeout=1, terminate_grace=0.05)
        with self.assertRaises(self.module.NativeError) as raised:
            await client.call("type", text="test")
        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(raised.exception.effect_uncertain)
        self.assert_reaped(pid_file)

    async def test_cancellation_waits_for_child_and_preserves_uncertainty(self):
        pid_file = self.root / "pid"
        client = self.client(f"import os, time\nopen({str(pid_file)!r},'w').write(str(os.getpid()))\ntime.sleep(30)\n", terminate_grace=0.05)
        task = asyncio.create_task(client.call("hotkey", key="return"))
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
            await client.call("open", path="/tmp")
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
        self.client = self.native.NativeClient(self.binary, state_dir=self.state)
        status = await self.client.call("status")
        if not status["result"]["accessibility"] or not status["result"]["screen_recording"]:
            self.tmp.cleanup()
            self.skipTest("Accessibility and Screen Recording are required")
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
        self.tmp.cleanup()

    async def observation(self):
        result = await self.client.call("observe", pid=self.process.pid)
        self.assertTrue(result["ok"], result)
        return result["result"]["observation"]

    async def effect(self, command, **params):
        result = await self.client.call(command, expected=await self.observation(), **params)
        self.assertTrue(result["ok"], result)
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
        expected = await self.observation()
        expected["window_id"] += 1
        result = await self.client.call("hotkey", expected=expected, key="a")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "observation_invalid")
        self.assertEqual(result["effect_status"], "not_started")


if __name__ == "__main__":
    unittest.main()
