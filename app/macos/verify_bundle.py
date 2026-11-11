"""Smoke-test the actual packaged service without opening a test page."""
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import argparse
from http.cookiejar import CookieJar

parser = argparse.ArgumentParser()
parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent / 'dist/Senrelay.app')
bundle = parser.parse_args().bundle.resolve()
service = bundle / 'Contents/Resources/service/senrelay-service'
with tempfile.TemporaryDirectory(prefix='senrelay-desktop-check-') as tmp:
    root = Path(tmp).resolve()
    home = root / 'home'
    home.mkdir()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(CookieJar()))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    with (root / 'service.log').open('w') as log:
        process = subprocess.Popen([str(service), '-m', 'app.cli', 'serve', '--data-dir', str(root / 'data'), '--port', str(port)], stdout=log, stderr=log, cwd=tmp,
            env={'HOME': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'})
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError((root / 'service.log').read_text())
                try:
                    with urllib.request.urlopen(base + '/health', timeout=1) as response:
                        assert json.load(response)['ok']; break
                except OSError: time.sleep(0.2)
            else: raise AssertionError('Packaged service did not become healthy')
            with urllib.request.urlopen(base + '/api/release') as response:
                release = json.load(response)
            assert release == json.loads((bundle / 'Contents/Resources/desktop-release.json').read_text())
            assert release['api_contract'] == 2
            with urllib.request.urlopen(base + '/') as response:
                html = response.read().decode()
            assert 'id="root"' in html and '/@vite/client' not in html
            import re
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
            assert assets
            for asset in assets:
                with urllib.request.urlopen(base + asset) as response: assert response.status == 200
            with sqlite3.connect(root / 'data/contextcore.db') as conn:
                assert conn.execute('SELECT count(*) FROM projects').fetchone()[0] == 0
                assert conn.execute('SELECT count(*) FROM connections WHERE enabled = 1').fetchone()[0] == 0
            with opener.open(base + '/api/session', timeout=3) as response:
                csrf = json.load(response)['csrf_token']
            def post(path, body, timeout=30):
                request = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
                    'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Origin': base}, method='POST')
                with opener.open(request, timeout=timeout) as response:
                    return json.load(response)
            # Exercise management inside the real frozen runtime, with a clean
            # temporary home and no credentials, host execution or model calls.
            created = post('/api/manage/providers/setup', {'request_id': 'bundle-smoke-setup-0001', 'host_id': 'codex',
                'provider': {'name': 'Bundle fixture', 'base_url': 'http://127.0.0.1:18999/v1', 'protocol': 'openai'}, 'model_id': 'bundle-fixture-model'})
            plan = post('/api/manage/plans', {'host_id': 'codex', 'provider_id': created['provider']['id'], 'model_id': created['model']['id'], 'activate_provider': True})
            applied = post('/api/manage/plans/' + plan['id'] + '/apply', {'confirmed': True, 'expected_version': plan['version']})
            assert applied['payload']['readback'] == 'verified'
            with opener.open(base + '/api/manage/hosts/codex/state', timeout=5) as response:
                configured = json.load(response)
            assert configured['selection']['status'] == 'verified'
            assert configured['selection']['model_id'] == 'bundle-fixture-model'
            assert configured['credentials_read'] is False
            assert 'bundle-fixture-model' in (home / '.codex/config.toml').read_text()
            # Installing/removing a project bundle without model activation
            # must preserve the selection recorded by the frozen service too.
            tool = post('/api/manage/assets', {'name': 'Project bundle tool', 'kind': 'mcp', 'config': {'url': 'http://127.0.0.1:18999/mcp', 'transport': 'http'}})
            composition = post('/api/manage/bundles', {'name': 'Project tools only', 'asset_ids': [tool['id']], 'host_ids': ['codex']})
            for operation in ('install', 'uninstall'):
                bundle_plan = post('/api/manage/plans', {'host_id': 'codex', 'bundle_id': composition['id'], 'operation': operation})
                assert bundle_plan['payload']['metadata']['configuration_changes'] == [{'name': 'Project bundle tool', 'kind': 'mcp', 'operation': operation}]
                result = post('/api/manage/plans/' + bundle_plan['id'] + '/apply', {'confirmed': True, 'expected_version': bundle_plan['version']})
                assert result['payload']['readback'] == 'verified'
                if operation == 'install':
                    with sqlite3.connect(root / 'data/contextcore.db') as conn:
                        previous = json.loads(conn.execute('SELECT data FROM management_activations WHERE host_id=?', ('codex',)).fetchone()[0])
                        previous.update(provider_id=None, model_id=None)
                        previous.pop('selection_receipt_version', None)
                        previous.pop('selection_receipt_unknown', None)
                        legacy = json.dumps(previous)
                        conn.execute('UPDATE management_activations SET data=? WHERE host_id=?', (legacy, 'codex'))
                with opener.open(base + '/api/manage/hosts/codex/state', timeout=5) as response:
                    bundle_state = json.load(response)
                assert bundle_state['selection']['provider_id'] == created['provider']['id']
                assert bundle_state['selection']['model_id'] == 'bundle-fixture-model' and bundle_state['selection']['status'] == 'verified'
                if operation == 'install':
                    with sqlite3.connect(root / 'data/contextcore.db') as conn:
                        assert conn.execute('SELECT data FROM management_activations WHERE host_id=?', ('codex',)).fetchone()[0] == legacy
            # Per-host mode planning is exercised in the frozen build too.
            # No confirmed gateway apply: this smoke never touches real Keychain.
            with opener.open(base + '/api/manage/hosts/codex/mode', timeout=5) as response:
                mode = json.load(response)
            assert mode['mode'] == 'direct' and mode['version'] == 0 and mode['supported']
            native_before = (home / '.codex/config.toml').read_bytes()
            gateway = post('/api/manage/hosts/codex/mode/preview', {'expected_version': 0, 'mode': 'routing', 'endpoint': 'responses',
                'target': {'provider_id': created['provider']['id'], 'model_id': created['model']['id']}})
            assert gateway['status'] == 'planned'
            assert gateway['payload']['metadata']['connection_change']['gateway_url'] == base + '/api/routing/v1/hosts/codex'
            assert 'host_routing' not in gateway['payload']['metadata'] and 'keychain:' not in json.dumps(gateway)
            import urllib.error
            try:
                post('/api/manage/plans/' + gateway['id'] + '/apply', {'confirmed': False, 'expected_version': gateway['version']})
                raise AssertionError('Unconfirmed gateway apply was accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 422
            assert (home / '.codex/config.toml').read_bytes() == native_before
            # Definition deletion retains relationship guards in the actual
            # frozen runtime, without touching host files or real credentials.
            unused = post('/api/manage/providers', {'name': 'Unused smoke provider', 'protocol': 'openai', 'base_url': 'http://127.0.0.1:18999/v1'})
            dependent = post('/api/manage/models', {'name': 'Unused smoke model', 'provider_id': unused['id'], 'model_id': 'unused-native'})
            def delete(path):
                request = urllib.request.Request(base + path, headers={'X-CSRF-Token': csrf, 'Origin': base}, method='DELETE')
                with opener.open(request, timeout=5) as response:
                    return json.load(response)
            try:
                delete('/api/manage/providers/' + unused['id'] + '?expected_version=' + str(unused['version']))
                raise AssertionError('Referenced provider deletion was accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 409
            assert delete('/api/manage/models/' + dependent['id'] + '?expected_version=' + str(dependent['version']))['deleted']
            assert delete('/api/manage/providers/' + unused['id'] + '?expected_version=' + str(unused['version']))['deleted']
            # Independent client routing must reject nonexistent providers and protect referenced providers from deletion.
            def patch(path, body):
                request = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={
                    'Content-Type': 'application/json', 'X-CSRF-Token': csrf, 'Origin': base}, method='PATCH')
                with opener.open(request, timeout=5) as response:
                    return json.load(response)
            try:
                patch('/api/routing/settings', {'enabled': True, 'provider_id': 'nonexistent-provider-id', 'fallback_provider_ids': [], 'model_routes': {}})
                raise AssertionError('Saving nonexistent provider in independent routing was accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 409
            routed_provider = post('/api/manage/providers', {'name': 'Routed smoke provider', 'protocol': 'openai', 'base_url': 'http://127.0.0.1:18999/v1'})
            patch('/api/routing/settings', {'enabled': True, 'provider_id': routed_provider['id'], 'fallback_provider_ids': [], 'model_routes': {}})
            try:
                delete('/api/manage/providers/' + routed_provider['id'] + '?expected_version=' + str(routed_provider['version']))
                raise AssertionError('Deleting provider referenced by independent routing was accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 409
            patch('/api/routing/settings', {'enabled': False, 'provider_id': None, 'fallback_provider_ids': [], 'model_routes': {}})
            assert delete('/api/manage/providers/' + routed_provider['id'] + '?expected_version=' + str(routed_provider['version']))['deleted']
            skill_entry = '# Packaged skill\n\n' + ('packaged-body-line\n' * 1500)
            large_skill = post('/api/manage/assets', {
                'name': 'Packaged large skill smoke',
                'kind': 'skill',
                'files': {'SKILL.md': skill_entry, **{f'references/part-{idx}.md': ('reference-line\n' * 2000) for idx in range(6)}},
            })
            assert delete('/api/manage/assets/' + large_skill['id'] + '?expected_version=' + str(large_skill['version']))['deleted']
            assert (home / '.codex/config.toml').read_bytes() == native_before
            status = subprocess.run([str(service), '-m', 'app.cli', 'status', '--data-dir', str(root / 'data'), '--json'], capture_output=True, text=True, timeout=10)
            assert status.returncode == 0, status.stderr
            assert json.loads(status.stdout)['running'], status.stdout
            # DEVNULL avoids a PIPE deadlock if the second process logs while we wait.
            duplicate = subprocess.run(
                [str(service), '-m', 'app.cli', 'serve', '--data-dir', str(root / 'data'), '--port', str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=8)
            assert duplicate.returncode != 0, duplicate.stderr
            print(json.dumps({'health': True, 'release': release, 'static_assets': len(assets), 'no_demo_projects': True, 'capture_disabled': True, 'duplicate_rejected': True, 'packaged_management_apply_readback': True, 'packaged_nonactivating_project_bundle_keeps_selection': True, 'packaged_legacy_bundle_selection_readback': True, 'packaged_reference_guard_and_delete': True, 'packaged_independent_routing_guard': True, 'packaged_metadata_only_large_skill_delete': True, 'packaged_host_mode_preview_and_unconfirmed_apply_refusal': True}, indent=2))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
        assert process.returncode == 0, (root / 'service.log').read_text()
        print('Graceful shutdown: PASS')
