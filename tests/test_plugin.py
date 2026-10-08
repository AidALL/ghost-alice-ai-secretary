import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class PluginTests(unittest.TestCase):
    def run_node(self, source):
        node = os.environ.get("SECRETARY_NODE") or shutil.which("node")
        if not node: self.skipTest("Node unavailable")
        result = subprocess.run([node, "--input-type=module", "-e", source], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_missing_native_admission_id_blocks_hook_and_tool(self):
        result = self.run_node('''
import plugin from './openclaw-plugin/index.mjs';
const hooks={};let factory;
plugin.register({pluginConfig:{},on:(name,fn)=>{hooks[name]=fn;},registerTool:(f)=>{factory=f;}});
const ctx={sessionKey:'agent:main:test',sessionId:'native-session',inputProvenance:{kind:'external_user'}};
const prompt=await hooks.before_prompt_build({currentUserMessage:'different new request',messages:[]},ctx);
const before=await hooks.before_tool_call({toolName:'secretary_act'},ctx);
let refused=false;
try{await factory(ctx).find(x=>x.name==='secretary_act').execute('call',{});}catch(error){refused=true;}
console.log(JSON.stringify({prompt,before,refused}));
''')
        self.assertEqual(result["prompt"]["toolsAllow"], [])
        self.assertTrue(result["before"]["block"])
        self.assertTrue(result["refused"])

    def test_new_native_session_separates_previous_conversation(self):
        result = self.run_node('''
import {sessionBinding} from './openclaw-plugin/index.mjs';
console.log(JSON.stringify([sessionBinding({sessionKey:'route',sessionId:'first'}),sessionBinding({sessionKey:'route',sessionId:'second'})]));
''')
        self.assertNotEqual(*result)

    def test_openclaw_vision_result_keeps_image_out_of_text_and_details(self):
        result = self.run_node('''
import {formatResult} from './openclaw-plugin/index.mjs';
console.log(JSON.stringify(formatResult({ok:true,result:{observation:{token:'guard'},screenshot:{base64:'aW1hZ2U=',format:'png'}}})));
''')
        self.assertEqual(result["content"][1], {"type": "image", "data": "aW1hZ2U=", "mimeType": "image/png"})
        self.assertNotIn("aW1hZ2U=", result["content"][0]["text"])
        self.assertNotIn("base64", result["details"]["result"]["result"]["screenshot"])

    def test_plugin_uses_core_provider_policy_and_rejects_hosted_route(self):
        result = self.run_node('''
import plugin, {modelPolicy, assertModelRoute} from './openclaw-plugin/index.mjs';
const hooks={};
plugin.register({pluginConfig:{},on:(name,fn)=>{hooks[name]=fn;},registerTool:()=>{}});
let refused=false;
try{assertModelRoute({provider:'hosted-provider'});}catch(error){refused=true;}
console.log(JSON.stringify({policy:modelPolicy,override:hooks.before_model_resolve(),refused}));
''')
        self.assertEqual(result["override"]["providerOverride"], result["policy"]["default_provider"])
        self.assertEqual(result["policy"]["on_prem_providers"], ["ollama"])
        self.assertTrue(result["refused"])

    def test_legacy_symlink_loads_physical_core_resources(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            managed = Path(directory)
            shutil.copytree(repository / "openclaw-plugin", managed / "app/openclaw-plugin")
            shutil.copytree(repository / "src", managed / "app/src")
            (managed / "plugin").symlink_to(managed / "app/openclaw-plugin", target_is_directory=True)
            source = f"""
import {{pathToFileURL}} from 'node:url';
const plugin=await import(pathToFileURL({json.dumps(str(managed / 'plugin/index.mjs'))}).href);
console.log(JSON.stringify({{tools:plugin.tools,provider:plugin.modelPolicy.default_provider}}));
"""
            result = self.run_node(source)
        self.assertIn("secretary_act", result["tools"])
        self.assertEqual(result["provider"], "ollama")

    def test_captured_plugin_loads_canonical_resources_from_configured_source(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(repository / "openclaw-plugin", root / "capture/node_modules/ai-secretary")
            shutil.copytree(repository / "src", root / "managed/app/src")
            source = f"""
import {{pathToFileURL}} from 'node:url';
const plugin=await import(pathToFileURL({json.dumps(str(root / 'capture/node_modules/ai-secretary/index.mjs'))}).href);
let factory;const hooks={{}};
plugin.default.register({{pluginConfig:{{source:{json.dumps(str(root / 'managed/app/src'))}}},on:(name,fn)=>{{hooks[name]=fn;}},registerTool:f=>{{factory=f;}}}});
console.log(JSON.stringify({{tools:factory({{}}).map(tool=>tool.name),provider:hooks.before_model_resolve().providerOverride}}));
"""
            result = self.run_node(source)
        self.assertIn("secretary_act", result["tools"])
        self.assertEqual(result["provider"], "ollama")

if __name__ == "__main__": unittest.main()
