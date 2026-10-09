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
const ctx={sessionKey:'agent:main:test',sessionId:'native-session',runId:'run-one',assertInvocationCurrent(){},inputProvenance:{kind:'external_user'}};
const prompt=await hooks.before_prompt_build({currentUserMessage:'different new request',messages:[]},ctx);
const before=await hooks.before_tool_call({toolName:'secretary_act'},ctx);
let refused=false;
try{await factory.create(ctx).find(x=>x.name==='secretary_act').execute('call',{});}catch(error){refused=true;}
console.log(JSON.stringify({prompt,before,refused}));
''')
        self.assertEqual(result["prompt"]["toolsAllow"], [])
        self.assertTrue(result["before"]["block"])
        self.assertTrue(result["refused"])

    def test_legacy_installed_config_uses_owned_worker_lifetime_wrapper(self):
        with tempfile.TemporaryDirectory() as directory:
            managed = Path(directory) / "managed"
            (managed / "app/src").mkdir(parents=True)
            (managed / "bin").mkdir()
            worker = managed / "bin/ai-secretary-worker"
            worker.write_text("#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{\"ok\":true,\"result\":{\"owned_wrapper\":true}}'\n")
            worker.chmod(0o700)
            result = self.run_node(f'''import {{worker}} from './openclaw-plugin/index.mjs';
console.log(JSON.stringify(await worker({{python:'/no-bare-python',source:{json.dumps(str(managed / 'app/src'))},stateDir:{json.dumps(str(managed))},nativeBinary:'/unused',ghostRoot:'/unused'}},{{kind:'gate',session:'fixture'}})));''')
        self.assertTrue(result["owned_wrapper"])

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
console.log(JSON.stringify({{tools:factory.create({{assertInvocationCurrent(){{}}}}).map(tool=>tool.name),provider:hooks.before_model_resolve().providerOverride}}));
"""
            result = self.run_node(source)
        self.assertIn("secretary_act", result["tools"])
        self.assertEqual(result["provider"], "ollama")




class BoundPluginLifecycleTests(unittest.TestCase):
    run_node = PluginTests.run_node
    def lifecycle(self, scenario):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src/ai_secretary"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            shutil.copytree(repository / "src/ai_secretary/resources", package / "resources")
            (package / "service.py").write_text("""
import json,os,sys,time
from pathlib import Path
request=json.load(sys.stdin)
log=Path(os.environ['SECRETARY_STATE_DIR'])/'requests.jsonl'
with log.open('a') as stream: stream.write(json.dumps(request)+'\\n')
kind=request['kind']
if kind=='worker_open' and (Path(os.environ['SECRETARY_STATE_DIR'])/'slow-open').exists(): time.sleep(0.3)
if kind=='worker_open': result={'token':'private-'+request['transport_id'],'transport_id':request['transport_id'],'context':{'run_id':'core-'+request['transport_id']},'expires_at':9999999999}
elif kind=='gate': result={'decision':'allow','task_id':'task-one'}
elif kind=='worker_close':
 marker=Path(os.environ['SECRETARY_STATE_DIR'])/'partial-drain'
 if marker.exists():
  marker.unlink()
  result={'state':'Draining','disposition':'timeout'}
 else: result={'state':'Stopped','disposition':'drained'}
elif kind=='intake': result={'decision':'allow'}
else:
 if request.get('args',{}).get('pid')==31337: time.sleep(3)
 result={'ok':True,'result':{'value':'desktop-result'}}
print(json.dumps({'ok':True,'result':result}))
""")
            config = {"python": os.environ.get("SECRETARY_PYTHON", __import__('sys').executable), "source": str(root / "src"), "stateDir": str(root), "nativeBinary": "/unused", "ghostRoot": "/unused"}
            source = """
import plugin from './openclaw-plugin/index.mjs';
import fs from 'node:fs';
const hooks={};let descriptor;
const config=CONFIG;
plugin.register({pluginConfig:config,on:(name,fn)=>{hooks[name]=fn;},registerTool:f=>{descriptor=f;}});
const hook=(runId)=>({sessionKey:'route',sessionId:'conversation',runId,inputProvenance:{kind:'external_user'},hookInvocation:{assertActive(){}}});
const event={currentUserMessage:'collect local files',currentUserMessageId:'human-message-one'};
const toolContext=()=>({sessionKey:'route',sessionId:'conversation',assertInvocationCurrent(){},activeModel:{provider:'ollama'}});
const requests=()=>fs.existsSync(config.stateDir+'/requests.jsonl')?fs.readFileSync(config.stateDir+'/requests.jsonl','utf8').trim().split('\\n').filter(Boolean).map(JSON.parse):[];
SCENARIO
""".replace('CONFIG',json.dumps(config)).replace('SCENARIO',scenario)
            return self.run_node(source)

    def test_unopened_run_without_intake_error_reaches_the_host_binding_guard(self):
        result = self.lifecycle("""
const before=await hooks.before_tool_call({toolName:'secretary_status'},hook('unopened'));
const tool=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
let message=null;try{await tool.execute('unbound',{});}catch(error){message=error.message;}
console.log(JSON.stringify({before,message,requests:requests()}));
""")
        self.assertTrue(result['before']['block'])
        self.assertEqual(result['before']['blockReason'],
                         'Secretary governance unavailable: Secretary host run is absent, closed or stale')
        self.assertEqual(result['message'], 'Secretary host run is absent, closed or stale')
        self.assertEqual(result['requests'], [])

    def test_version_two_factory_and_same_run_rebuild_reuse_private_binding(self):
        result = self.lifecycle("""
const prompt=await hooks.before_prompt_build(event,hook('r1'));
await hooks.before_prompt_build(event,hook('r1'));
const tool=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
const result=await tool.execute('call',{});
await hooks.agent_end({},hook('r1'));
const records=requests();
const call=records.find(r=>r.kind==='call');
console.log(JSON.stringify({version:descriptor.contextVersion,kinds:records.map(r=>r.kind),privateHeader:!!call.capability&&!!call.transport_id,output:JSON.stringify({prompt,result}),same:records.filter(r=>r.kind==='worker_open').length}));
""")
        self.assertEqual(result['version'], 2)
        self.assertEqual(result['same'], 1)
        self.assertTrue(result['privateHeader'])
        self.assertEqual(result['kinds'].count('worker_close'), 1)
        self.assertNotIn('private-', result['output'])
        self.assertNotIn('transport_id', result['output'])

    def test_new_run_revokes_retained_tool_and_late_end_preserves_current_run(self):
        result = self.lifecycle("""
await hooks.before_prompt_build(event,hook('r1'));
const old=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
await old.execute('c1',{});
await hooks.before_prompt_build({...event,currentUserMessageId:'human-message-two'},hook('r2'));
let stale=false;try{await old.execute('old',{});}catch{stale=true;}
await hooks.agent_end({},hook('r1'));
const current=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
await current.execute('c2',{});
await hooks.agent_end({},hook('r2'));
let closed=false;try{await current.execute('closed',{});}catch{closed=true;}
const afterEnd=await hooks.before_prompt_build(event,hook('r2'));
console.log(JSON.stringify({stale,closed,blocked:afterEnd.toolsAllow,opens:requests().filter(r=>r.kind==='worker_open').length,closes:requests().filter(r=>r.kind==='worker_close').length}));
""")
        self.assertTrue(result['stale'])
        self.assertTrue(result['closed'])
        self.assertEqual(result['blocked'], [])
        self.assertEqual(result['opens'], 2)
        self.assertEqual(result['closes'], 2)

    def test_model_authority_injection_rejected_without_worker_dispatch(self):
        result = self.lifecycle("""
await hooks.before_prompt_build(event,hook('r1'));
const tool=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
const count=requests().length;
let refused=false;try{await tool.execute('call',{capability:'forged',kind:'worker_open'});}catch{refused=true;}
console.log(JSON.stringify({refused,unchanged:requests().length===count}));
""")
        self.assertTrue(result['refused'])
        self.assertTrue(result['unchanged'])

    def test_late_old_prompt_failure_does_not_poison_current_run(self):
        result = self.lifecycle("""
await hooks.before_prompt_build(event,hook('r1'));
await hooks.before_prompt_build({...event,currentUserMessageId:'human-message-two'},hook('r2'));
const rejected=await hooks.before_prompt_build(event,hook('r1'));
const tool=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
let currentWorked=false;try{await tool.execute('current',{});currentWorked=true;}catch{}
await hooks.agent_end({},hook('r2'));
console.log(JSON.stringify({rejected:rejected.toolsAllow,currentWorked}));
""")
        self.assertEqual(result['rejected'],[])
        self.assertTrue(result['currentWorked'])

    def test_late_old_end_retries_incomplete_drain_without_closing_current_run(self):
        result = self.lifecycle("""
await hooks.before_prompt_build(event,hook('r1'));
fs.writeFileSync(config.stateDir+'/partial-drain','');
await hooks.before_prompt_build({...event,currentUserMessageId:'human-message-two'},hook('r2'));
await hooks.agent_end({},hook('r1'));
const tool=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
await tool.execute('current',{});
await hooks.agent_end({},hook('r2'));
const closes=requests().filter(r=>r.kind==='worker_close');
console.log(JSON.stringify({closes:closes.length,oldSame:closes[0].transport_id===closes[1].transport_id,currentDifferent:closes[2].transport_id!==closes[1].transport_id}));
""")
        self.assertEqual(result['closes'],3)
        self.assertTrue(result['oldSame'])
        self.assertTrue(result['currentDifferent'])

    def test_cancelled_call_is_reaped_and_exact_run_still_drains(self):
        result = self.lifecycle("""
await hooks.before_prompt_build(event,hook('r1'));
const tool=descriptor.create(toolContext()).find(t=>t.name==='secretary_observe');
const controller=new AbortController();
const start=Date.now();const invocation=tool.execute('call',{pid:31337},controller.signal);
setTimeout(()=>controller.abort(),60);
let cancelled=false;try{await invocation;}catch{cancelled=true;}
await hooks.agent_end({},hook('r1'));
console.log(JSON.stringify({cancelled,elapsed:Date.now()-start,closed:requests().filter(r=>r.kind==='worker_close').length}));
""")
        self.assertTrue(result['cancelled'])
        self.assertLess(result['elapsed'], 2500)
        self.assertEqual(result['closed'], 1)

    def test_missing_required_host_guard_cannot_invoke_worker(self):
        result = self.lifecycle("""
await hooks.before_prompt_build(event,hook('r1'));
const ctx=toolContext();delete ctx.assertInvocationCurrent;
const tool=descriptor.create(ctx).find(t=>t.name==='secretary_status');
const count=requests().length;
let refused=false;try{await tool.execute('unguarded',{});}catch{refused=true;}
await hooks.agent_end({},hook('r1'));
console.log(JSON.stringify({refused,noCall:!requests().slice(count).some(r=>r.kind==='call')}));
""")
        self.assertTrue(result['refused'])
        self.assertTrue(result['noCall'])

    def test_concurrent_same_run_prompt_builds_open_one_capability(self):
        result = self.lifecycle("""
await Promise.all([hooks.before_prompt_build(event,hook('r1')),hooks.before_prompt_build(event,hook('r1'))]);
const tool=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
await tool.execute('call',{});
await hooks.agent_end({},hook('r1'));
console.log(JSON.stringify({opens:requests().filter(r=>r.kind==='worker_open').length,closes:requests().filter(r=>r.kind==='worker_close').length}));
""")
        self.assertEqual(result['opens'],1)
        self.assertEqual(result['closes'],1)

    def test_end_during_capability_open_never_publishes_live_binding(self):
        result = self.lifecycle("""
fs.writeFileSync(config.stateDir+'/slow-open','');
const prompt=hooks.before_prompt_build(event,hook('r1'));
const deadline=Date.now()+2000;
while(!requests().some(r=>r.kind==='worker_open')){if(Date.now()>deadline)throw new Error('fixture open did not start');await new Promise(resolve=>setTimeout(resolve,5));}
const ending=hooks.agent_end({},hook('r1'));
const built=await prompt;await ending;
const tool=descriptor.create(toolContext()).find(t=>t.name==='secretary_status');
let refused=false;try{await tool.execute('call',{});}catch{refused=true;}
console.log(JSON.stringify({refused,blocked:built.toolsAllow,calls:requests().filter(r=>r.kind==='call').length,closed:requests().filter(r=>r.kind==='worker_close').length}));
""")
        self.assertTrue(result['refused'])
        self.assertEqual(result['blocked'],[])
        self.assertEqual(result['calls'],0)
        self.assertEqual(result['closed'],1)

    def test_invocation_guard_rechecks_before_publishing_worker_result(self):
        result = self.lifecycle("""
await hooks.before_prompt_build(event,hook('r1'));
let valid=true,checks=0;
const ctx={...toolContext(),assertInvocationCurrent(){checks++;if(!valid)throw new Error('stale host');}};
const tool=descriptor.create(ctx).find(t=>t.name==='secretary_observe');
const invocation=tool.execute('call',{pid:31337});
setTimeout(()=>{valid=false;},100);
let refused=false;try{await invocation;}catch{refused=true;}
await hooks.agent_end({},hook('r1'));
console.log(JSON.stringify({refused,checks}));
""")
        self.assertTrue(result['refused'])
        self.assertGreaterEqual(result['checks'], 2)


if __name__ == "__main__": unittest.main()
