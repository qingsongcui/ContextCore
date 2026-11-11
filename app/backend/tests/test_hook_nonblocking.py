"""Run real extension callbacks against an unresponsive synthetic transport."""
import os
from pathlib import Path
import shutil
import subprocess
import pytest


@pytest.mark.parametrize('host', ['omp', 'opencode'])
def test_lifecycle_returns_immediately_and_bounds_pending_network(tmp_path, host):
    node = shutil.which('node')
    if not node or int(subprocess.check_output([node, '--version'], text=True).lstrip('v').split('.')[0]) < 24:
        pytest.skip('Node 24 TypeScript stripping required')
    credential = tmp_path/'.contextcore/credentials'/f'{host}.token'
    credential.parent.mkdir(parents=True)
    credential.write_text('synthetic-token'); credential.chmod(0o600)
    source = Path(__file__).parents[1]/'app/integrations'/('omp_extension.ts' if host == 'omp' else 'opencode_plugin.ts')
    harness = tmp_path/'nonblocking.mjs'
    harness.write_text('''
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const {default:factory} = await import(pathToFileURL(process.argv[2]));
let calls=0, active=0, peak=0, aborted=0;
globalThis.fetch = (_url, options) => {
  calls++; active++; peak=Math.max(peak,active);
  return new Promise((_resolve,reject) => options.signal.addEventListener('abort',()=>{
    active--; aborted++; reject(new Error('synthetic timeout'));
  },{once:true}));
};
let invoke;
const notices=[];
if (process.argv[3] === 'omp') {
  const handlers=new Map();
  factory({on:(event,fn)=>handlers.set(event,fn), registerCommand:()=>{}, sendMessage:()=>notices.push('injected')});
  const ctx={cwd:'/synthetic',sessionManager:{getSessionId:()=> 'test'},ui:{notify:()=>notices.push('notice')}};
  invoke=()=>handlers.get('input')({text:'synthetic input'},ctx);
} else {
  const handlers=await factory({directory:'/synthetic'});
  invoke=()=>handlers['chat.message']({sessionID:'test'},{parts:[{type:'text',text:'synthetic input'}]});
}
for (let i=0;i<1000;i++) assert.equal(invoke(),undefined,'host must not receive a network promise');
assert.equal(calls,1); assert.equal(peak,1);
await new Promise(resolve=>setTimeout(resolve,650));
assert.equal(aborted,1); assert.equal(active,0);
assert.equal(invoke(),undefined); assert.equal(calls,2);
assert.deepEqual(notices,[]);
await new Promise(resolve=>setTimeout(resolve,650));
assert.equal(active,0); assert.equal(aborted,2);
console.log('nonblocking and bounded');
''')
    result = subprocess.run([node,'--experimental-strip-types',str(harness),str(source),host],
                            env={**os.environ,'HOME':str(tmp_path)},capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'nonblocking and bounded' in result.stdout
