"""OpenClaw client argv and lifecycle, outside the Secretary core contract."""
import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from ..policy import model_policy
from ..runtime import RunRequest


def resolve_executable(executable=None):
    candidate = executable or os.environ.get("SECRETARY_RUNTIME_EXECUTABLE") or os.environ.get("SECRETARY_OPENCLAW_BINARY")
    if not candidate or not Path(candidate).expanduser().is_file():
        raise ValueError("use the installed launcher or pass --runtime-executable with the pinned executable")
    return str(Path(candidate).expanduser().resolve())


class OpenClawRuntime:
    provider = model_policy()["default_provider"]

    def __init__(self, executable=None):
        self.executable = resolve_executable(executable)

    async def dispatch(self, request: RunRequest) -> int:
        child = subprocess.Popen([self.executable, "agent", "--session-key", request.route,
                                  "--session-id", request.conversation_id, "--message", request.text, "--json"])
        try:
            while child.poll() is None:
                await asyncio.sleep(.05)
            return child.returncode
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Reap the local client even after repeated Ctrl-C. A terminated client
            # does not establish that the Gateway stopped or that an effect failed.
            main_thread = threading.current_thread() is threading.main_thread()
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN) if main_thread else None
            try:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            finally:
                print("Client interrupted; use the Gateway stop control and reconcile pending effects before retrying.", file=sys.stderr)
                if main_thread:
                    signal.signal(signal.SIGINT, previous)
            raise
