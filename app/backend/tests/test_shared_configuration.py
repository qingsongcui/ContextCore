import copy
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.config_transactions import ConfigurationError
from app.routers import manage
from app.services import management_service
from app.services.configuration_backup import restore
from tests.test_management import FakeVault, manager, mcp
from tests.test_project_bundles import apply, provider


def test_archive_is_atomic_idempotent_and_portable(manager, tmp_path):
    current = provider(manager, 'Daily')
    resource = mcp(manager)
    account = manager.save('account', {'name': 'Local account', 'kind': 'api_key', 'provider_id': current['provider']['id']})
    bundle = manager.save('bundle', {'name': 'Work', 'provider_id': current['provider']['id'],
        'model_id': current['model']['id'], 'host_ids': ['codex'], 'asset_ids': [resource['id']]})
    archive = manager.export()
    assert 'synthetic-bundle-key' not in json.dumps(archive)
    home = tmp_path / 'other-home'
    home.mkdir()
    other = management_service.ManagementService(tmp_path / 'other/database.db', home, FakeVault())
    planned = restore(other, archive)
    assert planned['changed'] == 5 and not other.list('bundle') and not list(home.iterdir())
    result = restore(other, archive, preview_id=planned['preview_id'], confirmed=True)
    assert result['status'] == 'restored' and len(other.list('provider')) == 1
    assert restore(other, archive, preview_id=planned['preview_id'], confirmed=True)['status'] == 'restored'
    assert restore(other, archive)['changed'] == 0
    assert other.get('bundle', bundle['id'])['model_id'] == current['model']['id']
    assert other.get('account', account['id'])['provider_id'] == current['provider']['id']
    assert not other.get('provider', current['provider']['id']).get('credential_ref')
    assert not list(home.iterdir())


def test_restore_verifies_versions_and_leaves_bound_credentials_alone(manager):
    current = provider(manager, 'Daily')
    resource = mcp(manager)
    archive = manager.export()
    manager.save('asset', {'name': 'Changed'}, resource['id'], resource['version'])
    provider_record = next(row['data'] for row in archive['objects'] if row['entity_kind'] == 'provider')
    provider_record['base_url'] = 'https://elsewhere.example/v1'
    planned = restore(manager, archive, replace_existing=True)
    assert {item['action'] for item in planned['items']} == {'unchanged', 'replace', 'keep_protected'}
    changed = manager.get('asset', resource['id'])
    manager.save('asset', {'name': 'External edit'}, resource['id'], changed['version'])
    with pytest.raises(ConfigurationError, match='changed since'):
        restore(manager, archive, True, planned['preview_id'], True)
    assert manager.get('asset', resource['id'])['name'] == 'External edit'
    planned = restore(manager, archive, True)
    assert restore(manager, archive, True, planned['preview_id'], True)['changed'] == 1
    assert manager.get('asset', resource['id'])['name'] == resource['name']
    assert manager.get('provider', current['provider']['id'])['base_url'] == current['provider']['base_url']
    poisoned = copy.deepcopy(archive)
    poisoned['objects'][-1]['data']['credential_ref'] = 'fake'
    with pytest.raises(ConfigurationError, match='credential'):
        restore(manager, poisoned)


def test_profile_switch_records_removals_files_and_model(manager):
    a, b, manual = mcp(manager, 'Previous tool'), mcp(manager, 'Next tool'), mcp(manager, 'Manual tool')
    current = provider(manager, 'Daily')
    old = manager.save('bundle', {'name': 'Earlier', 'host_ids': ['codex'], 'asset_ids': [a['id']]})
    new = manager.save('bundle', {'name': 'Daily', 'host_ids': ['codex'], 'asset_ids': [b['id']],
        'replace_previous_assets': True, 'model_id': current['model']['id'], 'provider_id': current['provider']['id']})
    apply(manager, {'host_id': 'codex', 'asset_ids': [manual['id']]})
    apply(manager, {'host_id': 'codex', 'bundle_id': old['id']})
    plan = manager.plan({'host_id': 'codex', 'bundle_id': new['id']})
    meta = plan['payload']['metadata']
    assert meta['model_selection']['native_model'] == 'Daily-native'
    assert meta['removed_assets'] == [a['id']]
    assert meta['scope'] == 'selected_host_global'
    assert manager.host_state('codex')['resources'][a['id']]['status'] == 'verified'
    manager.apply(plan['id'], plan['version'], True)
    state = manager.host_state('codex')
    assert state['resources'][manual['id']]['status'] == state['resources'][b['id']]['status'] == 'verified'
    assert state['resources'][a['id']]['status'] == 'removed'
    assert state['selection']['provider_id'] == current['provider']['id']
    with pytest.raises(ConfigurationError):
        manager.delete('model', current['model']['id'], current['model']['version'])


def test_matrix_and_archive_include_only_scoped_readback(manager, monkeypatch):
    api = FastAPI()
    api.include_router(manage.router)
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    resource = mcp(manager)
    apply(manager, {'host_id': 'codex', 'asset_ids': [resource['id']]})
    client = TestClient(api)
    response = client.get('/api/manage/shared-resource-matrix?host_ids=codex,claude-code&kind=mcp')
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    assert item['cells']['codex']['status'] == 'verified'
    assert item['cells']['claude-code']['status'] == 'not_managed'
    assert 'config' not in item
    assert client.get('/api/manage/shared-resource-matrix?host_ids=unknown').status_code == 422
    history = client.get('/api/manage/plans?summary=true&agent_id=codex').json()['items'][0]
    assert history['payload']['metadata']['configuration_changes'][0]['name'] == resource['name']
    backup = client.get('/api/manage/export').json()
    assert client.post('/api/manage/configuration-backup/preview', json={'data': backup}).json()['changed'] == 0
    assert client.post('/api/manage/configuration-backup/restore', json={'data': backup}).status_code == 422


@pytest.mark.parametrize('manual_before,surface', [(True, 'codex'), (False, 'codex'), (False, 'codex')])
def test_profile_switch_preserves_independently_managed_resources(manager, manual_before, surface):
    first, second = mcp(manager, 'Shared tool'), mcp(manager, 'Other tool')
    old = manager.save('bundle', {'name': 'First', 'host_ids': ['codex'], 'asset_ids': [first['id']]})
    new = manager.save('bundle', {'name': 'Second', 'host_ids': ['codex'], 'asset_ids': [second['id']], 'replace_previous_assets': True})
    def manual():
        apply(manager, {'host_id': surface, 'asset_ids': [first['id']]})
    if manual_before:
        manual()
    apply(manager, {'host_id': 'codex', 'bundle_id': old['id']})
    if not manual_before:
        stale = manager.plan({'host_id': 'codex', 'bundle_id': new['id']})
        manual()
        with pytest.raises(ConfigurationError, match='changed since'):
            manager.apply(stale['id'], stale['version'], True)
    apply(manager, {'host_id': 'codex', 'bundle_id': new['id']})
    assert manager.host_state('codex')['resources'][first['id']]['status'] == 'verified'


def test_new_restore_after_deletion_gets_a_new_receipt(manager):
    resource = mcp(manager)
    archive = manager.export()
    for _ in range(2):
        manager.delete('asset', resource['id'], manager.get('asset', resource['id'])['version'])
        preview = restore(manager, archive)
        assert restore(manager, archive, False, preview['preview_id'], True)['changed'] == 1
        assert manager.get('asset', resource['id'])['name'] == resource['name']


@pytest.mark.asyncio
async def test_portable_mcp_requires_review_and_rebinds_only_a_selected_local_key(manager, tmp_path):
    from unittest.mock import AsyncMock
    account = manager.save('account', {'kind': 'api_key', 'name': 'MCP credential'})
    account = manager.credential('account', account['id'], 'synthetic-original-value')
    resource = manager.save('asset', {'name': 'Header tool', 'kind': 'mcp',
        'config': {'transport': 'http', 'url': 'https://fixture.example/mcp'},
        'mcp_account_bindings': [{'location': 'headers', 'name': 'Authorization', 'account_id': account['id'], 'bearer': True}]})
    archive = manager.export()
    assert 'synthetic-original-value' not in json.dumps(archive)
    assert 'keychain:senrelay:' not in json.dumps(archive)
    entry = next(item['data'] for item in archive['objects'] if item['entity_kind'] == 'asset')
    assert entry['mcp_fields_to_review'] == [{'location': 'headers', 'name': 'Authorization', 'bearer': True}]
    assert 'mcp_account_bindings' not in entry
    other = management_service.ManagementService(tmp_path / 'new/db', tmp_path / 'cleanhome', FakeVault())
    preview = restore(other, archive)
    restore(other, archive, False, preview['preview_id'], True)
    item = other.get('asset', resource['id'])
    with pytest.raises(ConfigurationError, match='Review'):
        other.plan({'host_id': 'codex', 'asset_ids': [item['id']]})
    other._probe_sdk = AsyncMock(side_effect=AssertionError('Probe must stop before connecting'))
    with pytest.raises(ConfigurationError, match='Review'):
        await other.probe_asset(item['id'], True)
    item = other.save('asset', {'name': 'Rename only', 'config': item['config']}, item['id'], item['version'])
    with pytest.raises(ConfigurationError, match='Review'):
        other.plan({'host_id': 'codex', 'asset_ids': [item['id']]})
    account = other.credential('account', account['id'], 'synthetic-new-value')
    item = other.save('asset', {'config': item['config'], 'mcp_reviewed': True, 'mcp_account_bindings': [
        {'account_id': account['id'], 'name': 'Authorization', 'location': 'headers', 'bearer': True}]}, item['id'], item['version'])
    plan = other.plan({'host_id': 'codex', 'asset_ids': [item['id']]})
    assert 'synthetic-new-value' not in json.dumps(plan)
    assert plan['payload']['metadata']['entity_versions'][account['id']] == account['version']
    other.credential('account', account['id'], 'rotated-fixture-key')
    with pytest.raises(ConfigurationError, match='changed since preview'):
        other.apply(plan['id'], plan['version'], True)
    apply(other, {'host_id': 'codex', 'asset_ids': [item['id']]})
    assert 'Bearer rotated-fixture-key' in (other.home / '.codex/config.toml').read_text()


def test_replacement_checks_content_when_a_backup_recreates_the_same_id_and_version(manager):
    resource = mcp(manager)
    original = manager.export()
    replacement = copy.deepcopy(original)
    replacement['objects'][0]['data']['name'] = 'Reviewed replacement'
    planned = restore(manager, replacement, replace_existing=True)
    manager.delete('asset', resource['id'], resource['version'])
    different = copy.deepcopy(original)
    different['objects'][0]['data']['name'] = 'Recreated elsewhere'
    fresh = restore(manager, different)
    restore(manager, different, False, fresh['preview_id'], True)
    assert manager.get('asset', resource['id'])['version'] == resource['version']
    with pytest.raises(ConfigurationError, match='changed since'):
        restore(manager, replacement, True, planned['preview_id'], True)
    assert manager.get('asset', resource['id'])['name'] == 'Recreated elsewhere'


def test_global_matrix_shows_all_surfaces_and_shared_updates(manager, monkeypatch):
    from app.services.management_service import HOSTS
    api = FastAPI()
    api.include_router(manage.router)
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    client = TestClient(api)
    asset = manager.save('asset', {'name': 'Release checklist', 'kind': 'skill', 'files': {'SKILL.md': '# Review'}})
    apply(manager, {'host_id': 'codex', 'asset_ids': [asset['id']]})
    manager.save('asset', {'files': {'SKILL.md': '# Revised'}}, asset['id'], 1)
    params = {'host_ids': ','.join(HOSTS), 'kind': 'skill'}
    assert client.get('/api/manage/shared-resource-matrix', params=params).status_code == 422
    response = client.get('/api/manage/shared-resource-matrix', params={**params, 'scope': 'global'})
    assert response.status_code == 200, response.text
    item, = response.json()['items']
    assert set(item['cells']) == set(HOSTS)
    assert item['cells']['codex']['status'] == item['cells']['codex']['status'] == 'update_available'
    assert item['cells']['codex']['applied_version'] == item['cells']['codex']['applied_version'] == 1
    assert item['cells']['cursor']['status'] == 'not_managed'
    assert 'files' not in item


def test_global_matrix_keeps_other_readbacks_when_one_host_cannot_be_inspected(manager, monkeypatch):
    api = FastAPI()
    api.include_router(manage.router)
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    real_state = manager.host_state
    def state(host):
        if host == 'cursor':
            raise OSError('Private fixture details')
        return real_state(host)
    monkeypatch.setattr(manager, 'host_state', state)
    mcp(manager)
    response = TestClient(api).get('/api/manage/shared-resource-matrix', params={
        'host_ids': 'codex,cursor', 'kind': 'mcp', 'scope': 'global'})
    assert response.status_code == 200
    cells = response.json()['items'][0]['cells']
    assert cells['codex']['status'] == 'not_managed' and cells['cursor']['status'] == 'not_checked'
    assert 'Private fixture' not in response.text


def test_mixed_global_matrix_filters_and_paginates_before_host_reads(manager, monkeypatch):
    api = FastAPI()
    api.include_router(manage.router)
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    client = TestClient(api)
    for idx in range(15):
        mcp(manager, 'Tool '+str(idx))
    skill = manager.save('asset', {'name':'Review guide','kind':'skill','files':{'SKILL.md':'# Review'}})
    prompt = manager.save('asset', {'name':'Review policy','kind':'prompt','content':'Ask about scope'})
    calls = []
    state = manager.host_state
    def read(host_id):
        calls.append(host_id)
        return state(host_id)
    monkeypatch.setattr(manager,'host_state',read)
    path = '/api/manage/shared-resource-matrix'
    query = {'host_ids':'codex,opencode', 'scope':'global', 'kind':'all'}
    first = client.get(path, params=query).json()
    assert first['total'] == 17 and len(first['items']) == 15 and first['has_more']
    assert calls == ['codex','opencode']
    calls.clear()
    second = client.get(path, params={**query,'offset':15}).json()
    assert [row['id'] for row in second['items']] == [skill['id'],prompt['id']]
    by_id = {row['id']: row for row in second['items']}
    assert by_id[skill['id']]['cells']['opencode']['status'] == 'not_managed'
    assert by_id[prompt['id']]['cells']['opencode']['status'] == 'unsupported'
    assert all(row['cells']['codex']['status'] == 'not_managed' for row in second['items'])
    assert calls == ['codex','opencode']
    calls.clear()
    # A host with no configured root for the requested kind is skipped without a host read.
    blocked = client.get(path, params={'host_ids':'codex,opencode', 'scope':'global', 'kind':'prompt'}).json()
    assert blocked['items'][0]['cells']['opencode']['status'] == 'unsupported'
    assert calls == ['codex']
    focused = client.get(path,params={**query,'asset_id':prompt['id']}).json()
    assert focused['total'] == 1 and focused['items'][0]['kind'] == 'prompt'
    assert client.get(path,params={**query,'scope':'selected'}).status_code == 422
