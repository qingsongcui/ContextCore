"""Execute installed hook commands from an actual bundle in an isolated HOME."""
import argparse
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request


def commands(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'command' and isinstance(item, str) and '--hook' in item:
                yield item
            else:
                yield from commands(item)
    elif isinstance(value, list):
        for item in value:
            yield from commands(item)


def run_extension(module, host, root, env, *, revoked=False):
    node = shutil.which('node')
    assert node, 'OMP/OpenCode module verification requires the host Node runtime'
    harness = root/'extension-check.mjs'
    harness.write_text('''
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
const {default:factory} = await import(pathToFileURL(process.argv[2]));
const originalFetch = globalThis.fetch;
let requests = 0, accepted = 0;
globalThis.fetch = async (...args) => {
  requests++;
  const response = await originalFetch(...args);
  if (response.ok) accepted++;
  return response;
};
const notices=[];
let invoke;
if (process.argv[3] === 'omp') {
  const handlers=new Map();
  factory({on:(event,fn)=>handlers.set(event,fn),registerCommand:()=>{},sendMessage:()=>notices.push('injected')});
  const ctx={cwd:process.cwd(),sessionManager:{getSessionId:()=> 'synthetic-bundle-check'},ui:{notify:()=>notices.push('notice')}};
  invoke=()=>handlers.get('input')({text:'synthetic bundle event'},ctx);
} else {
  const handlers=await factory({directory:process.cwd()});
  invoke=()=>handlers['chat.message']({sessionID:'synthetic-bundle-check'},{parts:[{type:'text',text:'synthetic bundle event'}]});
}
assert.equal(invoke(),undefined);
await new Promise(resolve=>setTimeout(resolve,900));
assert.deepEqual(notices,[]);
assert.equal(requests,process.argv[4]==='revoked'?0:1);
assert.equal(accepted,process.argv[4]==='revoked'?0:1);
''')
    result = subprocess.run([node, '--experimental-strip-types', str(harness), str(module), host,
                             'revoked' if revoked else 'active'], cwd=root, env=env,
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def verify(bundle, output):
    output.mkdir(parents=True, exist_ok=True)
    service = bundle.resolve()/'Contents/Resources/service/senrelay-service'
    results = []
    with tempfile.TemporaryDirectory(prefix='senrelay-bundled-hooks-') as directory:
        root = Path(directory); home = root/'home'; home.mkdir(mode=0o700)
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'}
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(CookieJar()))
        csrf = None
        def request(path, payload=None):
            headers = {'Content-Type': 'application/json'}
            if csrf: headers['X-CSRF-Token'] = csrf
            req = urllib.request.Request(base+path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
            with opener.open(req, timeout=10) as response:
                return json.load(response)
        with (output/'service.log').open('w') as log:
            proc = subprocess.Popen([str(service), '--data-dir', str(root/'data'), '--port', str(port)], cwd=root, env=env, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    try:
                        csrf = request('/api/session')['csrf_token']; break
                    except Exception:
                        if proc.poll() is not None: raise RuntimeError('Bundled service exited')
                        time.sleep(.1)
                assert csrf, 'Service did not start'
                for host, surface, config in [
                    ('codex', 'cli', '.codex/hooks.json'), ('cursor', 'ide', '.cursor/hooks.json'),
                    ('antigravity', 'desktop', '.gemini/config/hooks.json'), ('claude-code', 'cli', '.claude/settings.json'),
                    ('omp', 'cli', None), ('opencode', 'cli', None),
                ]:
                    capabilities = ['context_tools', 'continuation', 'lifecycle']
                    plan = request(f'/api/connections/{host}/plans', {'operation': 'install', 'surface': surface, 'capabilities': capabilities})
                    applied = request(f"/api/connections/{host}/plans/{plan['id']}/apply", {'confirmed': True, 'version': plan['version']})
                    assert applied['status'] == 'applied' and applied['readback'] == 'verified'
                    if config:
                        generated = list(commands(json.loads((home/config).read_text())))
                        assert generated, f'{host}: generated hook command missing'
                        command = shlex.split(generated[0]); hook_env = dict(env)
                        while command and '=' in command[0] and command[0].split('=', 1)[0] == 'CONTEXTCORE_BASE_URL':
                            key, value = command.pop(0).split('=', 1); hook_env[key] = value
                        assert command[:2] == [str(service), '--hook'], command
                        payload = ({'conversationId': 'synthetic-bundle-check'} if host == 'antigravity'
                                   else {'session_id': 'synthetic-bundle-check'})
                        ran = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, cwd=root, env=hook_env, timeout=5)
                        assert ran.returncode == 0, ran.stderr
                        json.loads(ran.stdout)
                    else:
                        installed = list(home.rglob('*.ts'))
                        assert len(installed) == 1, f'{host}: expected one installed module'
                        module = root/f'{host}-retained.ts'
                        module.write_bytes(installed[0].read_bytes())
                        run_extension(installed[0], host, root, env)
                    observed = False
                    for _ in range(90):
                        connections = request('/api/connections')
                        state = next(item for item in connections if item['id'] == host)
                        if any(item['capability'] == 'capture' for item in state['discovery']['observations']):
                            observed = True; break
                        time.sleep(.5)
                    assert observed, f'{host}: hook ran but bundled background did not consume notification at candidate port'
                    remove = request(f'/api/connections/{host}/plans', {'operation': 'uninstall', 'surface': surface, 'capabilities': capabilities})
                    removed = request(f"/api/connections/{host}/plans/{remove['id']}/apply", {'confirmed': True, 'version': remove['version']})
                    assert removed['status'] == 'applied'
                    slot = home/'.contextcore/notifications'/f'{host}.json'
                    before = slot.read_bytes() if slot.exists() else None
                    if config:
                        subprocess.run(command, input='{}', text=True, capture_output=True, cwd=root, env=hook_env, timeout=5, check=True)
                    else:
                        run_extension(module, host, root, env, revoked=True)
                    assert (slot.read_bytes() if slot.exists() else None) == before
                    results.append({'host': host, 'plan': 'applied', 'generated_command': 'executed', 'background': 'consumed', 'revoked': 'no_enqueue'})
                    print(json.dumps(results[-1]), flush=True)
                stopped = subprocess.run([str(service), '-m', 'app.cli', 'stop', '--data-dir', str(root/'data')],
                                         cwd=root, env=env, capture_output=True, text=True, timeout=20)
                assert stopped.returncode == 0, stopped.stderr
                proc.wait(timeout=10)
                print('Direct bundled entrypoint: status-safe shutdown verified', flush=True)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=10)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
                (output/'hooks.json').write_text(json.dumps({'bundle': str(bundle), 'results': results, 'scope': 'bundled commands and installed TS modules using host Node; not live host sessions'}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    verify(args.bundle, args.output_dir)
