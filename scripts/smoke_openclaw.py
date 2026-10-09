#!/usr/bin/env python3
"""Real pinned Gateway denies HTTP calls without a private host-run binding.

Dependencies: Python stdlib, Node, installed OpenClaw and Ghost-ALICE.
Bound native status success is exercised separately by smoke_agent_harness.py.
"""
import argparse
import asyncio
from contextlib import contextmanager
import datetime
import http.server
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request


def utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat()


@contextmanager
def scoped_environment(values):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class NoModel(http.server.BaseHTTPRequestHandler):
    """A disposable rejection endpoint prevents accidental live model access."""
    def log_message(self, *_args): pass

    def reject(self):
        self.server.requests.append({"method": self.command, "path": self.path})
        self.send_response(503)
        self.end_headers()
        self.wfile.write(b'{"error":"model inference disabled in HTTP smoke"}')

    do_GET = do_POST = reject


def stop_gateway(child):
    # The Gateway owns this new process group. Also stop descendants if the
    # parent exits early, and always wait the direct child before deleting state.
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=8)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait(timeout=3)


def invoke(port, token, label, session, precondition, expected_precondition):
    body = {"tool": "secretary_status", "sessionKey": session, "args": {}}
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/tools/invoke", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, result = response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        with error:
            status, result = error.code, json.loads(error.read())
    error = result.get("error", {})
    host_denied = (result.get("ok") is False and error.get("type") == "tool_call_blocked"
                   and "Secretary host run is absent, closed or stale" in error.get("message", ""))
    return {"case": label, "status": status, "expected": 403, "response": result,
            "precondition": precondition, "expected_precondition": expected_precondition,
            "pass": status == 403 and host_denied and precondition.get("decision") == expected_precondition,
            "verified_at": utc()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, help="npm prefix containing node_modules/openclaw")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True, mode=0o700)
    sys.path.insert(0, str(project / "src"))
    from ai_secretary.governance import Governance
    node = os.environ.get("SECRETARY_NODE", "node")
    ghost_root = str(Path(os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice"))).expanduser().resolve())
    runtime = Path(args.runtime).resolve()
    package = runtime / "node_modules/openclaw"
    if json.loads((package / "package.json").read_text())["version"] != "2026.9.8": raise RuntimeError("wrong runtime pin")
    rows = []
    model_requests = []
    child_reaped = False
    with tempfile.TemporaryDirectory(prefix="secretary-gateway-") as scratch:
        root = Path(scratch); worker = root / "worker"; worker.mkdir(mode=0o700)
        # Config, state and auth routes are disposable; never inherit the
        # operator's OpenClaw or Ollama coordinates.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("OPENCLAW_", "OLLAMA_", "SECRETARY_"))}
        secretary_env = {"SECRETARY_STATE_DIR": str(worker), "SECRETARY_GHOST_ROOT": ghost_root,
                         "SECRETARY_GA_LEDGER_ROOT": str(root / "ga"), "SECRETARY_NODE": node}
        env.update(secretary_env)
        env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
        with socket.socket() as sock: sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        token = "isolated-smoke-token"
        plugin_config = {"python": sys.executable, "source": str(project / "src"), "nativeBinary": str(project / "build/desktop-secretary"), "stateDir": str(worker), "ghostRoot": ghost_root, "gaLedgerRoot": str(root / "ga"), "node": node}
        names = json.loads((project / "openclaw-plugin/openclaw.plugin.json").read_text())["contracts"]["tools"]
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NoModel)
        server.daemon_threads = True
        server.requests = model_requests
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {
            "gateway": {"mode": "local", "bind": "loopback", "port": port, "auth": {"mode": "token", "token": token}},
            "agents": {"defaults": {"workspace": str(root / "workspace"), "skipBootstrap": True,
                       "heartbeat": {"every": "0m"}, "model": {"primary": "ollama/secretary-disabled:latest"}}},
            "models": {"catalogRefresh": {"enabled": False}, "providers": {"ollama": {
                "baseUrl": f"http://127.0.0.1:{server.server_port}", "api": "ollama", "apiKey": "isolated-no-model",
                "models": [{"id": "secretary-disabled:latest", "name": "No model inference", "input": ["text"],
                            "contextWindow": 8192, "maxTokens": 2048}]}}},
            "skills": {"workshop": {"autonomous": {"mode": "off"}}}, "update": {"checkOnStart": False},
            "channels": {}, "discovery": {"mdns": {"mode": "off"}}, "logging": {"file": str(root / "private.log")},
            "plugins": {"enabled": True, "allow": ["ai-secretary"], "slots": {"memory": "none"},
                        "entries": {"ai-secretary": {"enabled": True, "config": plugin_config,
                                    "hooks": {"allowConversationAccess": True, "allowPromptInjection": True}}},
                        "load": {"paths": [str(project / "openclaw-plugin")]}},
            "tools": {"allow": names, "toolSearch": False, "codeMode": {"enabled": False}}}
        config_path = root / "openclaw.json"; config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
        env.update({"OPENCLAW_CONFIG_PATH": str(config_path), "OPENCLAW_STATE_DIR": str(root / "state"), "OPENCLAW_NO_RESPAWN": "1"})
        try:
            with scoped_environment(secretary_env), (output / "gateway.log").open("w") as log:
                governance = Governance(worker)
                admitted = governance.intake("agent:main:smoke", "actual-console-smoke-1",
                                            "Inspect only the secretary permission status; no outbound action", "external_user")
                child = subprocess.Popen([node, str(package / "openclaw.mjs"), "gateway", "run", "--bind", "loopback",
                                          "--port", str(port), "--auth", "token", "--token", token],
                                         env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    rows.extend(run_cases(child, port, token, output, governance, admitted, ghost_root, root))
                finally:
                    stop_gateway(child)
                    child_reaped = child.poll() is not None
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
    report = {"verified_at": utc(), "scope": "direct HTTP without private host-run binding",
              "runtime_version": "2026.9.8", "node_version": subprocess.check_output([node, "--version"], text=True).strip(),
              "cases": rows, "model_requests": model_requests, "child_reaped": child_reaped,
              "all_pass": len(rows) == 3 and all(r["pass"] for r in rows) and not model_requests and child_reaped,
              "limitations": ["No model inference, live app access, attachments or outgoing send",
                              "All HTTP cases stop at the absent host-run binding; intake/gate preconditions are checked separately",
                              "Bound native permission status success is covered separately by smoke_agent_harness.py"]}
    report_path = output / "gateway-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    report_path.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["all_pass"] else 1


def run_cases(child, port, token, output, governance, admitted, ghost_root, root):
    # Wait for the real pinned Gateway, then validate each fixture's actual
    # GA state before the HTTP request. HTTP cannot open a run.
    deadline = time.monotonic() + 50
    while True:
        if child.poll() is not None:
            raise RuntimeError("gateway startup failed: " + str(output / "gateway.log"))
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.5) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() > deadline:
            raise TimeoutError("gateway readiness timeout")
        time.sleep(.2)

    rows = [invoke(port, token, "admitted-but-unbound-denied", "agent:main:smoke",
                   asyncio.run(governance.check("agent:main:smoke")), "allow")]
    try:
        missing = asyncio.run(governance.check("agent:main:missing"))
    except PermissionError as error:
        if str(error) != "no actual user intake bound to this conversation":
            raise
        missing = {"decision": "missing-intake", "error": str(error)}
    rows.append(invoke(port, token, "missing-intake-refused", "agent:main:missing", missing, "missing-intake"))
    spec = importlib.util.spec_from_file_location(
        "ga_smoke_intake", Path(ghost_root) / "session-intent-analyzer/scripts/session_intent_ledger.py")
    ga = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ga)
    ga.record_turn(root=root / "ga", platform="openclaw", session_id=governance.identity("agent:main:smoke"),
                   intent_delta={"model_security_decision": {"decision": "block", "input_event_id": admitted["inputEventId"],
                                 "risk_flags": ["synthetic-contract-probe"]}},
                   expected_input_event_id=admitted["inputEventId"], source="secretary-contract-test")
    rows.append(invoke(port, token, "actual-current-GA-block", "agent:main:smoke",
                       asyncio.run(governance.check("agent:main:smoke")), "block"))
    return rows

if __name__ == "__main__": raise SystemExit(main())
