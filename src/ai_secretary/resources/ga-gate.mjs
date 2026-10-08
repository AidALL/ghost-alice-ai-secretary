import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
try {
 const input=JSON.parse(fs.readFileSync(0,'utf8'));
 const core=await import(pathToFileURL(path.join(input.ghostRoot,'_shared/derive_downstream_gate.mjs')).href);
 const directory=path.join(input.root,input.platform,input.sessionId);
 const snapshot=core.readSessionIntentSnapshot(directory,input.platform,input.sessionId);
 if(!snapshot||snapshot.degraded||snapshot.latestInput?.event_id!==input.inputEventId) throw new Error('missing/current lineage mismatch');
 const gate=core.deriveDownstreamGateFromDecision(directory,input.platform,input.sessionId,snapshot);
 process.stdout.write(JSON.stringify({decision:gate?.decision==='block'?'block':'allow',inputEventId:snapshot.latestInput.event_id,rules:gate?.rules??[]}));
}catch(error){process.stderr.write(error.message);process.exitCode=1;}
