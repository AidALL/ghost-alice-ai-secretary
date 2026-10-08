#!/usr/bin/env python3
"""Real pinned Gateway/worker/Ghost-ALICE smoke; no messages or model inference.

Dependencies: Python stdlib, Node, installed OpenClaw, built native helper.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, help="npm prefix containing node_modules/openclaw")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(project / "src"))
    from ai_secretary.governance import Governance
    node = os.environ.get("SECRETARY_NODE", "node")
    ghost_root = str(Path(os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice"))).expanduser().resolve())
    runtime = Path(args.runtime).resolve()
    package = runtime / "node_modules/openclaw"
    if json.loads((package / "package.json").read_text())["version"] != "2026.9.8": raise RuntimeError("wrong runtime pin")
    rows = []
    with tempfile.TemporaryDirectory(prefix="secretary-gateway-") as scratch:
        root = Path(scratch); worker = root / "worker"; worker.mkdir(mode=0o700)
        env = {**os.environ, "SECRETARY_STATE_DIR": str(worker), "SECRETARY_GHOST_ROOT": ghost_root, "SECRETARY_GA_LEDGER_ROOT": str(root / "ga"), "SECRETARY_NODE": node}
        os.environ.update({k: v for k, v in env.items() if k.startswith("SECRETARY_")})
        governance = Governance(worker)
        admitted = governance.intake("agent:main:smoke", "actual-console-smoke-1", "Inspect only the secretary permission status; no outbound action", "external_user")
        with socket.socket() as sock: sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        token = "isolated-smoke-token"
        plugin_config = {"python": sys.executable, "source": str(project / "src"), "nativeBinary": str(project / "build/desktop-secretary"), "stateDir": str(worker), "ghostRoot": ghost_root, "gaLedgerRoot": str(root / "ga"), "node": node}
        names = json.loads((project / "openclaw-plugin/openclaw.plugin.json").read_text())["contracts"]["tools"]
        config = {"gateway": {"mode": "local", "bind": "loopback", "port": port, "auth": {"mode": "token", "token": token}}, "agents": {"defaults": {"workspace": str(root / "workspace"), "skipBootstrap": True, "model": {"primary": "ollama/qwen3-vl:8b"}}}, "models": {"providers": {"ollama": {"baseUrl": "http://127.0.0.1:11434", "api": "ollama", "apiKey": "ollama-local", "models": [{"id": "qwen3-vl:8b", "name": "qwen3-vl:8b", "input": ["text", "image"], "contextWindow": 8192, "maxTokens": 2048}]}}}, "channels": {}, "discovery": {"mdns": {"mode": "off"}}, "logging": {"file": str(root / "private.log")}, "plugins": {"enabled": True, "allow": ["ai-secretary"], "slots": {"memory": "none"}, "entries": {"ai-secretary": {"enabled": True, "config": plugin_config, "hooks": {"allowConversationAccess": True, "allowPromptInjection": True}}}, "load": {"paths": [str(project / "openclaw-plugin")]}}, "tools": {"allow": names}}
        config_path = root / "openclaw.json"; config_path.write_text(json.dumps(config)); config_path.chmod(0o600)
        env.update({"OPENCLAW_CONFIG_PATH": str(config_path), "OPENCLAW_STATE_DIR": str(root / "state"), "OPENCLAW_NO_RESPAWN": "1"})
        with (output / "gateway.log").open("w") as log:
            child = subprocess.Popen([node, str(package / "openclaw.mjs"), "gateway", "run", "--bind", "loopback", "--port", str(port), "--auth", "token", "--token", token], env=env, cwd=root, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 50
                while True:
                    if child.poll() is not None: raise RuntimeError("gateway startup failed: " + str(output / "gateway.log"))
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=.5) as response:
                            if response.status == 200: break
                    except (OSError, urllib.error.URLError): pass
                    if time.monotonic() > deadline: raise TimeoutError("gateway readiness timeout")
                    time.sleep(.2)

                def invoke(label, session, expected):
                    body = {"tool": "secretary_status", "sessionKey": session, "args": {}}
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/tools/invoke", data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
                    try:
                        with urllib.request.urlopen(request, timeout=20) as response: status, result = response.status, json.loads(response.read())
                    except urllib.error.HTTPError as error: status, result = error.code, json.loads(error.read())
                    rows.append({"case": label, "status": status, "expected": expected, "pass": status == expected, "response": result, "verified_at": utc()})

                invoke("real-admission-to-native-status", "agent:main:smoke", 200)
                invoke("missing-intake-refused", "agent:main:missing", 403)
                import importlib.util
                spec = importlib.util.spec_from_file_location("ga_smoke_intake", Path(ghost_root) / "session-intent-analyzer/scripts/session_intent_ledger.py")
                ga = importlib.util.module_from_spec(spec); spec.loader.exec_module(ga)
                ga.record_turn(root=root / "ga", platform="openclaw", session_id=governance.identity("agent:main:smoke"), intent_delta={"model_security_decision": {"decision": "block", "input_event_id": admitted["inputEventId"], "risk_flags": ["synthetic-contract-probe"]}}, expected_input_event_id=admitted["inputEventId"], source="secretary-contract-test")
                invoke("actual-current-GA-block", "agent:main:smoke", 403)
            finally:
                if child.poll() is None:
                    child.terminate()
                    try: child.wait(timeout=8)
                    except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
    report = {"verified_at": utc(), "runtime_version": "2026.9.8", "node_version": subprocess.check_output([node, "--version"], text=True).strip(), "cases": rows, "all_pass": len(rows) == 3 and all(r["pass"] for r in rows), "limitations": ["No model inference", "No actual WeChat/Kakao attachments or outgoing send", "Native status observation only; UI fixture evidence separate"]}
    (output / "gateway-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["all_pass"] else 1

if __name__ == "__main__": raise SystemExit(main())
