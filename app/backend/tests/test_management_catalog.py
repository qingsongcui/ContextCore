"""Paging must not materialize full libraries or change assignment semantics."""
import json
import time
import tracemalloc

import pytest

from app.integrations.config_transactions import ConfigurationError
from tests.test_management_experience import manager, activate


def tool(manager, name, agents=None):
    data = {'name': name, 'kind': 'mcp', 'config': {'command': 'fixture-command', 'args': ['opaque omitted body']}}
    if agents is not None:
        data['agent_ids'] = agents
    return manager.save('asset', data)


def test_summary_is_scoped_searchable_and_never_exposes_bodies_or_secret_references(manager):
    ours = tool(manager, 'CAFÉ research_100%', ['codex'])
    tool(manager, 'Other assistant', ['omp'])
    prompt = manager.save('asset', {'name': 'Writing', 'kind': 'prompt', 'content': 'BODY_MUST_NOT_ENTER_SUMMARY', 'agent_ids': ['codex']})
    page = manager.catalog_page('asset', agent_id='codex', resource_kind='mcp', query='café')
    assert page['total'] == 1 and page['items'][0]['id'] == ours['id']
    assert page['items'][0]['_summary'] is True
    assert 'config' not in page['items'][0] and 'files' not in page['items'][0] and 'content' not in page['items'][0]
    assert manager.catalog_page('asset', agent_id='codex', query='_100%')['total'] == 1
    assert manager.catalog_page('asset', agent_id='codex', query='no_match')['total'] == 0
    assert 'BODY_MUST_NOT_ENTER_SUMMARY' not in json.dumps(manager.catalog_page('asset'))
    assert manager.get('asset', prompt['id'])['content'] == 'BODY_MUST_NOT_ENTER_SUMMARY'


def test_owned_entries_remain_visible_without_metadata_and_available_library_is_the_complement(manager):
    ours = tool(manager, 'Installed legacy')
    foreign = tool(manager, 'Available elsewhere', ['omp'])
    plan = manager.plan({'host_id': 'codex', 'asset_ids': [ours['id']]})
    manager.apply(plan['id'], 1, True)
    assert [item['id'] for item in manager.catalog_page('asset', agent_id='codex')['items']] == [ours['id']]
    assert [item['id'] for item in manager.catalog_page('asset', agent_id='codex', scope='available')['items']] == [foreign['id']]


def test_legacy_provider_binding_and_explicit_empty_scope_remain_distinct(manager):
    provider = manager.save('provider', {'name': 'Legacy', 'protocol': 'openai', 'base_url': 'https://fixture.example/v1'})
    model = manager.save('model', {'name': 'Fixture model', 'provider_id': provider['id'], 'model_id': 'native'})
    manager.save('binding', {'host_id': 'omp', 'provider_id': provider['id'], 'model_id': model['id']})
    assert manager.catalog_page('provider', agent_id='omp')['total'] == 1
    assert manager.catalog_page('model', agent_id='omp')['total'] == 1
    assert manager.catalog_page('binding', agent_id='codex')['total'] == 0
    manager.save('provider', {'agent_ids': []}, provider['id'], provider['version'])
    assert manager.catalog_page('provider', agent_id='omp')['total'] == 0


def test_current_activation_is_visible_and_provider_keys_are_only_reported_as_presence(manager):
    result = activate(manager)
    provider = result['provider']
    manager.save('provider', {'agent_ids': []}, provider['id'], provider['version'])
    page = manager.catalog_page('provider', agent_id='codex')
    assert page['total'] == 1 and page['items'][0]['has_credential'] is True
    assert 'credential_ref' not in page['items'][0]
    assert 'keychain:' not in json.dumps(page)


def test_large_library_page_does_not_decode_or_allocate_full_asset_bodies(manager, monkeypatch):
    count = 1000
    body = 'DO_NOT_DOWNLOAD_SKILL_BODY\n' + 'synthetic reference text\n' * 1600
    with manager.connect() as conn:
        conn.executemany('INSERT INTO management_entities VALUES(?,?,?,1,?,?)', [
            (f'asset_{i:016x}', 'asset', json.dumps({'name': f'Fixture {i:04}', 'kind': 'skill', 'files': {'SKILL.md': body}, 'agent_ids': ['codex' if i % 2 == 0 else 'omp']}), '2026-10-26', '2026-10-26') for i in range(count)])
    monkeypatch.setattr(manager, '_row', lambda *_: (_ for _ in ()).throw(AssertionError('List must not decode a full entity')))
    tracemalloc.start()
    started = time.perf_counter()
    try:
        page = manager.catalog_page('asset', agent_id='codex', resource_kind='skill', limit=25, offset=25)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page['total'] == 500 and len(page['items']) == 25 and page['has_more'] is True
    assert page['items'][0]['name'] == 'Fixture 0050'
    encoded = json.dumps(page).encode()
    assert b'DO_NOT_DOWNLOAD_SKILL_BODY' not in encoded and len(encoded) < 30000
    assert peak < 8 * 1024 * 1024
    assert time.perf_counter() - started < 5
    last = manager.catalog_page('asset', agent_id='codex', limit=25, offset=500)
    assert last['items'] == [] and last['has_more'] is False


@pytest.mark.parametrize('options', [{'limit': 101}, {'limit': 0}, {'offset': -1}, {'agent_id': 'unknown'}, {'scope': 'available'}, {'resource_kind': 'unknown'}])
def test_invalid_catalog_scopes_and_budgets_fail_explicitly(manager, options):
    with pytest.raises(ConfigurationError):
        manager.catalog_page('asset', **options)


def test_summary_api_is_opt_in_and_the_existing_detail_contract_remains_intact(manager, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.manage import router
    from app.services import management_service
    asset = tool(manager, 'Fixture', ['codex'])
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert isinstance(client.get('/api/manage/assets').json(), list)
    response = client.get('/api/manage/assets?summary=true&agent_id=codex&kind=mcp&limit=1')
    assert response.status_code == 200 and response.json()['total'] == 1
    assert 'config' not in response.json()['items'][0]
    assert client.get('/api/manage/assets/' + asset['id']).json()['config'] == asset['config']
    assert client.get('/api/manage/assets?summary=true&limit=101').status_code == 422


def test_version_pages_do_not_load_bodies_and_a_selected_revision_remains_available(manager, monkeypatch):
    asset = manager.save('asset', {'name': 'History fixture', 'kind': 'skill', 'files': {'SKILL.md': '# Current'}, 'agent_ids': ['codex']})
    body = {'name': 'History fixture', 'kind': 'skill', 'files': {'SKILL.md': 'DO_NOT_LOAD_ALL_REVISIONS\n' * 2000}}
    with manager.connect() as conn:
        conn.executemany('INSERT INTO management_asset_versions VALUES(?,?,?,?)', [(asset['id'], i, json.dumps(body), '2026-10-26') for i in range(2, 202)])
    monkeypatch.setattr(manager, '_row', lambda *_: (_ for _ in ()).throw(AssertionError('Version list must not load the current full body')))
    tracemalloc.start()
    try:
        page = manager.asset_version_page(asset['id'], limit=20, offset=180)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page['total'] == 201 and len(page['items']) == 20 and page['has_more']
    assert [item['version'] for item in page['items']] == list(range(21, 1, -1))
    assert 'DO_NOT_LOAD' not in json.dumps(page) and peak < 4 * 1024 * 1024
    assert manager.asset_revision(asset['id'], 100)['data'] == body
    with pytest.raises(ConfigurationError):
        manager.asset_revision(asset['id'], 202)
    for options in ({'limit': 0}, {'limit': 101}, {'offset': -1}):
        with pytest.raises(ConfigurationError):
            manager.asset_version_page(asset['id'], **options)


def test_version_api_pages_metadata_fetches_one_body_and_refuses_deleted_orphan_history(manager, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.manage import router
    from app.services import management_service
    asset = tool(manager, 'Old fixture', ['codex'])
    manager.save('asset', {'name': 'New fixture'}, asset['id'], asset['version'])
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    url = '/api/manage/assets/' + asset['id'] + '/versions'
    page = client.get(url + '?summary=true&limit=1').json()
    assert page['total'] == 2 and page['has_more'] and 'data' not in page['items'][0]
    assert client.get(url + '/1').json()['data']['name'] == 'Old fixture'
    assert client.get(url + '?summary=true&offset=1&limit=1').json()['items'][0]['version'] == 1
    assert isinstance(client.get(url).json(), list)  # Existing explicit full-history API.
    assert client.get(url + '?summary=true&limit=101').status_code == 422
    with manager.connect() as conn:
        conn.execute('DELETE FROM management_entities WHERE id=?', (asset['id'],))
    assert client.get(url + '?summary=true').status_code == 404
    assert client.get(url + '/1').status_code == 404


def test_probe_pins_the_reviewed_definition_before_any_network_or_process(manager, monkeypatch):
    import asyncio
    asset = tool(manager, 'Reviewed tool', ['codex'])
    calls = []
    async def probe(asset_id, config):
        calls.append((asset_id, config))
        return {'status': 'verified'}
    monkeypatch.setattr(manager, '_probe_sdk', probe)
    manager.save('asset', {'config': {'command': 'changed-command', 'args': []}}, asset['id'], asset['version'])
    for expected in (1, True, '2'):
        with pytest.raises(ConfigurationError) as error:
            asyncio.run(manager.probe_asset(asset['id'], True, expected_version=expected))
        assert error.value.status_code == 409 and calls == []
    assert asyncio.run(manager.probe_asset(asset['id'], True, expected_version=2))['status'] == 'verified'
    assert calls[0][1]['command'] == 'changed-command'


def test_bounded_identifier_lookup_and_account_provider_choices(manager):
    provider = manager.save('provider', {'name': 'Fixture provider', 'protocol': 'openai', 'base_url': 'https://fixture.example/v1'})
    unbound = manager.save('account', {'name': 'Unbound fixture', 'kind': 'api_key'})
    owned = manager.save('account', {'name': 'Provider account', 'kind': 'api_key', 'provider_id': provider['id']})
    foreign_provider = manager.save('provider', {'name': 'Other provider', 'protocol': 'openai', 'base_url': 'https://other.example/v1'})
    manager.save('account', {'name': 'Other account', 'kind': 'api_key', 'provider_id': foreign_provider['id']})
    page = manager.catalog_page('account', provider_id=provider['id'], include_unbound=True)
    assert {item['id'] for item in page['items']} == {unbound['id'], owned['id']}
    assert [item['id'] for item in manager.catalog_page('account', provider_id='', include_unbound=True)['items']] == [unbound['id']]
    assert [item['id'] for item in manager.catalog_page('account', entity_ids=[owned['id']])['items']] == [owned['id']]
    assert manager.catalog_page('account', entity_ids=[])['total'] == 0
    for ids in ([unbound['id']] * 101, [None], ['a' * 101]):
        with pytest.raises(ConfigurationError):
            manager.catalog_page('account', entity_ids=ids)
    with pytest.raises(ConfigurationError):
        manager.catalog_page('provider', include_unbound=True)


def test_assignment_preserves_all_legacy_bindings_without_downloading_them_to_the_browser(manager):
    provider = manager.save('provider', {'name': 'Legacy provider', 'protocol': 'openai', 'base_url': 'https://fixture.example/v1', 'external_metadata': {'keep': True}})
    model = manager.save('model', {'name': 'Fixture model', 'provider_id': provider['id'], 'model_id': 'model'})
    for host in ('omp', 'claude-code'):
        manager.save('binding', {'host_id': host, 'provider_id': provider['id'], 'model_id': model['id']})
    result = manager.assign_agent('provider', provider['id'], 'codex', True, provider['version'])
    assert set(result['agent_ids']) == {'omp', 'claude-code', 'codex'}
    assert set(result) == {'id', 'version', 'agent_ids'}
    saved = manager.get('provider', provider['id'])
    assert saved['external_metadata'] == {'keep': True}
    with pytest.raises(ConfigurationError) as error:
        manager.assign_agent('provider', provider['id'], 'codex', False, provider['version'])
    assert error.value.status_code == 409
    result = manager.assign_agent('provider', provider['id'], 'codex', False, result['version'])
    assert set(result['agent_ids']) == {'omp', 'claude-code'}
    saved = manager.save('provider', {'agent_ids': []}, provider['id'], result['version'])
    result = manager.assign_agent('provider', provider['id'], 'codex', True, saved['version'])
    assert result['agent_ids'] == ['codex']  # Explicit empty membership is not legacy inference.


def test_summary_assignment_api_does_not_return_a_full_skill_and_invalidates_stale_versions(manager, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.manage import router
    from app.services import management_service
    asset = manager.save('asset', {'name': 'Fixture skill', 'kind': 'skill', 'agent_ids': ['omp'], 'files': {'SKILL.md': '# PRIVATE_SYNTHETIC_BODY'}})
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    url = '/api/manage/assets/' + asset['id']
    response = client.post(url + '/assignment', json={'host_id': 'codex', 'selected': True, 'expected_version': 1})
    assert response.status_code == 200 and response.json()['agent_ids'] == ['omp', 'codex']
    assert 'PRIVATE_SYNTHETIC_BODY' not in response.text
    assert client.post(url + '/assignment', json={'host_id': 'codex', 'selected': False, 'expected_version': 1}).status_code == 409
    assert client.post(url + '/assignment', json={'host_id': 'codex', 'selected': 'yes', 'expected_version': 2}).status_code == 422
    assert client.get('/api/manage/assets?summary=true&ids=' + asset['id']).json()['items'][0]['id'] == asset['id']
    assert client.get('/api/manage/assets?summary=true&ids=').json()['items'] == []


def test_host_status_does_not_echo_or_allocate_the_entire_unmanaged_library(manager):
    result = activate(manager)
    with manager.connect() as conn:
        conn.executemany('INSERT INTO management_entities VALUES(?,?,?,1,?,?)', [
            (f'asset_unmanaged_{i:05}', 'asset', json.dumps({'name': f'Unmanaged {i}', 'kind': 'skill', 'files': {'SKILL.md': '# Unmanaged body\n' * 200}, 'agent_ids': ['codex']}), '2026-10-26', '2026-10-26') for i in range(3000)])
    tracemalloc.start()
    try:
        state = manager.host_state('codex')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert state['selection']['status'] == 'verified'
    assert result['provider']['id'] in state['resources']
    assert len(state['resources']) <= 3 and len(json.dumps(state)) < 10000
    assert peak < 8 * 1024 * 1024


def test_plan_headers_are_scoped_paged_and_do_not_decode_snapshots(manager, monkeypatch):
    body = 'DO_NOT_LOAD_PLAN_SNAPSHOTS\n' * 1000
    with manager.connect() as conn:
        conn.executemany('INSERT INTO management_plans VALUES(?,1,?,?,?,?)', [
            (f'plan_{i:05}', 'planned', json.dumps({'metadata': {'host_id': 'codex' if i < 250 else 'omp', 'operation': 'install'}, 'files': [{'snapshot': body}]}), '2026-10-26', '2026-10-26') for i in range(500)])
    monkeypatch.setattr(manager.transactions, 'get', lambda *_: (_ for _ in ()).throw(AssertionError('List must not decode a complete plan')))
    tracemalloc.start()
    try:
        page = manager.plan_page('codex', limit=50, offset=200)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert page['total'] == 250 and len(page['items']) == 50 and not page['has_more']
    assert all(row['payload']['metadata']['host_id'] == 'codex' for row in page['items'])
    assert 'DO_NOT_LOAD_PLAN_SNAPSHOTS' not in json.dumps(page) and peak < 8 * 1024 * 1024
    assert page['items'][-1]['id'] == 'plan_00000'
    with pytest.raises(ConfigurationError):
        manager.plan_page('unknown')


def test_plan_headers_api_preserves_single_plan_review_and_host_validation(manager, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.manage import router
    from app.services import management_service
    asset = tool(manager, 'Review fixture', ['codex'])
    planned = manager.plan({'host_id': 'codex', 'asset_ids': [asset['id']]})
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    page = client.get('/api/manage/plans?summary=true&agent_id=codex&limit=1').json()
    assert page['items'][0]['id'] == planned['id'] and 'files' not in page['items'][0]['payload']
    assert client.get('/api/manage/plans/' + planned['id']).json()['payload']['files']
    assert client.get('/api/manage/plans?summary=true&agent_id=omp').json()['total'] == 0
    assert client.get('/api/manage/plans?summary=true&agent_id=unknown').status_code == 422


def test_oversized_configuration_receipts_do_not_expand_status_memory_or_claim_full_coverage(manager):
    with manager.connect() as conn:
        conn.execute('INSERT INTO management_plans VALUES(?,1,?,?,?,?)', ('plan_large_metadata', 'applied', json.dumps({'metadata': {'host_id': 'codex', 'operation': 'install', 'extra_fixture': 'x' * (5 * 1024 * 1024)}, 'files': []}), '2026-10-26', '2026-10-26'))
    tracemalloc.start()
    try:
        state = manager.host_state('codex')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert state['coverage'] == 'partial' and state['resources'] == {}
    assert peak < 8 * 1024 * 1024 and len(json.dumps(state)) < 5000


def test_provider_summary_exposes_default_model_and_cc_switch_current_hosts(manager):
    provider = manager.save('provider', {
        'name': 'DeepSeek',
        'protocol': 'anthropic',
        'base_url': 'https://api.deepseek.com/anthropic',
        'model_map': {'default': 'deepseek-v4-flash[1M]'},
        'agent_ids': ['claude-code'],
        'cc_switch_current_hosts': ['claude-code'],
    })
    page = manager.catalog_page('provider', agent_id='claude-code')
    assert page['total'] == 1
    item = page['items'][0]
    assert item['id'] == provider['id']
    assert item['default_model'] == 'deepseek-v4-flash[1M]'
    assert item['cc_switch_current_hosts'] == ['claude-code']
    assert 'model_map' not in item

