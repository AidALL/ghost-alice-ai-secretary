#!/usr/bin/env python3
"""Pinned native agent harness protocol smoke with a deterministic Ollama STUB.

This is transport/tool-boundary evidence, not AI/model-inference evidence. Uses
only disposable loopback endpoints and private isolated OpenClaw/secretary state.
No intake is seeded and no provenance or admission identity is manufactured.
"""
import argparse
import datetime
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


PROBE = """import fs from 'node:fs';
export default {id:'secretary-harness-observer', name:'Observer only', register(api){
const record=(hook,event,ctx)=>fs.appendFileSync(api.pluginConfig.trace,
 JSON.stringify({at:new Date().toISOString(),hook,
 event:{currentUserMessage:event.currentUserMessage,currentUserMessageId:event.currentUserMessageId,
 hasCurrentUserMessage:Object.hasOwn(event,'currentUserMessage'),hasCurrentUserMessageId:Object.hasOwn(event,'currentUserMessageId'),
 toolName:event.toolName,args:event.params??event.args},
 ctx:{sessionKey:ctx.sessionKey,sessionId:ctx.sessionId,agentId:ctx.agentId,
 inputProvenance:ctx.inputProvenance,inputProvenanceType:typeof ctx.inputProvenance,
 hasInputProvenance:Object.hasOwn(ctx,'inputProvenance'),activeModel:ctx.activeModel}})+'\\n',{mode:0o600});
api.on('before_prompt_build',(event,ctx)=>{record('before_prompt_build',event,ctx);return {};},{priority:200});
api.on('before_tool_call',(event,ctx)=>{record('before_tool_call',event,ctx);return {};},{priority:200});
api.on('after_tool_call',(event,ctx)=>{record('after_tool_call',event,ctx);},{priority:200});
}};
"""


class Stub(http.server.BaseHTTPRequestHandler):
    """Ollama-shaped protocol fixture; does no model inference."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, payload, status=200, streaming=False):
        raw = (json.dumps(payload) + ("\n" if streaming else "")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/x-ndjson" if streaming else "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/tags":
            self.reply({"models": [{"name": "secretary-protocol-stub:latest", "model": "secretary-protocol-stub:latest", "size": 1}]})
        elif self.path == "/api/version":
            self.reply({"version": "protocol-stub-no-model"})
        else:
            self.reply({"error": "Protocol stub supports only bounded Ollama fixture endpoints"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            self.reply({"error": "fixture body limit"}, 413)
            return
        body = json.loads(self.rfile.read(length) or b"{}")
        record = {"at": utc(), "path": self.path, "body": body, "fixture": "deterministic protocol STUB; no AI inference"}
        self.server.requests.append(record)
        with self.server.capture.open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.path == "/api/show":
            self.reply({"capabilities": ["completion", "tools", "vision"], "model_info": {"stub.context_length": 32768}, "parameters": "num_ctx 32768"})
            return
        if self.path != "/api/chat":
            self.reply({"error": "unsupported fixture endpoint"}, 404)
            return
        tool_result = any(m.get("role") == "tool" for m in body.get("messages", []))
        chat_count = sum(r["path"] == "/api/chat" for r in self.server.requests)
        if chat_count == 1:
            message = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "secretary_status", "arguments": {}}}]}
        else:
            text = "PROTOCOL_STUB_FINAL: tool transport result received; inspect captured payload for success or error." if tool_result else "PROTOCOL_STUB_FINAL: no tool result received."
            message = {"role": "assistant", "content": text}
        self.reply({"model": "secretary-protocol-stub:latest", "created_at": utc(), "message": message, "done": True,
                    "done_reason": "stop", "total_duration": 1, "prompt_eval_count": 1, "eval_count": 1}, streaming=bool(body.get("stream")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, help="npm prefix containing node_modules/openclaw")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--entrypoint", choices=("gateway", "exec"), default="gateway")
    parser.add_argument("--admission-mode", choices=("native", "user-control", "user-run"), default="native")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error("--timeout must be 1..60 seconds")
    if args.admission_mode == "user-run" and args.entrypoint != "gateway":
        parser.error("--admission-mode user-run requires the gateway entrypoint")
    project = Path(__file__).resolve().parents[1]
    package = Path(args.runtime).resolve() / "node_modules/openclaw"
    node = os.environ.get("SECRETARY_NODE", "node")
    ghost_root = str(Path(os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice"))).expanduser().resolve())
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    started = utc()
    clock_started = time.monotonic()
    version = json.loads((package / "package.json").read_text())["version"]
    if version != "2026.9.8":
        raise RuntimeError("Expected pinned OpenClaw 2026.9.8")
    sources = [package / "package.json", package / "openclaw.mjs", package / "docs/cli/agent.md",
               package / "docs/providers/ollama/configuration.md", package / "docs/providers/ollama/advanced.md",
               package / "dist/agent-harness-runtime-CnrNnAqy.mjs", package / "dist/lifecycle-hook-helpers--jVXBi1S.mjs",
               package / "dist/hooks-BKWpaofg.mjs",
               project / "openclaw-plugin/index.mjs", project / "src/ai_secretary/resources/tool-schemas.json",
               project / "src/ai_secretary/resources/model-policy.json", project / "src/ai_secretary/resources/ga-gate.mjs",
               project / "src/ai_secretary/runtime.py", project / "src/ai_secretary/adapters/openclaw.py",
               project / "src/ai_secretary/governance.py",
               project / "src/ai_secretary/service.py", project / "src/ai_secretary/control.py",
               package / "dist/agent-via-gateway-D-L8NW-V.mjs", package / "dist/session-cY6eTKvB.mjs",
               project / "build/desktop-secretary", Path(__file__)]
    provenance = [{"file_path": str(p), "sha256": sha(p), "page": "n/a", "region": "n/a"} for p in sources]
    report = {"status": "Inconclusive", "started_at": started, "runtime_version": version,
              "node_version": subprocess.check_output([node, "--version"], text=True).strip(),
              "kind": "real OpenClaw native agent harness; deterministic loopback Ollama protocol STUB, no AI inference",
              "source_provenance": provenance, "limitations": ["No real model inference", "No real app, file download, GUI, or message-send interaction", "Ollama vision metadata only; no image inference"]}
    report["admission_mode"] = args.admission_mode
    report["status_scope"] = "production USER run admitted native protocol chain" if args.admission_mode == "user-run" else "USER-control-admitted native protocol chain" if args.admission_mode == "user-control" else "automatic native hook admission protocol chain"
    process = None
    gateway = None
    server = None
    thread = None
    try:
        with tempfile.TemporaryDirectory(prefix="secretary-native-harness-") as scratch:
            root = Path(scratch)
            root.chmod(0o700)
            for name in ("worker", "state", "workspace", "probe"):
                (root / name).mkdir(mode=0o700)
            trace = output / "native-hook-trace.jsonl"
            trace.touch(mode=0o600)
            capture = output / "ollama-stub-requests.jsonl"
            capture.touch(mode=0o600)
            probe = root / "probe"
            (probe / "index.mjs").write_text(PROBE)
            write_json(probe / "package.json", {"name": "secretary-harness-observer", "version": "0.0.0", "type": "module", "openclaw": {"extensions": ["./index.mjs"]}})
            write_json(probe / "openclaw.plugin.json", {"id": "secretary-harness-observer", "name": "Observer only", "configSchema": {"type": "object", "properties": {"trace": {"type": "string"}}, "required": ["trace"], "additionalProperties": False}})
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
            server.daemon_threads = True
            server.requests = []
            server.capture = capture
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            names = json.loads((project / "openclaw-plugin/openclaw.plugin.json").read_text())["contracts"]["tools"]
            config = {"gateway": {"mode": "local", "bind": "loopback"},
                      "agents": {"defaults": {"workspace": str(root / "workspace"), "skipBootstrap": True, "model": {"primary": "ollama/secretary-protocol-stub:latest"}}},
                      "models": {"providers": {"ollama": {"baseUrl": f"http://127.0.0.1:{server.server_port}", "api": "ollama", "apiKey": "protocol-stub-local", "models": [{"id": "secretary-protocol-stub:latest", "name": "Protocol STUB (not AI)", "reasoning": False, "input": ["text", "image"], "contextWindow": 32768, "maxTokens": 2048}]}}},
                      "channels": {}, "discovery": {"mdns": {"mode": "off"}}, "logging": {"file": str(root / "private.log")},
                      "plugins": {"enabled": True, "allow": ["ai-secretary", "secretary-harness-observer"], "slots": {"memory": "none"}, "load": {"paths": [str(project / "openclaw-plugin"), str(probe)]},
                                  "entries": {"ai-secretary": {"enabled": True, "hooks": {"allowConversationAccess": True, "allowPromptInjection": True}, "config": {"python": sys.executable, "source": str(project / "src"), "nativeBinary": str(project / "build/desktop-secretary"), "stateDir": str(root / "worker"), "ghostRoot": ghost_root, "gaLedgerRoot": str(root / "ga"), "node": node}},
                                              "secretary-harness-observer": {"enabled": True, "config": {"trace": str(trace)}, "hooks": {"allowConversationAccess": True, "allowPromptInjection": True}}}},
                      "tools": {"allow": names, "toolSearch": False, "codeMode": {"enabled": False}}}
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                gateway_port = sock.getsockname()[1]
            token = "isolated-native-harness-stub-token"
            config["gateway"].update({"port": gateway_port, "auth": {"mode": "token", "token": token}})
            config_path = root / "openclaw.json"
            write_json(config_path, config)
            # Pin every state/config coordinate; do not inherit operator auth directories.
            env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENCLAW_", "OLLAMA_", "SECRETARY_"))}
            env.update({"OPENCLAW_CONFIG_PATH": str(config_path), "OPENCLAW_STATE_DIR": str(root / "state"), "OPENCLAW_NO_RESPAWN": "1", "OLLAMA_API_KEY": "protocol-stub-local"})
            env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
            message = "Inspect only the secretary permission status. No outbound action, GUI operation, or real user files."
            session_key = "agent:main:native-harness-smoke"
            requested_native_id = str(uuid.uuid4()) if args.admission_mode == "user-control" else None
            if requested_native_id:
                # The user-operated control entrypoint admits the supplied task text.
                # Its own generated receipt is used; no SDK fields are fabricated.
                control_env = {**env, "PYTHONPATH": str(project / "src"), "SECRETARY_STATE_DIR": str(root / "worker"),
                               "SECRETARY_GHOST_ROOT": ghost_root, "SECRETARY_GA_LEDGER_ROOT": str(root / "ga"), "SECRETARY_NODE": node}
                control_command = [sys.executable, "-m", "ai_secretary.control", "--session", f"{session_key}::{requested_native_id}", "task", message]
                report["user_control_command"] = control_command
                report["user_control_started_at"] = utc()
                controlled = subprocess.run(control_command, cwd=root, env=control_env, capture_output=True, text=True, timeout=20)
                (output / "user-control-stdout.log").write_text(controlled.stdout)
                (output / "user-control-stderr.log").write_text(controlled.stderr)
                report["user_control_exit_code"] = controlled.returncode
                report["user_control_finished_at"] = utc()
                if controlled.returncode:
                    raise RuntimeError("Actual user control intake command failed")
                report["user_control_receipt"] = json.loads(controlled.stdout)
                report["requested_native_session_id"] = requested_native_id
            command = [node, str(package / "openclaw.mjs"), "agent", "exec", message, "--config", str(config_path), "--state-dir", str(root / "state"), "--cwd", str(root / "workspace"), "--model", "ollama/secretary-protocol-stub:latest", "--code-mode", "direct", "--thinking", "off", "--timeout", str(args.timeout), "--json"]
            if args.entrypoint == "gateway":
                gateway_command = [node, str(package / "openclaw.mjs"), "gateway", "run", "--bind", "loopback", "--port", str(gateway_port), "--auth", "token", "--token", token]
                report["gateway_command"] = gateway_command
                report["gateway_started_at"] = utc()
                with (output / "gateway.log").open("w") as gateway_log:
                    gateway = subprocess.Popen(gateway_command, cwd=root, env=env, stdout=gateway_log, stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic() + 30
                while True:
                    if gateway.poll() is not None:
                        raise RuntimeError("Disposable gateway exited during startup; inspect gateway.log")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{gateway_port}/health", timeout=.5) as response:
                            if response.status == 200:
                                break
                    except (OSError, urllib.error.URLError):
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Disposable gateway did not become ready within 30 seconds")
                    time.sleep(.2)
                report["gateway_ready_at"] = utc()
                command = [node, str(package / "openclaw.mjs"), "agent", "--session-key", session_key, "--message", message, "--model", "ollama/secretary-protocol-stub:latest", "--thinking", "off", "--timeout", str(args.timeout), "--json"]
                if requested_native_id:
                    command.extend(["--session-id", requested_native_id])
                if args.admission_mode == "user-run":
                    env.update({"PYTHONPATH": str(project / "src"), "SECRETARY_STATE_DIR": str(root / "worker"),
                                "SECRETARY_GHOST_ROOT": ghost_root, "SECRETARY_GA_LEDGER_ROOT": str(root / "ga"),
                                "SECRETARY_NATIVE_BINARY": str(project / "build/desktop-secretary"), "SECRETARY_NODE": node})
                    command = [sys.executable, "-m", "ai_secretary.control", "--session", session_key,
                               "run", message, "--openclaw", str(package / "openclaw.mjs")]
            report["command"] = command
            report["isolated_config"] = config
            report["cell_started_at"] = utc()
            with (output / "agent-stdout.log").open("w") as stdout, (output / "agent-stderr.log").open("w") as stderr:
                process = subprocess.Popen(command, cwd=root, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
                try:
                    report["exit_code"] = process.wait(timeout=args.timeout + 8)
                except subprocess.TimeoutExpired:
                    report["timeout"] = True
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
                    report["exit_code"] = process.returncode
            report["cell_finished_at"] = utc()
            operations = root / "worker/operations.sqlite"
            if args.admission_mode == "user-run" and operations.exists():
                deadline = time.monotonic() + 3
                while True:
                    with sqlite3.connect(operations) as db:
                        db.row_factory = sqlite3.Row
                        report["worker_lifetimes"] = [dict(row) for row in db.execute("SELECT worker_sessions.state AS worker_state,execution_contexts.state AS context_state,lifecycle_id FROM worker_sessions JOIN execution_contexts USING(run_id)")]
                        report["execution_lifetimes"] = [dict(row) for row in db.execute("SELECT lifecycle_id,status,enabled FROM execution_lifecycle")]
                    if all(row["status"] == "Stopped" for row in report["execution_lifetimes"]) or time.monotonic() > deadline:
                        break
                    time.sleep(.05)
            if args.admission_mode == "user-run":
                with sqlite3.connect(root / "worker/control-sessions.sqlite") as db:
                    db.row_factory = sqlite3.Row
                    report["actual_control_sessions"] = [dict(row) for row in db.execute("SELECT * FROM conversations")]
                requested_native_id = next((row["native_session"] for row in report["actual_control_sessions"] if row["route"] == session_key), None)
                report["actual_control_native_session_id"] = requested_native_id
            if gateway is not None:
                gateway.terminate()
                try:
                    gateway.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(gateway.pid, signal.SIGKILL)
                    gateway.wait(timeout=3)
                report["gateway_waited"] = gateway.poll() is not None
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            report["stub_cleanup_confirmed"] = not thread.is_alive()
            hooks = [json.loads(line) for line in trace.read_text().splitlines()]
            prompts = [h for h in hooks if h["hook"] == "before_prompt_build"]
            chats = [r for r in server.requests if r["path"] == "/api/chat"]
            admissions = []
            db_path = root / "worker/admissions.sqlite"
            if db_path.exists():
                with sqlite3.connect(db_path) as db:
                    db.row_factory = sqlite3.Row
                    admissions = [dict(row) for row in db.execute("SELECT * FROM admission")]
            report["actual_admissions"] = admissions
            report["actual_prompt_hooks"] = prompts
            report["model_requests"] = len(chats)
            report["tool_names_sent_to_stub"] = [[t.get("function", {}).get("name") for t in r["body"].get("tools", [])] for r in chats]
            results = [m for r in chats for m in r["body"].get("messages", []) if m.get("role") == "tool"]
            report["actual_tool_results_sent_to_stub"] = results
            parsed_results = []
            for result in results:
                try:
                    parsed_results.append(json.loads(result.get("content", "")))
                except (TypeError, json.JSONDecodeError):
                    pass
            report["parsed_tool_results"] = parsed_results
            native_status = any('"accessibility": true' in json.dumps(m) or '"screenRecording": true' in json.dumps(m) for m in parsed_results)
            provenance_ok = bool(prompts) and all(p["event"].get("currentUserMessage") == message and p["event"].get("currentUserMessageId") and p["ctx"].get("inputProvenance", {}).get("kind") == "external_user" for p in prompts)
            binding_ok = bool(admissions) and any(a["session"] == f'{p["ctx"].get("sessionKey", "session")}::{p["ctx"].get("sessionId")}' and a["admission_id"] == p["event"].get("currentUserMessageId") for a in admissions for p in prompts)
            control_binding_ok = bool(requested_native_id and admissions) and any(
                a["session"] == f'{p["ctx"].get("sessionKey", "session")}::{p["ctx"].get("sessionId")}'
                and p["ctx"].get("sessionId") == requested_native_id
                and a["digest"] == hashlib.sha256(message.encode()).hexdigest()
                and (bool(a["event_id"]) if args.admission_mode == "user-run" else a["event_id"] == report.get("user_control_receipt", {}).get("inputEventId"))
                for a in admissions for p in prompts)
            report["checks"] = {"native_admission_context": bool(provenance_ok), "native_route_session_binding": binding_ok, "actual_GA_intake": bool(admissions), "actual_model_protocol_request": bool(chats), "native_permission_status_in_tool_result": native_status, "protocol_final_returned": "PROTOCOL_STUB_FINAL" in (output / "agent-stdout.log").read_text(), "child_waited": process.poll() is not None}
            complete = all(report["checks"].values()) and report["exit_code"] == 0
            report["required_checks"] = list(report["checks"])
            if requested_native_id:
                report["checks"]["actual_user_control_bound_to_native_session"] = control_binding_ok
                complete = control_binding_ok and bool(admissions) and bool(chats) and native_status and report["checks"]["protocol_final_returned"] and report["exit_code"] == 0
                report["required_checks"] = ["actual_user_control_bound_to_native_session", "actual_GA_intake", "actual_model_protocol_request", "native_permission_status_in_tool_result", "protocol_final_returned", "child_waited"]
                if args.admission_mode == "user-run":
                    report["checks"]["production_USER_run_command"] = report["command"][2:4] == ["ai_secretary.control", "--session"] and "run" in report["command"]
                    report["required_checks"].append("production_USER_run_command")
                    complete = complete and report["checks"]["production_USER_run_command"]
                    lifetimes = report.get("execution_lifetimes", [])
                    report["checks"]["bound_worker_lifecycle_drained"] = bool(lifetimes) and all(row["status"] == "Stopped" and row["enabled"] == 0 for row in lifetimes)
                    report["required_checks"].append("bound_worker_lifecycle_drained")
                    complete = complete and report["checks"]["bound_worker_lifecycle_drained"]
            report["status"] = "Passed" if complete else "Failed" if prompts or chats else "Inconclusive"
            report["fail_closed_observation"] = bool(prompts) and not provenance_ok and not admissions and not native_status
            report["boundary_verdicts"] = {
                "native_admission_to_GA": "Passed" if provenance_ok and binding_ok and admissions else "Failed" if prompts else "Inconclusive",
                "Ollama_protocol_roundtrip": "Passed" if chats and report["checks"]["protocol_final_returned"] else "Inconclusive",
                "unknown_admission_closed": "Passed" if report["fail_closed_observation"] else "Inconclusive",
                "worker_to_native_permission_status": "Passed" if native_status else "Inconclusive",
                "AI_model_inference": "Inconclusive (protocol stub; no model)"}
            if requested_native_id:
                report["boundary_verdicts"]["USER_control_to_native_session"] = "Passed" if control_binding_ok else "Failed"
                report["limitations"].append("Native CLI did not supply current user text, admission ID, or classified input provenance; automatic native hook admission remains unproven")
            report["reason"] = "All native protocol boundaries exercised" if complete else "Native admission/tool chain criteria not satisfied; inspect captured actual hook context and protocol/tool results"
            if complete and requested_native_id:
                report["reason"] = "USER control receipt bound to actual native session and status protocol roundtrip passed; automatic SDK admission fields still omitted"
                if args.admission_mode == "user-run":
                    report["reason"] = "Production USER run admitted original argv text, persisted native UUID matched actual hook/tool contexts, and native status protocol roundtrip passed"
            if (root / "private.log").exists():
                shutil.copyfile(root / "private.log", output / "openclaw-private.log")
            report["private_state_removed_after_capture"] = True
    except Exception as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        if gateway is not None and gateway.poll() is None:
            os.killpg(gateway.pid, signal.SIGKILL)
            gateway.wait(timeout=3)
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3)
        report["finished_at"] = utc()
        report["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
        write_json(output / "native-harness-report.json", report)
    print(json.dumps({"status": report["status"], "checks": report.get("checks"), "error": report.get("error"), "report": str(output / "native-harness-report.json")}, ensure_ascii=False))
    return 0 if report["status"] == "Passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
