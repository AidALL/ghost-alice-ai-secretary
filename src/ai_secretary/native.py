"""Bounded, single-request access to the macOS desktop adapter.

Dispatch confirms only that the OS accepted input. Callers reconcile observable
application state before treating a consequential effect as completed.
"""

import asyncio
import json
import os
from pathlib import Path


READ_COMMANDS = frozenset({"status", "apps", "windows", "observe"})
EFFECT_COMMANDS = frozenset({"focus", "click", "drag", "scroll", "type", "hotkey", "open"})


class NativeError(RuntimeError):
    def __init__(self, code, message, *, effect_uncertain=False):
        super().__init__(message)
        self.code = code
        self.effect_uncertain = effect_uncertain


class NativeCancelledError(asyncio.CancelledError):
    def __init__(self, *, effect_uncertain):
        super().__init__("native request cancelled after child cleanup")
        self.effect_uncertain = effect_uncertain


class NativeClient:
    def __init__(self, binary, *, timeout=15, max_output_bytes=8 * 1024 * 1024,
                 terminate_grace=0.3, state_dir=None):
        if timeout <= 0 or max_output_bytes < 128 or terminate_grace <= 0:
            raise ValueError("native bounds must be positive")
        self.binary = str(Path(binary).expanduser().resolve())
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.terminate_grace = terminate_grace
        self.state_dir = Path(state_dir or os.environ.get("SECRETARY_STATE_DIR")
                              or Path.home() / "Library/Application Support/AI_Secretary")

    async def call(self, command, **params):
        if command not in READ_COMMANDS | EFFECT_COMMANDS or "key_path" in params:
            raise NativeError("invalid_request", "unsupported command or reserved key_path")
        request = {**params, "command": command,
                   "key_path": str((self.state_dir / "native-observation.key").resolve())}
        try:
            data = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise NativeError("invalid_request", "parameters must be finite JSON values") from exc
        if len(data) > 1024 * 1024:
            raise NativeError("invalid_request", "request exceeds 1 MiB")
        process = None
        tasks = []
        effect = command in EFFECT_COMMANDS
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

            async def read_bounded(stream):
                result = bytearray()
                while chunk := await stream.read(16384):
                    result.extend(chunk)
                    if len(result) > self.max_output_bytes:
                        raise NativeError("output_limit", "native output exceeded limit", effect_uncertain=effect)
                return bytes(result)

            async def send_request():
                process.stdin.write(data)
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()

            tasks = [asyncio.create_task(read_bounded(process.stdout)),
                     asyncio.create_task(read_bounded(process.stderr)),
                     asyncio.create_task(send_request()),
                     asyncio.create_task(process.wait())]
            stdout, stderr, _, returncode = await asyncio.wait_for(
                asyncio.gather(*tasks), self.timeout)
            if returncode:
                detail = stderr.decode("utf-8", errors="replace")[:512]
                raise NativeError("native_exit", f"native exited {returncode}: {detail}", effect_uncertain=effect)
            try:
                response = json.loads(stdout)
            except (ValueError, UnicodeDecodeError) as exc:
                raise NativeError("protocol", "native response was not JSON", effect_uncertain=effect) from exc
            if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                raise NativeError("protocol", "native response lacks boolean ok", effect_uncertain=effect)
            return response
        except asyncio.TimeoutError as exc:
            raise NativeError("timeout", "native request timed out", effect_uncertain=effect and process is not None) from exc
        except asyncio.CancelledError as exc:
            raise NativeCancelledError(effect_uncertain=effect and process is not None) from exc
        except OSError as exc:
            raise NativeError("native_io", str(exc)[:512], effect_uncertain=effect and process is not None) from exc
        finally:
            if process is not None:
                cleanup = asyncio.create_task(self._cleanup(process, tasks))
                # Repeated cancellation must not strand a running subprocess.
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()

    async def _cleanup(self, process, tasks):
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), self.terminate_grace)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if process.stdin:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
