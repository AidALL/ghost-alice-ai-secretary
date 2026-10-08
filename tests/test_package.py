"""Exercise the first-party core without shipping any runtime adapter files."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "src"
        shutil.copytree(Path(__file__).resolve().parents[1] / "src", self.source)
        self.env = {**os.environ, "PYTHONPATH": str(self.source),
                    "SECRETARY_STATE_DIR": str(self.root / "state"),
                    "SECRETARY_GHOST_ROOT": os.environ.get("SECRETARY_GHOST_ROOT", str(Path.home() / "ghost-alice")),
                    "SECRETARY_GA_LEDGER_ROOT": str(self.root / "ga")}
        (self.root / "state").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_actual_mcp_tools_list_works_from_core_alone(self):
        result = subprocess.run([sys.executable, "-m", "ai_secretary.rpc"],
                                input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
                                env=self.env, cwd=self.root, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertNotIn("error", reply, reply)
        action = next(tool for tool in reply["result"]["tools"] if tool["name"] == "secretary_act")
        self.assertIn("operation_id", action["inputSchema"]["required"])
        self.assertIn("expected", action["inputSchema"]["required"])
        self.assertFalse((self.root / "openclaw-plugin").exists())

    def test_real_admitted_ghost_gate_works_from_core_alone(self):
        source = """
import asyncio,json,os
from ai_secretary.governance import Governance
g = Governance(os.environ['SECRETARY_STATE_DIR'])
admission = g.intake('route::isolated', 'original-user-input', 'Inspect local files', 'external_user')
gate = asyncio.run(g.check('route::isolated'))
print(json.dumps({'admission': admission, 'gate': gate}))
"""
        result = subprocess.run([sys.executable, "-c", source], env=self.env, cwd=self.root,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = json.loads(result.stdout)
        self.assertEqual(reply["gate"]["decision"], "allow")
        self.assertEqual(reply["gate"]["task_id"], reply["admission"]["task_id"])
        self.assertEqual(reply["gate"]["inputEventId"], reply["admission"]["inputEventId"])


if __name__ == "__main__":
    unittest.main()
