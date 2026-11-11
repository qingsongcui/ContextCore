"""Account-backed native configuration, metadata and owned removal journeys."""
import asyncio
import json
import tomllib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.integrations.config_transactions import ConfigurationError
from app.services import management_service
from tests.test_management import manager
from tests.test_management_oauth import oauth_env


def create(manager, host='codex', own_key=False):
    provider = manager.save('provider', {'name': 'Lifecycle fixture', 'base_url': 'https://fixture.example/v1', 'protocol': 'anthropic' if host == 'claude-code' else 'openai', 'agent_ids': [host], 'model_context_windows': {'native-fixture': 32768}})
    if own_key:
        provider = manager.credential('provider', provider['id'], 'synthetic-provider-value')
    model = manager.save('model', {'name': 'Fixture model', 'provider_id': provider['id'], 'model_id': 'native-fixture', 'context_window': 32768})
    return provider, model


def account_provider(manager, host='codex', own_key=False):
    provider, model = create(manager, host, own_key)
    account = manager.save('account', {'name': 'Fixture account', 'kind': 'api_key', 'provider_id': provider['id']})
    account = manager.credential('account', account['id'], 'synthetic-account-value')
    provider = manager.save('provider', {'account_id': account['id']}, provider['id'], provider['version'])
    return provider, model, account


def activate(manager, provider, model, host='codex'):
    return manager.plan({'host_id': host, 'provider_id': provider['id'], 'model_id': model['id'], 'activate_provider': True})


def apply(manager, plan):
    return manager.apply(plan['id'], plan['version'], True)


@pytest.mark.parametrize('host', ['codex', 'claude-code', 'opencode', 'pi', 'kimi-code', 'hermes', 'minimax-code', 'openclaw'])
def test_bound_api_account_reaches_native_configuration_only_after_confirmation(manager, host):
    provider, model, account = account_provider(manager, host)
    plan = activate(manager, provider, model, host)
    assert not list(manager.home.iterdir())
    assert 'synthetic-account-value' not in json.dumps(plan)
    apply(manager, plan)
    native = '\n'.join(path.read_text() for path in manager.home.rglob('*') if path.is_file())
    assert 'synthetic-account-value' in native
    assert 'synthetic-account-value'.encode() not in manager.db_path.read_bytes()
    assert plan['payload']['metadata']['entity_versions'][account['id']] == account['version']


def test_bound_oauth_applies_access_token_only_and_stale_refresh_invalidates_preview(oauth_env):
    manager, oauth, provider, account = oauth_env
    flow = oauth.authorize(account['id'], provider['oauth']['redirect_uri'])
    state = parse_qs(urlsplit(flow['authorization_url']).query)['state'][0]
    asyncio.run(oauth.callback(state, 'synthetic-code', transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'access_token': 'synthetic-access-value', 'refresh_token': 'synthetic-refresh-value', 'expires_in': 3600}))))
    provider = manager.save('provider', {'account_id': account['id']}, provider['id'], provider['version'])
    model = manager.save('model', {'name': 'Native model', 'provider_id': provider['id'], 'model_id': 'native-model'})
    plan = activate(manager, provider, model)
    apply(manager, plan)
    text = (manager.home / '.codex/config.toml').read_text()
    native = tomllib.loads(text)
    headers = native['model_providers'][native['model_provider']]['http_headers']
    assert headers['Authorization'] == 'Bearer synthetic-access-value'
    assert 'synthetic-refresh-value' not in text and 'refresh_token' not in text
    assert 'synthetic-access-value'.encode() not in manager.db_path.read_bytes()


def test_account_replacement_after_preview_blocks_apply_before_native_writes(manager):
    provider, model, account = account_provider(manager)
    plan = activate(manager, provider, model)
    manager.credential('account', account['id'], 'replacement-account-value')
    with pytest.raises(ConfigurationError, match='changed since preview'):
        apply(manager, plan)
    assert not list(manager.home.iterdir())


def test_account_rotation_marks_each_using_native_provider_as_update_available(manager):
    provider, model, account = account_provider(manager)
    apply(manager, activate(manager, provider, model))
    manager.credential('account', account['id'], 'replacement-account-value')
    manager.vault.locked = True
    state = manager.host_state('codex')
    assert state['resources'][provider['id']]['status'] == 'update_available'
    assert state['selection']['status'] == 'update_available'
    assert state['credentials_read'] is False


@pytest.mark.parametrize('host', ['codex', 'claude-code', 'opencode', 'pi', 'kimi-code', 'hermes', 'minimax-code', 'openclaw'])
def test_owned_uninstall_survives_removed_key_and_changed_protocol_without_vault_reads(manager, host, monkeypatch):
    provider, model = create(manager, host, own_key=True)
    plan = manager.plan({'host_id': host, 'provider_id': provider['id']})
    apply(manager, plan)
    changed = manager.remove_credential('provider', provider['id'])
    manager.save('provider', {'protocol': 'gemini', 'model_map': {}, 'model_context_windows': {}}, provider['id'], changed['version'])
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('Owned uninstall must not ask for credentials'))
    remove = manager.plan({'host_id': host, 'provider_id': provider['id'], 'operation': 'uninstall'})
    apply(manager, remove)
    with manager.connect() as conn:
        assert conn.execute('SELECT count(*) FROM management_owned WHERE owner=?', (provider['id'],)).fetchone()[0] == 0
    native = '\n'.join(path.read_text() for path in manager.home.rglob('*') if path.is_file())
    assert 'synthetic-provider-value' not in native


def test_current_provider_must_switch_or_restore_before_uninstall(manager):
    provider, model = create(manager, own_key=True)
    apply(manager, activate(manager, provider, model))
    original = (manager.home / '.codex/config.toml').read_bytes()
    with pytest.raises(ConfigurationError, match='current|active|selection'):
        manager.plan({'host_id': 'codex', 'provider_id': provider['id'], 'operation': 'uninstall'})
    assert (manager.home / '.codex/config.toml').read_bytes() == original


def test_kimi_saved_model_capacity_is_written_and_changes_invalidate_old_preview(manager):
    provider = manager.save('provider', {'name': 'Kimi fixture', 'protocol': 'openai', 'base_url': 'https://fixture.example/v1'})
    provider = manager.credential('provider', provider['id'], 'synthetic-kimi-value')
    model = manager.save('model', {'name': 'Saved capacity', 'provider_id': provider['id'], 'model_id': 'native-fixture', 'context_window': 32768})
    stale = activate(manager, provider, model, 'kimi-code')
    model = manager.save('model', {'context_window': 65536}, model['id'], model['version'])
    with pytest.raises(ConfigurationError, match='changed since preview'):
        apply(manager, stale)
    apply(manager, activate(manager, provider, model, 'kimi-code'))
    native = tomllib.loads((manager.home / '.kimi-code/config.toml').read_text())
    assert list(native['models'].values())[0]['max_context_size'] == 65536


@pytest.mark.parametrize('value', [True, 0, -1, '32768', 1.5, 2**63])
def test_invalid_saved_capacity_rejected_before_creating_definition(manager, value):
    provider, _ = create(manager)
    with pytest.raises(ConfigurationError):
        manager.save('model', {'name': 'Bad capacity', 'provider_id': provider['id'], 'model_id': 'bad-capacity', 'context_window': value})


def test_setup_saves_capacity_and_binds_it_into_native_kimi_format_idempotently(manager):
    body = {'request_id': 'kimi-capacity-request-0001', 'host_id': 'kimi-code', 'provider': {'name': 'Kimi setup', 'base_url': 'https://fixture.example/v1', 'protocol': 'openai'}, 'model_id': 'setup-model', 'context_window': 32768, 'secret': 'synthetic-kimi-value'}
    value = manager.setup_provider(body)
    assert value == manager.setup_provider(body)
    assert value['model']['context_window'] == 32768
    apply(manager, activate(manager, value['provider'], value['model'], 'kimi-code'))
    native = tomllib.loads((manager.home / '.kimi-code/config.toml').read_text())
    assert list(native['models'].values())[0]['max_context_size'] == 32768
    with pytest.raises(ConfigurationError, match='request changed'):
        manager.setup_provider({**body, 'context_window': 65536})


def test_explicit_account_source_is_used_consistently_and_does_not_fall_back_to_own_key(manager, monkeypatch):
    provider, model, account = account_provider(manager, own_key=True)
    provider = manager.save('provider', {'credential_source': 'account'}, provider['id'], provider['version'])
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    assert management_service.resolve_provider(provider['id'])['api_key'] == 'synthetic-account-value'
    apply(manager, activate(manager, provider, model))
    text = (manager.home / '.codex/config.toml').read_text()
    assert 'synthetic-account-value' in text and 'synthetic-provider-value' not in text
    manager.remove_credential('account', account['id'])
    with pytest.raises(ConfigurationError):
        management_service.resolve_provider(provider['id'])


def test_import_rebinds_shared_account_to_new_object_without_reusing_existing_local_key(manager, monkeypatch):
    provider, model = create(manager)
    account = manager.save('account', {'name': 'Unbound shared account', 'kind': 'api_key'})
    account = manager.credential('account', account['id'], 'existing-private-value')
    provider = manager.save('provider', {'account_id': account['id'], 'credential_source': 'account'}, provider['id'], provider['version'])
    result = manager.import_objects({'format': 'senrelay', 'data': manager.export()})
    assert result['unsupported'] == []
    copied_provider = next(row for row in result['imported'] if row['entity_kind'] == 'provider')
    copied_account = next(row for row in result['imported'] if row['entity_kind'] == 'account')
    assert copied_provider['account_id'] == copied_account['id'] != account['id']
    assert copied_provider['credential_source'] == 'account'
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    with pytest.raises(ConfigurationError):
        management_service.resolve_provider(copied_provider['id'])


def test_provider_only_share_cannot_attach_an_existing_account_by_opaque_identifier(manager, monkeypatch):
    provider, model = create(manager)
    account = manager.save('account', {'name': 'Unbound shared account', 'kind': 'api_key'})
    account = manager.credential('account', account['id'], 'existing-private-value')
    payload = {'objects': [{'entity_kind': 'provider', 'data': {'name': 'Imported endpoint', 'base_url': 'https://other.example/v1', 'protocol': 'openai', 'account_id': account['id'], 'credential_source': 'account'}}]}
    result = manager.import_objects({'format': 'senrelay', 'data': payload})
    assert result['unsupported'] and len(result['imported']) == 1
    copied = result['imported'][0]
    assert not copied.get('account_id') and copied['credential_reattach_required'] is True
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    with pytest.raises(ConfigurationError):
        management_service.resolve_provider(copied['id'])


def test_shared_account_readback_does_not_confuse_a_new_apply_with_other_providers_old_configuration(manager):
    a, am = create(manager)
    b, bm = create(manager)
    account = manager.save('account', {'name': 'Shared account', 'kind': 'api_key'})
    account = manager.credential('account', account['id'], 'first-account-value')
    a = manager.save('provider', {'account_id': account['id']}, a['id'], a['version'])
    b = manager.save('provider', {'account_id': account['id']}, b['id'], b['version'])
    apply(manager, manager.plan({'host_id': 'opencode', 'provider_id': a['id']}))
    manager.credential('account', account['id'], 'second-account-value')
    apply(manager, activate(manager, b, bm, 'opencode'))
    state = manager.host_state('opencode')
    assert state['resources'][a['id']]['status'] == 'update_available'
    assert state['resources'][b['id']]['status'] == 'verified'


@pytest.mark.parametrize('field,value', [('account_id', {}), ('provider_id', []), ('id', {}), ('model_id', False)])
def test_bad_share_identifier_types_reject_before_creating_any_objects(manager, field, value):
    payload = {'objects': [{'entity_kind': 'provider', 'data': {'name': 'Do not partially save', 'base_url': 'https://fixture.example/v1', 'protocol': 'openai'}}, {'entity_kind': 'provider', 'data': {'name': 'Bad reference', 'base_url': 'https://fixture.example/v1', 'protocol': 'openai', field: value}}]}
    with pytest.raises(ConfigurationError):
        manager.import_objects({'format': 'senrelay', 'data': payload})
    assert manager.list('provider') == []


def test_provider_catalog_uses_same_account_source_and_invalidates_after_key_change(manager, monkeypatch):
    from app.database import create_schema
    from app.services import provider_checks
    monkeypatch.setenv('CONTEXTCORE_DB', str(manager.db_path)); create_schema(manager.db_path)
    provider, model, account = account_provider(manager, own_key=True)
    provider = manager.save('provider', {'credential_source': 'account'}, provider['id'], provider['version'])
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    calls = []
    def endpoint(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer synthetic-account-value'
        return httpx.Response(200, json={'data': [{'id': 'saved-catalog-model'}]})
    monkeypatch.setattr(provider_checks, 'client_factory', lambda: httpx.AsyncClient(transport=httpx.MockTransport(endpoint)))
    result = asyncio.run(provider_checks.refresh_catalog(provider['id']))
    assert result['items'][0]['id'] == 'saved-catalog-model'
    assert provider_checks.catalog(provider['id'])['status'] == 'fresh'
    manager.credential('account', account['id'], 'next-account-value')
    assert provider_checks.catalog(provider['id'])['status'] == 'not_fetched' and len(calls) == 1


def test_oauth_expiry_between_preview_and_apply_stops_before_keychain_or_file_writes(oauth_env, monkeypatch):
    from app.integrations import config_transactions
    manager, oauth, provider, account = oauth_env
    flow = oauth.authorize(account['id'], provider['oauth']['redirect_uri'])
    state = parse_qs(urlsplit(flow['authorization_url']).query)['state'][0]
    asyncio.run(oauth.callback(state, 'synthetic-code', transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'access_token': 'synthetic-access-value', 'expires_in': 3600}))))
    provider = manager.save('provider', {'account_id': account['id']}, provider['id'], provider['version'])
    model = manager.save('model', {'name': 'Native model', 'provider_id': provider['id'], 'model_id': 'native-model'})
    plan = activate(manager, provider, model)
    expiry = manager.get('account', account['id'])['expires_at']
    monkeypatch.setattr(config_transactions, 'time', SimpleNamespace(time=lambda: expiry + 1))
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('Expired preview must not read credentials'))
    with pytest.raises(ConfigurationError, match='expired after preview'):
        apply(manager, plan)
    assert not list(manager.home.iterdir())


def test_removing_a_legacy_provider_key_does_not_switch_billing_to_bound_account(manager, monkeypatch):
    provider, model, account = account_provider(manager, own_key=True)
    # Model an older stored definition that predates credential_source.
    with manager.connect() as conn:
        value = manager.get('provider', provider['id']); value.pop('credential_source', None)
        for key in ('id', 'version', 'created_at', 'updated_at'): value.pop(key, None)
        conn.execute('UPDATE management_entities SET data=? WHERE id=?', (json.dumps(value), provider['id']))
    manager.remove_credential('provider', provider['id'])
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    with pytest.raises(ConfigurationError):
        management_service.resolve_provider(provider['id'])
    assert manager.get('provider', provider['id'])['credential_source'] == 'provider'
    assert manager.get('account', account['id'])['credential_ref'] is not None


def test_saving_a_provider_key_does_not_override_an_account_previously_in_use(manager, monkeypatch):
    provider, model, account = account_provider(manager)
    manager.credential('provider', provider['id'], 'new-unused-provider-value')
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    assert management_service.resolve_provider(provider['id'])['api_key'] == 'synthetic-account-value'


def test_known_saved_model_capacity_fills_an_unknown_legacy_provider_capacity_map(manager):
    provider, model = create(manager, own_key=True)
    provider = manager.save('provider', {'model_context_windows': None}, provider['id'], provider['version'])
    apply(manager, activate(manager, provider, model, 'kimi-code'))
    native = tomllib.loads((manager.home / '.kimi-code/config.toml').read_text())
    assert list(native['models'].values())[0]['max_context_size'] == 32768


def test_invalid_share_kind_type_cannot_interrupt_after_partially_creating_objects(manager):
    objects = [{'entity_kind': 'provider', 'data': {'name': 'Do not partially save', 'base_url': 'https://fixture.example/v1', 'protocol': 'openai'}}, {'entity_kind': {}, 'data': {}}]
    with pytest.raises(ConfigurationError):
        manager.import_objects({'format': 'senrelay', 'data': {'objects': objects}})
    assert manager.list('provider') == []
