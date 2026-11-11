"""Quick operations obey the same preview/confirm/ownership contract."""
import pytest
from app.integrations.config_transactions import ConfigurationError
from tests.test_management import manager


def provider_model(manager):
    provider = manager.save('provider', {'name': 'Synthetic provider', 'protocol': 'openai',
        'base_url': 'https://synthetic.invalid/v1', 'agent_ids': ['codex']})
    model = manager.save('model', {'name': 'Chosen', 'provider_id': provider['id'], 'model_id': 'explicit-model'})
    return provider, model


def owned(manager):
    with manager.connect() as conn:
        return [tuple(row) for row in conn.execute('SELECT * FROM management_owned ORDER BY path,owner')]


def test_no_model_does_not_guess_or_change_definition(manager):
    provider, model = provider_model(manager)
    before = manager.get('provider', provider['id'])
    with pytest.raises(ConfigurationError, match='explicit model'):
        manager.quick_switch_provider('codex', provider['id'])
    assert manager.get('provider', provider['id']) == before
    assert not (manager.home / '.codex/config.toml').exists()
    assert owned(manager) == []


def test_switch_without_model_preserves_previously_confirmed_selection(manager):
    provider, model = provider_model(manager)
    plan = manager.quick_switch_provider('codex', provider['id'], model['id'])['plan']
    manager.apply(plan['id'], plan['version'], True)
    path = manager.home / '.codex/config.toml'
    before = path.read_bytes()
    result = manager.quick_switch_provider('codex', provider['id'])
    assert result['status'] == 'planned'
    assert result['model_id'] == model['model_id']
    assert path.read_bytes() == before
    assert 'explicit-model' in path.read_text()


def test_switch_only_writes_after_confirmed_apply(manager):
    provider, model = provider_model(manager)
    result = manager.quick_switch_provider('codex', provider['id'], model['id'])
    assert result['status'] == 'planned'
    assert not (manager.home / '.codex/config.toml').exists()
    plan = result['plan']
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'], plan['version'], False)
    assert not (manager.home / '.codex/config.toml').exists()
    manager.apply(plan['id'], plan['version'], True)
    assert 'explicit-model' in (manager.home / '.codex/config.toml').read_text()
    assert owned(manager)


def test_external_edit_failure_preserves_file_definition_and_ownership(manager):
    provider, model = provider_model(manager)
    plan = manager.quick_switch_provider('codex', provider['id'], model['id'])['plan']
    manager.apply(plan['id'], plan['version'], True)
    path = manager.home / '.codex/config.toml'
    path.write_text(path.read_text().replace('https://synthetic.invalid/v1', 'https://external.invalid/v1'))
    before, owners, definition = path.read_bytes(), owned(manager), manager.get('provider', provider['id'])
    with pytest.raises(ConfigurationError) as error:
        manager.quick_switch_provider('codex', provider['id'], model['id'])
    assert error.value.status_code == 409
    assert path.read_bytes() == before
    assert owned(manager) == owners
    assert manager.get('provider', provider['id']) == definition
    path.write_bytes(before.replace(b'https://external.invalid/v1', b'https://synthetic.invalid/v1'))
    restore = manager.plan({'host_id': 'codex', 'operation': 'restore'})
    manager.apply(restore['id'], restore['version'], True)
    uninstall = manager.plan({'host_id': 'codex', 'provider_id': provider['id'], 'operation': 'uninstall'})
    manager.apply(uninstall['id'], uninstall['version'], True)
    with manager.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM management_owned WHERE owner=?', (provider['id'],)).fetchone()[0] == 0


def test_multi_host_prepares_without_writing_and_reports_partial_failure(manager):
    provider, model = provider_model(manager)
    result = manager.sync_to_hosts('provider', provider['id'], ['codex', 'cursor'], model_id=model['id'])
    assert result['status'] == 'partial_plan'
    assert result['planned'] == ['codex']
    assert 'cursor' in result['failed']
    assert not (manager.home / '.codex/config.toml').exists()
    assert owned(manager) == []


def test_asset_external_edit_preserves_ownership_on_quick_retry(manager):
    asset = manager.save('asset', {'name': 'Synthetic MCP', 'kind': 'mcp',
        'config': {'transport': 'stdio', 'command': 'synthetic-server'}, 'agent_ids': ['codex']})
    result = manager.quick_toggle_asset('codex', asset['id'], True)
    assert result['status'] == 'planned'
    plan = result['plan']
    manager.apply(plan['id'], plan['version'], True)
    path = manager.home / '.codex/config.toml'
    path.write_text(path.read_text().replace('synthetic-server', 'external-server'))
    before, owners = path.read_bytes(), owned(manager)
    for enabled in (True, False):
        with pytest.raises(ConfigurationError):
            manager.quick_toggle_asset('codex', asset['id'], enabled)
        assert path.read_bytes() == before
        assert owned(manager) == owners
    with pytest.raises(ConfigurationError):
        manager.quick_toggle_asset('codex', asset['id'], 'false')


def test_http_quick_actions_return_redacted_previews_and_reject_nonboolean(manager, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import manage
    monkeypatch.setattr(manage.management, 'service', lambda: manager)
    api = FastAPI(); api.include_router(manage.router)
    provider, model = provider_model(manager)
    with TestClient(api) as client:
        missing = client.post('/api/manage/hosts/codex/quick-switch', json={'provider_id': provider['id']})
        assert missing.status_code == 422
        response = client.post('/api/manage/hosts/codex/quick-switch', json={'provider_id': provider['id'], 'model_id': model['id']})
        assert response.status_code == 200
        assert response.json()['status'] == 'planned'
        assert not (manager.home/'.codex/config.toml').exists()
        for file in response.json()['plan']['payload']['files']:
            assert not {'specs', 'snapshot', 'changes'} & set(file)
        multi = client.post('/api/manage/sync-hosts', json={'kind': 'provider', 'entity_id': provider['id'], 'host_ids': ['codex'], 'model_id': model['id']})
        assert multi.status_code == 200
        assert multi.json()['planned'] == ['codex']
        for file in multi.json()['results']['codex']['plan']['payload']['files']:
            assert not {'specs', 'snapshot', 'changes'} & set(file)
        rejected = client.post('/api/manage/hosts/codex/quick-toggle-asset', json={'asset_id': 'irrelevant', 'enabled': 'false'})
        assert rejected.status_code == 422
    assert owned(manager) == []


def test_import_without_model_keeps_model_unknown(manager):
    import json
    import sqlite3
    directory = manager.home / '.cc-switch'
    directory.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(directory / 'cc-switch.db') as conn:
        conn.executescript('PRAGMA user_version=21; CREATE TABLE providers (id TEXT, app_type TEXT, name TEXT, settings_config TEXT, is_current INTEGER);')
        conn.execute('INSERT INTO providers VALUES (?,?,?,?,?)', ('missing', 'codex', 'No model configured', json.dumps({'config': 'model_provider="synthetic"\n[model_providers.synthetic]\nname="Synthetic"\nbase_url="https://synthetic.invalid/v1"\nwire_api="responses"\n'}), 1))
    manager.sync_local_environment()
    providers = manager.list('provider')
    imported = next(p for p in providers if p['name'] == 'No model configured')
    assert imported.get('model_map', {}) == {}
    assert not [m for m in manager.list('model') if m['provider_id'] == imported['id']]
    with pytest.raises(ConfigurationError, match='explicit model'):
        manager.quick_switch_provider('codex', imported['id'])
    assert not (manager.home / '.codex/config.toml').exists()


@pytest.mark.parametrize('shortcut', ['switch', 'toggle', 'sync'])
def test_shortcut_cannot_apply_even_when_request_contains_confirmation(manager, monkeypatch, shortcut):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import manage
    monkeypatch.setattr(manage.management, 'service', lambda: manager)
    api = FastAPI()
    api.include_router(manage.router)
    provider, model = provider_model(manager)
    asset = manager.save('asset', {'name': 'Synthetic MCP', 'kind': 'mcp',
        'config': {'transport': 'stdio', 'command': 'synthetic-server'}, 'agent_ids': ['codex']})
    requests = {
        'switch': ('/api/manage/hosts/codex/quick-switch', {'provider_id': provider['id'], 'model_id': model['id']}),
        'toggle': ('/api/manage/hosts/codex/quick-toggle-asset', {'asset_id': asset['id'], 'enabled': True}),
        'sync': ('/api/manage/sync-hosts', {'kind': 'provider', 'entity_id': provider['id'], 'host_ids': ['codex'], 'model_id': model['id']}),
    }
    path, body = requests[shortcut]
    with TestClient(api) as client:
        response = client.post(path, json={**body, 'confirmed': True})
        assert response.status_code == 200
        payload = response.json()
        plan = payload['results']['codex']['plan'] if shortcut == 'sync' else payload['plan']
        assert plan['status'] == 'planned'
        assert not (manager.home / '.codex/config.toml').exists()
        assert owned(manager) == []
        for confirmation in (False, None, 'true', 1):
            rejected = client.post(f"/api/manage/plans/{plan['id']}/apply", json={
                'expected_version': plan['version'], 'confirmed': confirmation})
            assert rejected.status_code == 422
            assert not (manager.home / '.codex/config.toml').exists()
            assert owned(manager) == []
        applied = client.post(f"/api/manage/plans/{plan['id']}/apply", json={
            'expected_version': plan['version'], 'confirmed': True})
        assert applied.status_code == 200
        assert applied.json()['status'] == 'applied'
        assert (manager.home / '.codex/config.toml').exists()
        assert owned(manager)


@pytest.mark.parametrize('entry', ['service', 'transaction'])
def test_expired_preview_is_rejected_before_any_configuration_write(manager, monkeypatch, entry):
    from datetime import datetime, timedelta, timezone
    provider, model = provider_model(manager)
    plan = manager.quick_switch_provider('codex', provider['id'], model['id'])['plan']
    old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
    with manager.connect() as conn:
        conn.execute('UPDATE management_plans SET created_at=? WHERE id=?', (old, plan['id']))
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('Expired preview must not read credentials'))
    target = manager if entry == 'service' else manager.transactions
    with pytest.raises(ConfigurationError, match='Plan expired'):
        target.apply(plan['id'], plan['version'], True)
    assert not (manager.home / '.codex/config.toml').exists()
    assert owned(manager) == []
    assert manager.transactions.get(plan['id'])['status'] == 'planned'


def test_applied_plan_receipt_remains_idempotent_after_expiry(manager):
    from datetime import datetime, timedelta, timezone
    provider, model = provider_model(manager)
    plan = manager.quick_switch_provider('codex', provider['id'], model['id'])['plan']
    manager.apply(plan['id'], plan['version'], True)
    applied = manager.transactions.get(plan['id'])
    before = (manager.home / '.codex/config.toml').read_bytes()
    with manager.connect() as conn:
        conn.execute('UPDATE management_plans SET created_at=? WHERE id=?', ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), plan['id']))
    assert manager.transactions.apply(plan['id'], applied['version'], True)['status'] == 'applied'
    assert (manager.home / '.codex/config.toml').read_bytes() == before


@pytest.mark.parametrize('operation', ['bundle', 'restore', 'uninstall'])
def test_bundle_restore_and_uninstall_bind_preview_confirmation_and_file_readback(manager, operation):
    import hashlib
    import json
    from pathlib import Path
    from app.routers.manage import public_plan
    path = manager.home / '.config/opencode/opencode.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'model': 'original/native', 'permission': {'foreign': 'ask'}}))
    provider = manager.save('provider', {'name': 'Synthetic', 'protocol': 'openai',
        'base_url': 'https://synthetic.invalid/v1', 'model_map': {'chosen': 'explicit-model'}})
    asset = manager.save('asset', {'name': 'Synthetic MCP', 'kind': 'mcp',
        'config': {'transport': 'stdio', 'command': 'synthetic-server'}})
    bundle = manager.save('bundle', {'name': 'Synthetic bundle', 'provider_id': provider['id'],
        'asset_ids': [asset['id']], 'host_ids': ['opencode']})
    install_request = {'host_id': 'opencode', 'bundle_id': bundle['id'], 'activate_provider': True, 'model_id': 'chosen'}
    if operation != 'bundle':
        installed = manager.plan(install_request)
        manager.apply(installed['id'], installed['version'], True)
    before = path.read_bytes()
    owners = owned(manager)
    request = install_request if operation == 'bundle' else {'host_id': 'opencode', 'operation': operation}
    if operation == 'uninstall':
        request['asset_ids'] = [asset['id']]
    plan = manager.plan(request)
    preview = public_plan(plan)
    assert preview['payload']['metadata']['host_id'] == 'opencode'
    assert preview['payload']['files']
    assert all(item['diff'] and item['path'] for item in preview['payload']['files'])
    assert all('specs' not in item and 'snapshot' not in item for item in preview['payload']['files'])
    assert path.read_bytes() == before and owned(manager) == owners
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'], plan['version'], False)
    assert path.read_bytes() == before and owned(manager) == owners
    receipt = manager.apply(plan['id'], plan['version'], True)
    assert receipt['id'] == plan['id'] and receipt['status'] == 'applied'
    assert receipt['payload']['readback'] == 'verified'
    assert receipt['payload']['host_reload'] == receipt['payload']['real_request'] == 'not_run'
    for item in receipt['payload']['files']:
        target = Path(item['path'])
        actual_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        assert actual_hash == item['after_hash']
    data = json.loads(path.read_text())
    assert data['permission'] == {'foreign': 'ask'}
    assert data['model'] == ('original/native' if operation == 'restore' else provider['id'] + '/explicit-model')
    assert manager.host_state('opencode')['effects']['invocation'] == 'not_checked'


@pytest.mark.parametrize('host', ['codex', 'opencode'])
def test_explicit_selection_takeover_never_overwrites_later_external_model_choice(manager, host):
    import json
    provider, model = provider_model(manager)
    path = manager.home / ('.codex/config.toml' if host == 'codex' else '.config/opencode/opencode.json')
    path.parent.mkdir(parents=True)
    original = 'model = "foreign-original"\n# preserve me\n' if host == 'codex' else json.dumps({'model': 'foreign-original', 'permission': {'foreign': 'ask'}})
    path.write_text(original)
    plan = manager.quick_switch_provider(host, provider['id'], model['id'])['plan']
    assert path.read_text() == original
    assert plan['payload']['metadata']['model_selection']['native_model'] == 'explicit-model'
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'], plan['version'], False)
    assert path.read_text() == original
    manager.apply(plan['id'], plan['version'], True)
    if host == 'codex':
        path.write_text(path.read_text().replace('model = "explicit-model"', 'model = "external-choice"'))
        assert '# preserve me' in path.read_text()
    else:
        data = json.loads(path.read_text())
        assert data['permission'] == {'foreign': 'ask'}
        data['model'] = 'external-choice'
        path.write_text(json.dumps(data))
    changed = path.read_bytes()
    assert b'external-choice' in changed
    owners = owned(manager)
    for request in ({'host_id': host, 'provider_id': provider['id'], 'activate_provider': True, 'model_id': model['id']}, {'host_id': host, 'operation': 'restore'}):
        with pytest.raises(ConfigurationError):
            manager.plan(request)
        assert path.read_bytes() == changed and owned(manager) == owners


def test_apply_failure_records_safe_reason_without_exception_body(manager, monkeypatch):
    import json
    provider, model = provider_model(manager)
    plan = manager.quick_switch_provider('codex', provider['id'], model['id'])['plan']
    original_atomic = manager.transactions._atomic
    owners = owned(manager)
    def fail(*_):
        raise OSError('private password=synthetic-sensitive-body')
    monkeypatch.setattr(manager.transactions, '_atomic', fail)
    with pytest.raises(ConfigurationError, match='recovery is required'):
        manager.apply(plan['id'], plan['version'], True)
    receipt = manager.transactions.get(plan['id'])
    assert receipt['status'] == 'recovery_required'
    assert receipt['payload']['readback'] == 'not_run'
    assert receipt['payload']['failure']['code'] == 'filesystem_error'
    assert 'synthetic-sensitive-body' not in json.dumps(receipt)
    assert owned(manager) == owners
    monkeypatch.setattr(manager.transactions, '_atomic', original_atomic)
    recovered = manager.recover_plan(plan['id'], True)
    assert recovered['status'] == 'recovered'
    assert recovered['payload']['failure']['code'] == 'filesystem_error'
    fresh = manager.quick_switch_provider('codex', provider['id'], model['id'])['plan']
    manager.apply(fresh['id'], fresh['version'], True)
    restore = manager.plan({'host_id': 'codex', 'operation': 'restore'})
    manager.apply(restore['id'], restore['version'], True)
    uninstall = manager.plan({'host_id': 'codex', 'provider_id': provider['id'], 'operation': 'uninstall'})
    assert manager.apply(uninstall['id'], uninstall['version'], True)['status'] == 'applied'


def test_failed_plan_operations_are_queryable_in_unified_audit_without_private_text(manager, monkeypatch, caplog, capsys):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import audit
    monkeypatch.setenv('CONTEXTCORE_DB', str(manager.db_path))
    provider, model = provider_model(manager)
    plan = manager.quick_switch_provider('codex', provider['id'], model['id'])['plan']
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'], plan['version'], False)
    original_atomic = manager.transactions._atomic
    def fail(*_):
        raise OSError('password=synthetic-audit-private-body')
    monkeypatch.setattr(manager.transactions, '_atomic', fail)
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'], plan['version'], True)
    monkeypatch.setattr(manager.transactions, '_atomic', original_atomic)
    manager.recover_plan(plan['id'], True)
    api = FastAPI()
    api.include_router(audit.router)
    with TestClient(api) as client:
        response = client.get('/api/audit', params={'actor': 'local-management', 'action': 'management.plan.apply'})
        assert response.status_code == 200
        rows = response.json()
        assert {r['result'] for r in rows} == {'attempted', 'failed:confirmation_required', 'failed:recovery_required:filesystem_error'}
        assert all(r['target'] == plan['id'] for r in rows)
        all_rows = client.get('/api/audit').json()
        assert any(r['action'] == 'management.plan.recover_plan' and r['result'] == 'recovered' for r in all_rows)
        assert 'synthetic-audit-private-body' not in json.dumps(all_rows)
    captured = capsys.readouterr()
    assert 'synthetic-audit-private-body' not in caplog.text + captured.out + captured.err


def test_unavailable_audit_prevents_configuration_write(manager):
    import sqlite3
    provider, model = provider_model(manager)
    plan = manager.quick_switch_provider('codex', provider['id'], model['id'])['plan']
    with manager.connect() as conn:
        conn.execute("CREATE TRIGGER deny_audit BEFORE INSERT ON audit_logs BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END")
    with pytest.raises(sqlite3.DatabaseError):
        manager.apply(plan['id'], plan['version'], True)
    assert not (manager.home / '.codex/config.toml').exists()
    assert owned(manager) == []


@pytest.mark.parametrize('kind', ['mcp', 'prompt'])
def test_marker_or_native_name_cannot_replace_missing_ownership(manager, kind):
    asset = manager.save('asset', {'name': 'Synthetic', 'kind': kind, **({'config': {'command': 'synthetic-server'}} if kind == 'mcp' else {'content': 'Synthetic rule'})})
    plan = manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']]})
    manager.apply(plan['id'], plan['version'], True)
    from pathlib import Path
    paths = [Path(item['path']) for item in plan['payload']['files']]
    before = {str(p): p.read_bytes() for p in paths}
    with manager.connect() as conn:
        conn.execute('DELETE FROM management_owned WHERE owner=?', (asset['id'],))
    for operation in ('install', 'uninstall'):
        with pytest.raises(ConfigurationError):
            manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']], 'operation': operation})
        assert {str(p): p.read_bytes() for p in paths} == before
        assert owned(manager) == []


def test_recovery_refuses_changed_ownership_even_with_matching_file_hash(manager, monkeypatch):
    asset = manager.save('asset', {'name': 'Synthetic MCP', 'kind': 'mcp', 'config': {'command': 'synthetic-server'}})
    plan = manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']]})
    atomic = manager.transactions._atomic
    def fail(*_):
        raise OSError('synthetic interruption')
    monkeypatch.setattr(manager.transactions, '_atomic', fail)
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'], plan['version'], True)
    spec = plan['payload']['files'][0]['specs'][0]
    path = spec['path']
    with manager.connect() as conn:
        conn.execute('INSERT INTO management_owned VALUES(?,?,?,?)', (path, manager.transactions._item_key(spec), 'external-hash', 'external-owner'))
    monkeypatch.setattr(manager.transactions, '_atomic', atomic)
    owners = owned(manager)
    with pytest.raises(ConfigurationError, match='ownership changed'):
        manager.recover_plan(plan['id'], True)
    assert owned(manager) == owners
    assert manager.transactions.get(plan['id'])['status'] == 'recovery_required'


def test_recovery_rejects_corrupt_snapshot_before_replacing_target(manager, monkeypatch):
    from pathlib import Path
    path = manager.home / '.codex/config.toml'
    path.parent.mkdir(parents=True)
    path.write_text('# foreign original\n')
    asset = manager.save('asset', {'name': 'Synthetic MCP', 'kind': 'mcp', 'config': {'command': 'synthetic-server'}})
    plan = manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']]})
    atomic = manager.transactions._atomic
    def interrupted(target, data):
        atomic(target, data)
        raise OSError('synthetic crash after replacement')
    monkeypatch.setattr(manager.transactions, '_atomic', interrupted)
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'], plan['version'], True)
    receipt = manager.transactions.get(plan['id'])
    snapshot = Path(receipt['payload']['files'][0]['snapshot'])
    snapshot.write_text('corrupted snapshot')
    before = path.read_bytes()
    monkeypatch.setattr(manager.transactions, '_atomic', atomic)
    with pytest.raises(ConfigurationError, match='integrity validation'):
        manager.recover_plan(plan['id'], True)
    assert path.read_bytes() == before
    assert manager.transactions.get(plan['id'])['status'] == 'recovery_required'


@pytest.mark.parametrize('kind', ['mcp', 'skill', 'prompt'])
def test_external_asset_edit_preserves_managed_state_and_allows_explicit_repair_then_uninstall(manager, kind):
    from pathlib import Path
    body = {'config': {'command': 'synthetic-original'}} if kind == 'mcp' else {'files': {'SKILL.md': '# synthetic-original'}} if kind == 'skill' else {'content': 'synthetic-original'}
    asset = manager.save('asset', {'name': 'Synthetic editable', 'kind': kind, **body})
    plan = manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']]})
    manager.apply(plan['id'], plan['version'], True)
    path = Path(plan['payload']['files'][0]['path'])
    original = path.read_bytes()
    changed = original.replace(b'synthetic-original', b'external-change')
    assert changed != original
    path.write_bytes(changed)
    owners = owned(manager)
    for operation in ('install', 'uninstall'):
        with pytest.raises(ConfigurationError) as failure:
            manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']], 'operation': operation})
        assert failure.value.status_code == 409
        assert path.read_bytes() == changed and owned(manager) == owners
    assert manager.host_state('codex')['resources'][asset['id']]['status'] == 'externally_changed'
    # Simulate the user's explicit repair of the external edit to known bytes.
    path.write_bytes(original)
    assert manager.host_state('codex')['resources'][asset['id']]['status'] == 'verified'
    fresh = manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']]})
    manager.apply(fresh['id'], fresh['version'], True)
    uninstall = manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']], 'operation': 'uninstall'})
    manager.apply(uninstall['id'], uninstall['version'], True)
    assert manager.host_state('codex')['resources'][asset['id']]['status'] == 'removed'


@pytest.mark.skipif(__import__('sys').platform != 'darwin', reason='macOS snapshot ACL')
def test_private_recovery_snapshot_does_not_leak_into_public_copies(manager):
    import json
    import subprocess
    from pathlib import Path
    from app.routers.manage import public_plan
    secret = 'synthetic-snapshot-only-value'
    path = manager.home / '.claude.json'
    path.write_text(json.dumps({'env': {'CUSTOM_AUTH_TOKEN': secret}, 'foreign': True}))
    directory = manager.transactions.state_dir
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(['chmod', '+a', 'everyone allow read,search', str(directory)], check=True)
    asset = manager.save('asset', {'name': 'Synthetic', 'kind': 'mcp', 'config': {'command': 'synthetic-server'}})
    plan = manager.plan({'host_id': 'claude-code', 'asset_ids': [asset['id']]})
    receipt = manager.apply(plan['id'], plan['version'], True)
    snapshot = Path(receipt['payload']['files'][0]['snapshot'])
    assert secret.encode() in snapshot.read_bytes()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert len(subprocess.check_output(['ls', '-lde', str(directory)], text=True).splitlines()) == 1
    with manager.connect() as conn:
        audit = [dict(row) for row in conn.execute('SELECT * FROM audit_logs')]
    for public in (public_plan(plan), public_plan(receipt), manager.export(), audit):
        assert secret not in json.dumps(public)
        assert '.snapshot' not in json.dumps(public)
