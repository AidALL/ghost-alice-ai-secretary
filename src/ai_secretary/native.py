"""Bounded, single-request access to the macOS desktop adapter.

Dispatch confirms only that the OS accepted input. Callers reconcile observable
application state before treating a consequential effect as completed.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import signal
import stat

from .desktop_owner import DesktopOwner, DesktopOwnershipError


READ_COMMANDS = frozenset({"status", "apps", "windows", "observe"})
EFFECT_COMMANDS = frozenset({"focus", "click", "drag", "scroll", "type", "hotkey", "open"})
AUTHORIZATION_FIELDS = frozenset({"db_path", "run_id", "session", "admission_id", "task_id",
    "input_event_id", "lifecycle_id", "epoch", "run_fence", "operation_id", "attempt_id",
    "lease_owner", "lease_fence", "lease_boot_id", "dispatch_lock_fd"})
COUNTER_FIELDS = frozenset({"epoch", "run_fence", "lease_fence", "dispatch_lock_fd"})
RESERVED_FIELDS = AUTHORIZATION_FIELDS | {"key_path", "_authorization", "ownership", "backend"}


class NativeError(RuntimeError):
    def __init__(self, code, message, *, effect_uncertain=False, cleanup_complete=True):
        super().__init__(message)
        self.code = code
        self.effect_uncertain = effect_uncertain
        self.cleanup_complete = cleanup_complete


class NativeCancelledError(asyncio.CancelledError):
    def __init__(self, *, effect_uncertain):
        super().__init__("native request cancelled after child cleanup")
        self.effect_uncertain = effect_uncertain


class NativeClient:
    def __init__(self, binary, *, timeout=15, max_output_bytes=8 * 1024 * 1024,
                 terminate_grace=0.3, state_dir=None, backend="builtin"):
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0
                or type(max_output_bytes) is not int or max_output_bytes < 128
                or type(terminate_grace) not in (int, float)
                or not math.isfinite(terminate_grace) or terminate_grace <= 0):
            raise ValueError("native bounds must be positive")
        if backend not in ("builtin", "peekaboo"):
            raise ValueError("native backend must be builtin or peekaboo")
        self.binary = str(Path(binary).expanduser().resolve())
        self.timeout = timeout
        self.max_output_bytes = max_output_bytes
        self.terminate_grace = terminate_grace
        self.backend = backend
        self.state_dir = Path(os.path.abspath(Path(state_dir or os.environ.get("SECRETARY_STATE_DIR")
                              or Path.home() / "Library/Application Support/AI_Secretary").expanduser()))

    @staticmethod
    def _validate_request(command, params):
        if command not in READ_COMMANDS | EFFECT_COMMANDS or RESERVED_FIELDS.intersection(params):
            raise NativeError("invalid_request", "unsupported command or reserved private field")

    async def call(self, command, **params):
        """Read-only public transport; action-ready observations require authority."""
        self._validate_request(command, params)
        if command in EFFECT_COMMANDS:
            raise NativeError("authorization_required", "effects require server dispatch authority")
        return await self._request(command, params)

    async def call_authorized(self, command, authorization_payload, dispatch_owner, **params):
        """Internal server entrypoint. Never accept the payload from tool arguments."""
        self._validate_request(command, params)
        if type(authorization_payload) is not dict or set(authorization_payload) != AUTHORIZATION_FIELDS:
            raise NativeError("authorization_invalid", "authorization fields must match the native contract")
        payload = dict(authorization_payload)
        for name in AUTHORIZATION_FIELDS - COUNTER_FIELDS:
            value = payload[name]
            try:
                valid = type(value) is str and bool(value) and len(value.encode("utf-8")) <= 4096
            except UnicodeError:
                valid = False
            if not valid:
                raise NativeError("authorization_invalid", "authorization identity is incomplete or too long")
        for name in COUNTER_FIELDS:
            limit = 2**31 - 1 if name == "dispatch_lock_fd" else 2**53 - 1
            if type(payload[name]) is not int or not 0 < payload[name] <= limit:
                raise NativeError("authorization_invalid", "authorization counters must be positive safe integers")
        if not isinstance(dispatch_owner, DesktopOwner):
            raise NativeError("authorization_invalid", "server must retain actual OS desktop ownership")
        try:
            if (payload["db_path"] != str(dispatch_owner.db_path)
                    or payload["dispatch_lock_fd"] != dispatch_owner.fd):
                raise NativeError("authorization_invalid", "authorization does not identify retained desktop owner")
            dispatch_owner._begin_dispatch()
        except (DesktopOwnershipError, RuntimeError) as exc:
            raise NativeError("authorization_invalid", str(exc)) from exc
        cleanup_complete = True
        try:
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(raw) > 16 * 1024:
                raise NativeError("authorization_invalid", "authorization exceeds 16 KiB")
            envelope = {"payload": base64.b64encode(raw).decode("ascii"),
                        "token": hmac.new(self._observation_key(), raw, hashlib.sha256).hexdigest()}
            return await self._request(command, {**params, "_authorization": envelope},
                                       pass_fds=(dispatch_owner.fd,))
        except NativeError as exc:
            cleanup_complete = exc.cleanup_complete
            raise
        finally:
            if cleanup_complete:
                dispatch_owner._end_dispatch()

    def _observation_key(self):
        parent = descriptor = None
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            metadata = os.fstat(parent)
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise NativeError("key_unavailable", "native state directory must be owned and mode 700")
            flags = os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                descriptor = os.open("native-observation.key", os.O_RDWR | os.O_CREAT | os.O_EXCL | flags,
                                     0o600, dir_fd=parent)
            except FileExistsError:
                descriptor = os.open("native-observation.key", os.O_RDONLY | flags, dir_fd=parent)
            else:
                os.fchmod(descriptor, 0o600)
                key = os.urandom(32)
                if os.write(descriptor, key) != 32:
                    raise NativeError("key_unavailable", "native key could not be initialized")
                os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
            metadata = os.fstat(descriptor)
            if (metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size != 32):
                raise NativeError("key_unavailable", "native key must be an owned mode-600 32-byte regular file")
            key = os.read(descriptor, 33)
            if len(key) != 32:
                raise NativeError("key_unavailable", "native key could not be read completely")
            return key
        except OSError as exc:
            raise NativeError("key_unavailable", "private native key is unavailable") from exc
        finally:
            for opened in (descriptor, parent):
                if opened is not None:
                    os.close(opened)

    async def _request(self, command, params, *, pass_fds=()):
        request = {**params, "command": command,
                   "backend": self.backend,
                   "key_path": str(self.state_dir / "native-observation.key")}
        try:
            data = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError) as exc:
            raise NativeError("invalid_request", "parameters must be finite JSON values") from exc
        if len(data) > 1024 * 1024:
            raise NativeError("invalid_request", "request exceeds 1 MiB")
        process = None
        tasks = []
        effect = command in EFFECT_COMMANDS
        try:
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                self.binary, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                pass_fds=pass_fds, start_new_session=True))
            try:
                process = await asyncio.wait_for(asyncio.shield(spawn), self.timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                # A cancellation during subprocess creation must first recover
                # its handle; otherwise a launched child can escape cleanup.
                process, _ = await self._settle(spawn)
                raise

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
                try:
                    _, cancelled = await self._settle(cleanup)
                except OSError as exc:
                    raise NativeError("cleanup", "native process group could not be reaped",
                                      effect_uncertain=effect, cleanup_complete=False) from exc
                if cancelled:
                    raise NativeCancelledError(effect_uncertain=effect)

    @staticmethod
    async def _settle(task):
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        return task.result(), cancelled

    @staticmethod
    def _signal_group(process, value):
        try:
            os.killpg(process.pid, value)
        except ProcessLookupError:
            return False
        return True

    async def _cleanup(self, process, tasks):
        # Descendants can retain both pipes and the inherited flock even after
        # the direct child returns. Always terminate the owned session group.
        if self._signal_group(process, signal.SIGTERM):
            deadline = asyncio.get_running_loop().time() + self.terminate_grace
            while self._signal_group(process, 0):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self._signal_group(process, signal.SIGKILL)
                    break
                await asyncio.sleep(min(0.01, remaining))
        await process.wait()
        while self._signal_group(process, 0):
            # Keep ownership pinned while the OS closes inherited descriptors.
            await asyncio.sleep(0.01)
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
