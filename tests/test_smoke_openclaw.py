"""The HTTP smoke exercises denial, isolation and real subprocess cleanup."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("smoke_openclaw", PROJECT / "scripts/smoke_openclaw.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

GATEWAY = '''
import http.server, json, os, signal, subprocess, sys
from pathlib import Path
port = int(sys.argv[1]); output = Path(sys.argv[2]); status = int(sys.argv[3]); reason = sys.argv[4]
descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
(output / 'descendant.pid').write_text(str(descendant.pid))
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert body['tool'] == 'secretary_status' and body['args'] == {}
        assert self.headers['Authorization'] == 'Bearer isolated-smoke-token'
        with (output / 'requests.jsonl').open('a') as log: log.write(json.dumps(body) + '\\n')
        result = {'ok': False, 'error': {'type': 'tool_call_blocked', 'message': reason}}
        if status == 200: result = {'ok': True, 'result': {'permissions': {}}}
        self.send_response(status); self.end_headers(); self.wfile.write(json.dumps(result).encode())
http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
'''


class ExternalGovernance:
    """Only Ghost-ALICE is substituted; HTTP and child lifetime are real."""
    def __init__(self, root):
        self.ledger = Path(os.environ["SECRETARY_GA_LEDGER_ROOT"])
    def intake(self, *_args):
        return {"inputEventId": "actual-event", "task_id": "actual-task"}
    def identity(self, session):
        return session
    async def check(self, session):
        if session.endswith(":missing"):
            raise PermissionError("no actual user intake bound to this conversation")
        return {"decision": "block" if (self.ledger / "block").exists() else "allow",
                "inputEventId": "actual-event", "task_id": "actual-task"}


class GatewaySmokeTests(unittest.TestCase):
    def run_smoke(self, status=403, reason="Secretary governance unavailable: Secretary host run is absent, closed or stale"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "runtime/node_modules/openclaw"
            package.mkdir(parents=True)
            (package / "package.json").write_text('{"version":"2026.9.8"}')
            ghost = root / "ghost/session-intent-analyzer/scripts"
            ghost.mkdir(parents=True)
            (ghost / "session_intent_ledger.py").write_text(
                "def record_turn(*, root, **kwargs):\n"
                "    root.mkdir(parents=True, exist_ok=True)\n"
                "    (root / 'block').write_text('current block')\n")
            gateway = root / "gateway.py"
            gateway.write_text(GATEWAY)
            output = root / "output"
            captures, children = [], []
            original_popen = subprocess.Popen

            def launch(command, **kwargs):
                if "gateway" not in command:
                    return original_popen(command, **kwargs)
                config = json.loads(Path(kwargs["env"]["OPENCLAW_CONFIG_PATH"]).read_text())
                captures.append((config, kwargs["env"], kwargs.get("start_new_session", False)))
                # Keep even the unfixed implementation's test children in an owned group.
                kwargs["start_new_session"] = True
                child = original_popen([sys.executable, str(gateway), str(config["gateway"]["port"]),
                                        str(output), str(status), reason], **kwargs)
                children.append(child)
                return child

            environment = {"SECRETARY_NODE": sys.executable, "SECRETARY_GHOST_ROOT": str(root / "ghost"),
                           "OPENCLAW_CONFIG_PATH": "/operator/config.json",
                           "OPENCLAW_AGENT_DIR": "/operator/auth",
                           "OLLAMA_BASE_URL": "http://operator-model:11434",
                           "SECRETARY_STATE_DIR": "/operator/secretary"}
            try:
                with patch.dict(os.environ, environment), patch("ai_secretary.governance.Governance", ExternalGovernance), \
                        patch.object(subprocess, "Popen", launch), \
                        patch.object(sys, "argv", ["smoke", "--runtime", str(root / "runtime"), "--output", str(output)]), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = smoke.main()
                report = json.loads((output / "gateway-report.json").read_text())
                calls = [json.loads(line) for line in (output / "requests.jsonl").read_text().splitlines()]
                descendant = int((output / "descendant.pid").read_text())
                # macOS reaps a terminated orphan asynchronously; ps state excludes zombies.
                process_state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                                               capture_output=True, text=True).stdout.strip()
                return code, report, calls, captures[0], process_state
            finally:
                for child in children:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    child.wait(timeout=3)

    def test_admitted_unbound_http_is_a_successful_denial_smoke(self):
        code, report, calls, _capture, _descendant = self.run_smoke()
        self.assertEqual(code, 0, report)
        self.assertTrue(report["all_pass"])
        self.assertEqual([row["case"] for row in report["cases"]],
                         ["admitted-but-unbound-denied", "missing-intake-refused", "actual-current-GA-block"])
        self.assertEqual([row["expected"] for row in report["cases"]], [403] * 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual([row["precondition"]["decision"] for row in report["cases"]],
                         ["allow", "missing-intake", "block"])
        self.assertFalse(any("Native status observation only" in line for line in report["limitations"]))

    def test_an_accidentally_successful_http_call_fails_the_smoke(self):
        code, report, _calls, _capture, _descendant = self.run_smoke(status=200)
        self.assertEqual(code, 1)
        self.assertFalse(report["all_pass"])
        self.assertFalse(any(row["pass"] for row in report["cases"]))

    def test_unrelated_403_does_not_prove_the_host_binding_guard(self):
        code, report, _calls, _capture, _descendant = self.run_smoke(
            reason="Secretary governance unavailable: Cannot read properties of undefined (reading 'message')")
        self.assertEqual(code, 1)
        self.assertFalse(report["all_pass"])
        self.assertFalse(any(row["pass"] for row in report["cases"]))

    def test_background_work_auth_and_child_lifetime_are_isolated(self):
        _code, _report, _calls, (config, environment, own_group), descendant = self.run_smoke()
        self.assertEqual(config["agents"]["defaults"].get("heartbeat", {}).get("every"), "0m")
        self.assertEqual(config.get("skills", {}).get("workshop", {}).get("autonomous", {}).get("mode"), "off")
        self.assertIs(config["models"].get("catalogRefresh", {}).get("enabled"), False)
        self.assertIs(config.get("update", {}).get("checkOnStart"), False)
        self.assertNotEqual(config["models"]["providers"]["ollama"]["baseUrl"], "http://127.0.0.1:11434")
        self.assertNotIn("OPENCLAW_AGENT_DIR", environment)
        self.assertNotIn("OLLAMA_BASE_URL", environment)
        self.assertTrue(own_group)
        self.assertTrue(not descendant or descendant.startswith("Z"), descendant)


if __name__ == "__main__":
    unittest.main()
