"""Per-assistant modes use synthetic homes, fake vaults and mock HTTP only."""
import copy
import asyncio
import json
import tomllib

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import create_schema
from app.integrations.config_transactions import ConfigurationError
from app.integrations.secret_store import SecretUnavailable
from app.routers.manage import router as manage_router, public_plan
from app.routers.routing import router
from app.services import management_service, routing_service, usage_service
from app.services.host_routing import HostRouting, DEFAULT_ALIAS
from tests.test_management_experience import manager, setup


ORIGIN = 'http://127.0.0.1:18931'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def provider(manager, host='codex', token='setup-request-0001', protocol='openai'):
    body = setup(host, token)
    if host == 'kimi-code':
        body['context_window'] = 32768
    body['provider'].update(protocol=protocol, supported_endpoints=['messages'] if protocol == 'anthropic' else ['chat/completions', 'responses'])
    return manager.setup_provider(body)


def choice(value):
    return {'provider_id': value['provider']['id'], 'model_id': value['model']['id']}


def preview(manager, value, host='codex', **patch):
    return manager.preview_connection(host, {'expected_version': manager.connection_mode(host)['version'], 'mode': 'routing', 'endpoint': 'messages' if host == 'claude-code' else 'responses', 'target': choice(value), **patch}, ORIGIN)


def apply(manager, plan):
    return manager.apply(plan['id'], plan['version'], True)


def token(manager, host='codex'):
    value = HostRouting(manager).private_state(host)
    return manager.vault.get(value['gateway']['credential_ref'])


def auth(manager, host='codex'):
    return {'authorization': 'Bearer ' + token(manager, host)}


@pytest.fixture
def client(manager, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(manager.db_path))
    create_schema(manager.db_path)
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    app = FastAPI(); app.include_router(router); app.include_router(manage_router)
    return TestClient(app, base_url=ORIGIN)


def fake_upstream(monkeypatch, handler):
    monkeypatch.setattr(routing_service, 'client_factory', lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout, trust_env=False))


def response(request):
    value = json.loads(request.content)
    return httpx.Response(200, json={'id': 'fixture-response', 'object': 'response', 'status': 'completed', 'model': value['model'], 'output': [], 'usage': {'input_tokens': 2, 'output_tokens': 1}})


def test_view_and_preview_do_not_write_native_files_or_create_gateway_secrets(manager):
    value = provider(manager)
    before = copy.deepcopy(manager.vault.values)
    assert manager.connection_mode('codex')['mode'] == 'direct'
    plan = preview(manager, value)
    assert plan['status'] == 'planned' and manager.connection_mode('codex')['version'] == 0
    assert manager.vault.values == before and not list(manager.home.iterdir())
    assert 'host_routing' not in public_plan(plan)['payload']['metadata']
    assert 'keychain:' not in json.dumps(public_plan(plan))
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'], plan['version'], False)
    assert manager.vault.values == before


def test_native_gateway_apply_then_return_to_retained_direct_revokes_token(manager, client):
    value = provider(manager)
    direct = manager.plan({'host_id': 'codex', **choice(value), 'activate_provider': True})
    apply(manager, direct)
    original = tomllib.loads((manager.home / '.codex/config.toml').read_text())
    plan = preview(manager, value)
    apply(manager, plan)
    native = tomllib.loads((manager.home / '.codex/config.toml').read_text())
    gateway = native['model_providers'][native['model_provider']]
    assert gateway['base_url'] == ORIGIN + '/api/routing/v1/hosts/codex'
    assert gateway['http_headers']['Authorization'] == 'Bearer ' + token(manager)
    assert token(manager) != setup()['secret'] and native['model'] == DEFAULT_ALIAS
    assert manager.connection_mode('codex')['mode'] == 'routing'
    old_auth = auth(manager)
    restore = manager.plan({'host_id': 'codex', 'operation': 'restore'})
    apply(manager, restore)
    restored = tomllib.loads((manager.home / '.codex/config.toml').read_text())
    assert restored['model_provider'] == original['model_provider'] and restored['model'] == original['model']
    assert set(restored['model_providers']) == set(original['model_providers'])
    assert manager.connection_mode('codex')['mode'] == 'direct'
    assert client.post('/api/routing/v1/hosts/codex/responses', headers=old_auth, json={'model': DEFAULT_ALIAS}).status_code == 401
    assert list(manager.vault.values.values()) == [setup()['secret']]


def test_original_unmanaged_selection_is_restored_when_no_direct_selection_was_saved(manager):
    path = manager.home / '.codex/config.toml'; path.parent.mkdir()
    original = 'model_provider = "original"\nmodel = "original-model"\n[foreign]\nkeep = true\n'
    path.write_text(original)
    value = provider(manager)
    apply(manager, preview(manager, value))
    restore = manager.plan({'host_id': 'codex', 'operation': 'restore'})
    apply(manager, restore)
    restored = tomllib.loads(path.read_text())
    assert restored == tomllib.loads(original)


def test_two_profiles_tokens_targets_and_circuits_do_not_cross_hosts(manager, client, monkeypatch):
    a = provider(manager)
    b = provider(manager, 'opencode', 'setup-request-0002')
    apply(manager, preview(manager, a, circuit_threshold=1))
    apply(manager, preview(manager, b, 'opencode'))
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(429) if len(seen) == 1 else response(request)
    fake_upstream(monkeypatch, handler)
    assert token(manager) != token(manager, 'opencode')
    assert client.post('/api/routing/v1/hosts/opencode/responses', headers=auth(manager), json={'model': DEFAULT_ALIAS}).status_code == 401
    assert client.post('/api/routing/v1/hosts/codex/responses', headers=auth(manager), json={'model': DEFAULT_ALIAS}).status_code == 429
    assert routing_service.circuit_status('codex')[a['provider']['id']]['state'] == 'open'
    assert routing_service.circuit_status('opencode') == routing_service.circuit_status() == {}
    assert client.post('/api/routing/v1/hosts/opencode/responses', headers=auth(manager, 'opencode'), json={'model': DEFAULT_ALIAS}).status_code == 200
    assert seen[1].headers['authorization'] == 'Bearer ' + setup()['secret']
    assert usage_service.requests({'host_id': 'opencode'})['total'] == 1


def test_model_alias_dispatch_and_catalog_are_explicit_without_fallback(manager, client, monkeypatch):
    a, b = provider(manager), provider(manager, token='setup-request-0002')
    body = {'expected_version': 0, 'mode': 'aggregation', 'endpoint': 'responses', 'members': [{**choice(a), 'alias': 'everyday'}, {**choice(b), 'alias': 'deep'}], 'default_alias': 'everyday'}
    apply(manager, manager.preview_connection('codex', body, ORIGIN))
    catalog = client.get('/api/routing/v1/hosts/codex/models', headers=auth(manager)).json()
    assert [item['id'] for item in catalog['data']] == ['deep', 'everyday']
    assert client.get('/api/routing/v1/hosts/codex/models').status_code == 401
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(429)
    fake_upstream(monkeypatch, handler)
    assert client.post('/api/routing/v1/hosts/codex/responses', headers=auth(manager), json={'model': 'unknown'}).status_code == 422
    assert calls == []
    result = client.post('/api/routing/v1/hosts/codex/responses', headers=auth(manager), json={'model': 'deep', 'input': 'synthetic'} )
    assert result.status_code == 429 and len(calls) == 1
    assert usage_service.requests()['items'][0]['provider_id'] == b['provider']['id']
    body.update(expected_version=1, members=[{**choice(b), 'alias': 'deep'}])
    with pytest.raises(ConfigurationError, match='another default'):
        manager.preview_connection('codex', body, ORIGIN)
    with pytest.raises(ConfigurationError, match='route/default'):
        manager.delete('provider', b['provider']['id'], b['provider']['version'])


def test_stale_profile_and_definition_previews_reject_before_credentials_or_files(manager):
    a, b = provider(manager), provider(manager, token='setup-request-0002')
    first, stale = preview(manager, a), preview(manager, b)
    apply(manager, first)
    before = (manager.home / '.codex/config.toml').read_bytes()
    with pytest.raises(ConfigurationError, match='settings changed since preview'):
        apply(manager, stale)
    assert (manager.home / '.codex/config.toml').read_bytes() == before
    next_plan = preview(manager, b)
    manager.save('provider', {'name': 'Updated'}, b['provider']['id'], b['provider']['version'])
    with pytest.raises(ConfigurationError, match='changed since preview'):
        apply(manager, next_plan)
    assert manager.connection_mode('codex')['pending'] is None


def test_definition_edit_requires_reapply_and_does_not_silently_redirect_active_route(manager, client, monkeypatch):
    value = provider(manager)
    apply(manager, preview(manager, value))
    manager.save('provider', {'base_url': 'https://different.example/v1'}, value['provider']['id'], value['provider']['version'])
    fake_upstream(monkeypatch, lambda _: pytest.fail('unapplied definition must not receive a request'))
    assert manager.connection_mode('codex')['definition_state'] == 'update_available'
    result = client.post('/api/routing/v1/hosts/codex/responses', headers=auth(manager), json={'model': DEFAULT_ALIAS})
    assert result.status_code == 409


def test_partial_file_failure_blocks_dispatch_and_recovery_restores_old_profile(manager, client, monkeypatch):
    value = provider(manager)
    plan = preview(manager, value)
    original_atomic = manager.transactions._atomic
    count = 0
    def fail(path, data):
        nonlocal count
        original_atomic(path, data); count += 1
        if count == 1:
            raise OSError('synthetic interrupted write')
    monkeypatch.setattr(manager.transactions, '_atomic', fail)
    with pytest.raises(ConfigurationError, match='recovery is required'):
        apply(manager, plan)
    state = manager.connection_mode('codex')
    assert state['mode'] == 'direct' and state['pending']['status'] == 'recovery_required'
    pending = HostRouting(manager).private_state('codex')['pending']
    new_token = manager.vault.get(pending['new_ref'])
    assert client.post('/api/routing/v1/hosts/codex/responses', headers={'authorization': 'Bearer ' + new_token}, json={'model': DEFAULT_ALIAS}).status_code in (401, 409)
    monkeypatch.setattr(manager.transactions, '_atomic', original_atomic)
    assert manager.recover_plan(plan['id'], True)['status'] == 'recovered'
    assert manager.connection_mode('codex')['pending'] is None and manager.connection_mode('codex')['version'] == 0
    assert not manager.transactions._read(manager.home / '.codex/config.toml')
    assert list(manager.vault.values.values()) == [setup()['secret']]


def test_gateway_token_is_absent_from_lists_exports_audit_and_plan_database(manager, client):
    value = provider(manager)
    plan = preview(manager, value)
    apply(manager, plan)
    secret = token(manager)
    assert secret.encode() not in manager.db_path.read_bytes()
    for endpoint in ['/api/manage/hosts/codex/mode', '/api/manage/plans', '/api/manage/plans/' + plan['id'], '/api/manage/export']:
        result = client.get(endpoint)
        assert result.status_code == 200 and secret not in result.text and 'keychain:senrelay:' not in result.text


def test_claude_native_key_header_and_interface_mismatch(manager, client, monkeypatch):
    value = provider(manager, 'claude-code', protocol='anthropic')
    apply(manager, preview(manager, value, 'claude-code'))
    native = json.loads((manager.home / '.claude/settings.json').read_text())
    key = native['env']['ANTHROPIC_AUTH_TOKEN']
    assert key == token(manager, 'claude-code')
    assert native['env']['ANTHROPIC_MODEL'] == DEFAULT_ALIAS
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path.endswith('/messages') and request.headers['x-api-key'] == setup()['secret']
        return httpx.Response(200, json={'type': 'message', 'model': 'fixture-model', 'content': [], 'usage': {'input_tokens': 2, 'output_tokens': 1}})
    fake_upstream(monkeypatch, handler)
    headers = {'x-api-key': key}
    assert client.post('/api/routing/v1/hosts/claude-code/responses', headers=headers, json={'model': DEFAULT_ALIAS}).status_code == 422
    assert calls == []
    assert client.post('/api/routing/v1/hosts/claude-code/messages', headers=headers, json={'model': DEFAULT_ALIAS, 'max_tokens': 1, 'messages': []}).status_code == 200


@pytest.mark.parametrize('host,endpoint', [('opencode', 'responses'), ('pi', 'chat/completions'), ('kimi-code', 'responses'), ('hermes', 'responses'), ('minimax-code', 'responses'), ('openclaw', 'responses')])
def test_each_supported_native_renderer_can_start_route_and_return_to_direct(manager, host, endpoint):
    value = provider(manager, host)
    if host == 'kimi-code':
        value['model'] = manager.save('model', {'context_window': 32768}, value['model']['id'], value['model']['version'])
    apply(manager, manager.plan({'host_id': host, **choice(value), 'activate_provider': True}))
    plan = preview(manager, value, host, endpoint=endpoint)
    apply(manager, plan)
    assert manager.connection_mode(host)['mode'] == 'routing'
    apply(manager, manager.plan({'host_id': host, 'operation': 'restore'}))
    assert manager.connection_mode(host)['mode'] == 'direct'
    assert manager.host_state(host)['selection']['model_id'] == 'fixture-model'


def test_safe_fallback_uses_reviewed_backup_model_and_linked_state_never_crosses_providers(manager, client, monkeypatch):
    a, b = provider(manager), provider(manager, token='setup-request-0002')
    b['model'] = manager.save('model', {'model_id': 'backup-native'}, b['model']['id'], b['model']['version'])
    apply(manager, preview(manager, a, fallbacks=[choice(b)], circuit_threshold=1))
    seen = []
    def handler(request):
        model = json.loads(request.content)['model']; seen.append(model)
        return httpx.Response(429) if model == 'fixture-model' else response(request)
    fake_upstream(monkeypatch, handler)
    path = '/api/routing/v1/hosts/codex/responses'
    assert client.post(path, headers=auth(manager), json={'model': DEFAULT_ALIAS, 'input': 'synthetic'}).status_code == 200
    assert seen == ['fixture-model', 'backup-native']
    # Even when the primary circuit is open, a previous response identifier
    # cannot be transplanted to the sole remaining backup candidate.
    assert client.post(path, headers=auth(manager), json={'model': DEFAULT_ALIAS, 'previous_response_id': 'prior-provider-state'}).status_code == 422
    assert seen == ['fixture-model', 'backup-native']
    rows = usage_service.requests()['items']
    assert len(rows) == 2 and len({row['logical_request_id'] for row in rows}) == 1
    assert all(row['requested_model'] == DEFAULT_ALIAS and row['host_id'] == 'codex' for row in rows)


@pytest.mark.anyio
async def test_route_change_affects_new_requests_and_keeps_in_flight_target(manager, client, monkeypatch):
    a, b = provider(manager), provider(manager, token='setup-request-0002')
    b['model'] = manager.save('model', {'model_id': 'new-target'}, b['model']['id'], b['model']['version'])
    apply(manager, preview(manager, a))
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content)['model'])
        if len(seen) == 1:
            entered.set(); await release.wait()
        return response(request)
    fake_upstream(monkeypatch, handler)
    class Request:
        headers = auth(manager)
        async def stream(self):
            yield json.dumps({'model': DEFAULT_ALIAS, 'input': 'synthetic'}).encode()
        async def is_disconnected(self):
            return False
    first = asyncio.create_task(routing_service.forward(Request(), 'responses', 'codex'))
    await asyncio.wait_for(entered.wait(), 2)
    token_before = token(manager)
    next_plan = preview(manager, b)
    assert next_plan['payload']['metadata']['connection_change']['client_configuration_changed'] is False
    apply(manager, next_plan)
    assert token(manager) == token_before
    release.set()
    assert (await first).status_code == 200
    assert (await routing_service.forward(Request(), 'responses', 'codex')).status_code == 200
    assert seen == ['fixture-model', 'new-target']


def test_mode_management_keeps_csrf_while_native_ingress_uses_host_token(manager, client, monkeypatch):
    from app.security import SecurityMiddleware
    value = provider(manager)
    apply(manager, preview(manager, value))
    app = FastAPI(); app.add_middleware(SecurityMiddleware); app.include_router(router); app.include_router(manage_router)
    secured = TestClient(app, base_url=ORIGIN, client=('127.0.0.1', 50000))
    assert secured.post('/api/manage/hosts/codex/mode/preview', json={}).status_code == 403
    assert secured.post('/api/routing/token', json={'confirmed': True}).status_code == 403
    fake_upstream(monkeypatch, response)
    assert secured.post('/api/routing/v1/hosts/codex/responses', json={'model': DEFAULT_ALIAS}, headers=auth(manager)).status_code == 200
    assert secured.post('/api/routing/v1/hosts/codex/responses', json={'model': DEFAULT_ALIAS}, headers={'x-csrf-token': 'browser-token'}).status_code == 401


def test_endpoint_edit_is_reported_in_native_selection_without_vault_access(manager):
    value = provider(manager)
    apply(manager, preview(manager, value))
    manager.vault.locked = True
    assert manager.host_state('codex')['selection']['connection_mode'] == 'routing'
    path = manager.home / '.codex/config.toml'
    path.write_text(path.read_text().replace(ORIGIN, 'http://127.0.0.1:19999'))
    assert manager.host_state('codex')['selection']['status'] == 'externally_changed'


@pytest.mark.anyio
async def test_definition_change_during_body_upload_is_rejected_before_vault_read(manager, client, monkeypatch):
    value = provider(manager)
    account = manager.save('account', {'name': 'Synthetic API account', 'kind': 'api_key', 'provider_id': value['provider']['id']})
    account = manager.credential('account', account['id'], 'synthetic-active-account')
    value['provider'] = manager.save('provider', {'account_id': account['id'], 'credential_source': 'account'}, value['provider']['id'], value['provider']['version'])
    apply(manager, preview(manager, value))
    class Request:
        headers = auth(manager)
        async def stream(self):
            manager.save('account', {'name': 'Account changed during upload'}, account['id'], account['version'])
            yield json.dumps({'model': DEFAULT_ALIAS}).encode()
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('changed account must not reach the vault'))
    from fastapi import HTTPException
    with pytest.raises(HTTPException, match='definition changed'):
        await routing_service.forward(Request(), 'responses', 'codex')


def test_old_retained_provider_is_not_pinned_after_returning_to_direct_and_selecting_another(manager):
    a, b = provider(manager), provider(manager, token='setup-request-0002')
    apply(manager, manager.plan({'host_id': 'codex', **choice(a), 'activate_provider': True}))
    apply(manager, preview(manager, b))
    apply(manager, manager.plan({'host_id': 'codex', 'operation': 'restore'}))
    apply(manager, manager.plan({'host_id': 'codex', **choice(b), 'activate_provider': True}))
    apply(manager, manager.plan({'host_id': 'codex', 'provider_id': a['provider']['id'], 'operation': 'uninstall'}))
    manager.delete('model', a['model']['id'], a['model']['version'])
    assert manager.delete('provider', a['provider']['id'], a['provider']['version'])['deleted']
    assert manager.host_state('codex')['selection']['provider_id'] == b['provider']['id']


def test_failed_final_commit_keeps_pending_profile_and_recovery_reverts_files_and_ownership(manager):
    value = provider(manager)
    plan = preview(manager, value)
    with manager.connect() as conn:
        conn.execute("CREATE TRIGGER fail_mode_commit BEFORE UPDATE OF data ON management_routing_profiles BEGIN SELECT RAISE(ABORT,'fixture commit error'); END")
    with pytest.raises(ConfigurationError, match='recovery is required'):
        apply(manager, plan)
    with manager.connect() as conn:
        assert conn.execute('SELECT count(*) FROM management_owned').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM management_activations').fetchone()[0] == 0
    assert manager.connection_mode('codex')['mode'] == 'direct'
    assert manager.connection_mode('codex')['pending']['status'] == 'recovery_required'
    assert manager.recover_plan(plan['id'], True)['status'] == 'recovered'
    assert manager.connection_mode('codex')['pending'] is None
    assert list(manager.vault.values.values()) == [setup()['secret']]


def test_locked_or_partially_failed_gateway_vault_write_leaves_no_half_profile(manager, monkeypatch):
    value = provider(manager)
    plan = preview(manager, value)
    manager.vault.locked = True
    assert manager.connection_mode('codex')['mode'] == 'direct'
    with pytest.raises(SecretUnavailable):
        apply(manager, plan)
    assert manager.connection_mode('codex')['pending'] is None
    assert not list(manager.home.iterdir())
    manager.vault.locked = False
    original = manager.vault.put
    def partial(secret):
        reference = original(secret)
        raise SecretUnavailable('synthetic partial write', reference=reference)
    monkeypatch.setattr(manager.vault, 'put', partial)
    with pytest.raises(SecretUnavailable):
        apply(manager, plan)
    assert list(manager.vault.values.values()) == [setup()['secret']]
    assert manager.connection_mode('codex')['pending'] is None


@pytest.mark.parametrize('patch', [
    {'fallbacks': [None]}, {'fallbacks': [{}] * 9}, {'request_timeout_seconds': True},
    {'endpoint': 'messages'}, {'members': []}, {'unexpected': 1}, {'expected_version': True},
])
def test_invalid_mode_input_creates_no_partial_state(manager, patch):
    value = provider(manager)
    if patch == {'members': []}:
        patch = {'mode': 'aggregation', **patch}
    with pytest.raises(ConfigurationError):
        preview(manager, value, **patch)
    assert manager.connection_mode('codex')['version'] == 0 and not list(manager.home.iterdir())


def assert_mode_write_is_not_loaded_or_callable(state):
    assert state['effects']['configuration'] in {'verified', 'update_available', 'externally_changed', 'not_managed'}
    assert state['effects']['host_loaded'] == 'not_verified'
    assert state['effects']['reachability'] == 'not_checked'
    assert state['effects']['authentication'] == 'not_checked'
    assert state['effects']['invocation'] == 'not_checked'
    assert state['credentials_read'] is False and state['host_executed'] is False
    assert state['selection']['host_loaded'] == 'not_verified'


def test_direct_routed_aggregation_and_fallback_file_write_is_not_a_loaded_or_callable_claim(manager, client, monkeypatch):
    primary = provider(manager)
    backup = provider(manager, token='setup-request-0002')
    backup['model'] = manager.save('model', {'model_id': 'backup-native'}, backup['model']['id'], backup['model']['version'])
    fake_upstream(monkeypatch, response)
    path = '/api/routing/v1/hosts/codex/responses'

    apply(manager, manager.plan({'host_id': 'codex', **choice(primary), 'activate_provider': True}))
    direct = manager.host_state('codex')
    assert direct['selection']['connection_mode'] == 'direct'
    assert_mode_write_is_not_loaded_or_callable(direct)
    assert HostRouting(manager).private_state('codex').get('gateway') in (None, {})

    apply(manager, preview(manager, primary))
    routed = manager.host_state('codex')
    assert routed['selection']['connection_mode'] == 'routing'
    assert_mode_write_is_not_loaded_or_callable(routed)
    assert client.post(path, headers=auth(manager), json={'model': DEFAULT_ALIAS, 'input': 'synthetic'}).status_code == 200
    assert_mode_write_is_not_loaded_or_callable(manager.host_state('codex'))

    body = {'expected_version': manager.connection_mode('codex')['version'], 'mode': 'aggregation', 'endpoint': 'responses',
            'members': [{**choice(primary), 'alias': 'everyday'}, {**choice(backup), 'alias': 'deep'}], 'default_alias': 'everyday'}
    apply(manager, manager.preview_connection('codex', body, ORIGIN))
    aggregated = manager.host_state('codex')
    assert aggregated['selection']['connection_mode'] == 'aggregation'
    assert_mode_write_is_not_loaded_or_callable(aggregated)
    assert client.post(path, headers=auth(manager), json={'model': 'deep', 'input': 'synthetic'}).status_code == 200
    assert_mode_write_is_not_loaded_or_callable(manager.host_state('codex'))

    apply(manager, preview(manager, primary, fallbacks=[choice(backup)], expected_version=manager.connection_mode('codex')['version']))
    fallback = manager.host_state('codex')
    assert fallback['selection']['connection_mode'] == 'routing'
    assert_mode_write_is_not_loaded_or_callable(fallback)
    assert client.post(path, headers=auth(manager), json={'model': DEFAULT_ALIAS, 'input': 'synthetic'}).status_code == 200
    after_call = manager.host_state('codex')
    assert_mode_write_is_not_loaded_or_callable(after_call)
    assert after_call['effects']['configuration'] == 'verified'
