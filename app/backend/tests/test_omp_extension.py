"""Execute OMP's factory without inference, private sessions, or real credentials."""
import os
from pathlib import Path
import shutil
import subprocess
import textwrap
import pytest


def test_omp_factory_auth_live_session_and_nonexecuting_commands(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node with TypeScript stripping needed for OMP contract')
    major = int(subprocess.check_output([node, '--version'], text=True).lstrip('v').split('.')[0])
    if major < 24:
        pytest.skip('OMP factory execution check requires Node 24 TypeScript stripping')
    credentials = tmp_path / '.contextcore' / 'credentials'
    credentials.mkdir(parents=True)
    (credentials / 'omp.token').write_text('synthetic-omp-token\n')
    extension = Path(__file__).parents[1] / 'app' / 'integrations' / 'omp_extension.ts'
    harness = tmp_path / 'check.mjs'
    harness.write_text(textwrap.dedent('''
        import assert from 'node:assert/strict';
        import { pathToFileURL } from 'node:url';
        const { default: factory } = await import(pathToFileURL(process.argv[2]));
        assert.equal(typeof factory, 'function');
        const handlers = new Map(), commands = new Map(), calls = [], notices = [], messages = [];
        factory({on: (e,h) => handlers.set(e,h), registerCommand: (n,c) => commands.set(n,c),
          sendMessage: (message,options) => messages.push({message,options})});
        const ctx = {cwd: '/synthetic/workspace', sessionManager: {getSessionId: () => 'synthetic-session'},
          ui: {notify: (...args) => notices.push(args)}};
        globalThis.fetch = async (url,options) => {
          assert.equal(options.headers.Authorization, 'Bearer synthetic-omp-token');
          assert.equal(options.redirect, 'error');
          calls.push({url,body: options.body && JSON.parse(options.body)});
          return {ok:true,json:async () => ({injection:'six-part card; WAIT FOR CONFIRMATION'})};
        };
        await handlers.get('session_start')({},ctx);
        await new Promise(setImmediate);
        assert.equal(calls[0].body.sessionId,'synthetic-session');
        assert.equal(calls[0].body.cwd,ctx.cwd);
        assert.equal(messages.length,0,'session start must stay silent');
        const result = await handlers.get('input')({text:'接着做'},ctx);
        await new Promise(setImmediate);
        assert.equal(result,undefined,'input must not be rewritten');
        assert.equal(messages.length,0);
        assert.equal(calls.length,2,'one request per input');
        const command = commands.get('contextcore');
        assert.equal(typeof command.handler,'function');
        await command.handler('handoff synthetic task',ctx);
        assert.equal(calls[2].body.prompt,'synthetic task');
        assert.equal(calls[2].body.cwd,ctx.cwd);
        assert.equal(messages[0].options.triggerTurn,false);
        assert.match(messages[0].message.content,/WAIT FOR CONFIRMATION/);
        await handlers.get('input')({text:'/contextcore handoff'},ctx);
        assert.equal(calls.length,3,'no duplicate slash dispatch');
        await handlers.get('session_stop')({},ctx);
        await new Promise(setImmediate);
        assert.equal(calls[3].body.event,'session_stop');
        globalThis.fetch = async () => ({ok:false,status:401});
        assert.equal(await handlers.get('input')({text:'resume task'},ctx),undefined);
        assert.equal(notices.length,0,'automatic failures must stay silent');
        await command.handler('status',ctx);
        assert.match(notices.at(-1)[0],/not retrieved/);
        assert(!JSON.stringify(notices).includes('synthetic-omp-token'));
        const ctx2 = {...ctx,cwd:'/synthetic/other',sessionManager:{getSessionId:()=> 'session-two'}};
        globalThis.fetch = async (_url,options) => {
          assert.equal(JSON.parse(options.body).sessionId,'session-two');
          assert.equal(JSON.parse(options.body).cwd,ctx2.cwd);
          return {ok:true,json:async()=>({})};
        };
        await handlers.get('input')({text:'other project'},ctx2);
        console.log('OMP factory contract passed without inference');
    '''))
    result = subprocess.run([node,'--experimental-strip-types',str(harness),str(extension)],
                            env={**os.environ,'HOME':str(tmp_path)},capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'contract passed without inference' in result.stdout
