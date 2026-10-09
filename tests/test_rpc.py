import asyncio
import io
import tempfile
import subprocess
import sys
from pathlib import Path
import json
import base64
import unittest
from unittest.mock import AsyncMock, patch
from ai_secretary import rpc


class RPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_schema_documents_guard_and_identity(self):
        result = await rpc.dispatch({"method": "tools/list"})
        schema = next(x["inputSchema"] for x in result["tools"] if x["name"] == "secretary_act")
        self.assertIn("expected", schema["required"])
        self.assertIn("operation_id", schema["required"])
        self.assertIn("click", schema["properties"]["action"]["enum"])
    async def test_observation_reaches_vision_as_image_not_base64_text(self):
        encoded = base64.b64encode(b"synthetic PNG fixture").decode()
        native = {"ok": True, "result": {"observation": {"token": "guard"}, "screenshot": {"format": "png", "base64": encoded}}}
        async def worker(request):
            if request['kind'] == 'worker_open':
                return {'token': 'private-token', 'transport_id': request['transport_id'], 'context': {'run_id': 'bound-run'}}
            if request['kind'] == 'worker_close': return {'state': 'Stopped'}
            return native
        with patch.object(rpc, "handle", worker):
            result = await rpc.dispatch({"method": "tools/call", "params": {"name": "secretary_observe", "arguments": {"pid": 1}}})
        images = [x for x in result["content"] if x["type"] == "image"]
        self.assertEqual(images, [{"type": "image", "mimeType": "image/png", "data": encoded}])
        self.assertNotIn(encoded, result["content"][0]["text"])
        self.assertIn("guard", result["content"][0]["text"])




class PersistentRPCTests(unittest.IsolatedAsyncioTestCase):
    def worker(self):
        records=[]
        async def handle(request):
            records.append(request)
            if request['kind']=='worker_open': return {'token':'private-token','transport_id':request['transport_id'],'context':{'run_id':'bound-run'}}
            if request['kind']=='worker_close': return {'state':'Stopped'}
            return {'ok':True,'result':{'value':'desktop'}}
        return records,handle

    async def test_multiple_calls_reuse_host_capability_and_close_once(self):
        records,worker=self.worker()
        with patch.object(rpc, 'handle', worker):
            transport=rpc.RPCTransport(session='admitted-session')
            first=await transport.dispatch({'method':'tools/call','params':{'name':'secretary_status','arguments':{}}})
            await transport.dispatch({'method':'tools/call','params':{'name':'secretary_apps','arguments':{}}})
            await transport.close()
            await transport.close()
            with self.assertRaises(PermissionError):
                await transport.dispatch({'method':'tools/call','params':{'name':'secretary_status','arguments':{}}})
        self.assertEqual([r['kind'] for r in records], ['worker_open','call','call','worker_close'])
        self.assertEqual(records[1]['capability'],records[2]['capability'])
        self.assertEqual(records[1]['transport_id'],records[3]['transport_id'])
        self.assertNotIn('private-token',json.dumps(first))

    async def test_incomplete_drain_retains_private_binding_for_cleanup_retry(self):
        records,worker=self.worker()
        attempts=0
        async def incomplete(request):
            nonlocal attempts
            if request['kind']=='worker_close':
                records.append(request)
                attempts+=1
                return {'state':'Draining' if attempts==1 else 'Stopped'}
            return await worker(request)
        with patch.object(rpc,'handle',incomplete):
            transport=rpc.RPCTransport(session='admitted-session')
            await transport.dispatch({'method':'tools/call','params':{'name':'secretary_status','arguments':{}}})
            first=await transport.close()
            second=await transport.close()
        self.assertEqual(first['state'],'Draining')
        self.assertEqual(second['state'],'Stopped')
        self.assertEqual(attempts,2)

    async def test_repeated_cancellation_still_waits_for_host_drain(self):
        records,worker=self.worker()
        called,closing,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def bounded(request):
            if request['kind']=='call':
                records.append(request)
                called.set()
                await asyncio.Event().wait()
            if request['kind']=='worker_close':
                closing.set()
                await release.wait()
            return await worker(request)
        incoming=io.StringIO(json.dumps({'id':1,'method':'tools/call','params':{'name':'secretary_status','arguments':{}}})+'\n')
        with patch.object(rpc,'handle',bounded):
            server=asyncio.create_task(rpc.serve(incoming,io.StringIO(),session='session'))
            await asyncio.wait_for(called.wait(),2)
            server.cancel()
            await asyncio.wait_for(closing.wait(),2)
            server.cancel()
            await asyncio.sleep(0)
            self.assertFalse(server.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await server
        self.assertEqual([r['kind'] for r in records],['worker_open','call','worker_close'])

    async def test_eof_reports_incomplete_drain_instead_of_silent_success(self):
        records,worker=self.worker()
        async def incomplete(request):
            if request['kind']=='worker_close':
                records.append(request)
                return {'state':'Draining'}
            return await worker(request)
        incoming=io.StringIO(json.dumps({'id':1,'method':'tools/call','params':{'name':'secretary_status','arguments':{}}})+'\n')
        with patch.object(rpc,'handle',incomplete):
            with self.assertRaisesRegex(RuntimeError,'Draining'):
                await rpc.serve(incoming,io.StringIO(),session='session')
        self.assertEqual([r['kind'] for r in records],['worker_open','call','worker_close'])

    async def test_invalid_model_arguments_do_not_open_capability(self):
        records,worker=self.worker()
        with patch.object(rpc, 'handle', worker):
            transport=rpc.RPCTransport(session='admitted-session')
            with self.assertRaises(Exception):
                await transport.dispatch({'method':'tools/call','params':{'name':'secretary_status','arguments':{'session':'spoofed'}}})
            with self.assertRaises(Exception):
                await transport.dispatch({'method':'tools/call','params':{'name':'worker_open','arguments':{}}})
            await transport.close()
        self.assertEqual(records,[])

    async def test_eof_closes_the_persistent_transport_in_the_same_loop(self):
        records,worker=self.worker()
        loops=[]
        async def measured(request):
            loops.append(id(asyncio.get_running_loop()))
            return await worker(request)
        incoming=io.StringIO(''.join(json.dumps({'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'secretary_status','arguments':{}}})+'\n' for i in (1,2)))
        outgoing=io.StringIO()
        with patch.object(rpc,'handle',measured):
            await rpc.serve(incoming,outgoing,session='admitted-session')
        self.assertEqual(len(set(loops)),1)
        self.assertEqual([r['kind'] for r in records],['worker_open','call','call','worker_close'])
        self.assertEqual([json.loads(line)['id'] for line in outgoing.getvalue().splitlines()],[1,2])

    async def test_cancellation_revokes_open_transport_before_propagating(self):
        records,worker=self.worker()
        started=asyncio.Event()
        async def slow(request):
            if request['kind']=='call':
                records.append(request)
                started.set()
                await asyncio.Event().wait()
            return await worker(request)
        incoming=io.StringIO(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'secretary_status','arguments':{}}})+'\n')
        with patch.object(rpc,'handle',slow):
            server=asyncio.create_task(rpc.serve(incoming,io.StringIO(),session='admitted-session'))
            await asyncio.wait_for(started.wait(),2)
            server.cancel()
            with self.assertRaises(asyncio.CancelledError): await server
        self.assertEqual([r['kind'] for r in records],['worker_open','call','worker_close'])


class RealMCPAuthorityTests(unittest.TestCase):
    def test_real_stdio_eof_closes_durable_context_on_one_event_loop(self):
        repository=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            child_source=r"""
import json,sys,asyncio,os
os.environ['SECRETARY_SESSION']='session'
from pathlib import Path
from ai_secretary import service,rpc
from ai_secretary.ledger import Ledger
from ai_secretary.coordinator import AuthorityRepository
root=Path(sys.argv[1])
ledger=Ledger(root/'operations.sqlite')
ledger.create_task('task','operator','local read fixture')
with ledger.store.transaction() as uow:
 AuthorityRepository.initialize(uow)
 AuthorityRepository(uow).record_admission('session','admission','task','input')
loops=[]
async def gate(session):
 loops.append(id(asyncio.get_running_loop()))
 return {'decision':'allow','admission_id':'admission','task_id':'task','inputEventId':'input'}
class Native:
 async def call(self,command,**params): return {'ok':True,'result':{'command':command}}
secretary=service.Secretary(ledger,Native(),gate)
service.state_dir=lambda:root
service.build_services=lambda root=None:secretary
rpc.main()
with ledger.store.read() as db:
 result={'caps':[row[0] for row in db.execute('SELECT state FROM worker_sessions')],'contexts':[row[0] for row in db.execute('SELECT state FROM execution_contexts')],'lifecycles':[row[0] for row in db.execute('SELECT l.status FROM execution_lifecycle l JOIN execution_contexts c USING(lifecycle_id)')],'loops':len(set(loops))}
print(json.dumps(result),file=sys.stderr)
"""
            requests=''.join(json.dumps({'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':name,'arguments':{}}})+'\n' for i,name in [(1,'secretary_status'),(2,'secretary_apps')])
            result=subprocess.run([sys.executable,'-c',child_source,directory],input=requests,capture_output=True,text=True,cwd=repository,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        replies=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([reply['id'] for reply in replies],[1,2])
        self.assertTrue(all('result' in reply for reply in replies),repr(replies))
        lifecycle=json.loads(result.stderr)
        self.assertEqual(lifecycle,{'caps':['Closed'],'contexts':['Closed'],'lifecycles':['Stopped'],'loops':1})


if __name__ == "__main__": unittest.main()
