"""Declared API surfaces reach native files, dispatch and explicit paid checks."""
import json
import tomllib

import httpx
import pytest
from fastapi import HTTPException

from app.integrations.config_transactions import ConfigurationError
from app.services.provider_endpoints import validate_endpoints, declared_endpoints
from app.services.routing_service import _target
from app.services import usage_service
from tests.test_management_experience import manager, setup
from tests.test_provider_checks import env, transport


@pytest.mark.parametrize('patch', [
    {'wire_api': []}, {'wire_api': 'unknown'}, {'supported_endpoints': []},
    {'supported_endpoints': 'responses'}, {'supported_endpoints': ['messages']},
    {'supported_endpoints': [['responses']]},
    {'wire_api': 'responses', 'supported_endpoints': ['chat/completions']},
    {'protocol': 'anthropic', 'wire_api': 'chat'},
])
def test_invalid_declarations_fail_closed_on_save_and_dispatch(patch):
    value = {'protocol': 'openai', **patch}
    with pytest.raises(ConfigurationError):
        validate_endpoints(value)
    with pytest.raises(ConfigurationError):
        declared_endpoints(value)


def test_unknown_legacy_stays_unknown_and_one_endpoint_infers_preference():
    original = {'protocol': 'openai'}
    assert declared_endpoints(original) is None and original == {'protocol': 'openai'}
    value = validate_endpoints({'protocol': 'openai', 'supported_endpoints': ['responses', 'responses']})
    assert value['wire_api'] == 'responses' and value['supported_endpoints'] == ['responses']


def test_setup_idempotency_and_bounded_metadata_keep_api_declaration(manager):
    request = setup()
    request['provider'].update(wire_api='responses', supported_endpoints=['chat/completions', 'responses'])
    result = manager.setup_provider(request)
    assert result == manager.setup_provider(request)
    page = manager.catalog_page('provider', agent_id='codex')
    assert page['items'][0]['wire_api'] == 'responses'
    assert page['items'][0]['supported_endpoints'] == ['chat/completions', 'responses']
    assert 'model_map' not in page['items'][0] and 'credential_ref' not in json.dumps(page)
    request['provider']['wire_api'] = 'chat'
    with pytest.raises(ConfigurationError, match='request changed'):
        manager.setup_provider(request)


def test_chat_only_codex_fails_before_vault_or_native_write(manager, monkeypatch):
    request = setup()
    request['provider'].update(wire_api='chat')
    value = manager.setup_provider(request)
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('preview must not read credentials'))
    with pytest.raises(ConfigurationError, match='required by this host: responses'):
        manager.plan({'host_id': 'codex', 'provider_id': value['provider']['id'], 'activate_provider': True, 'model_id': value['model']['id']})
    assert not list(manager.home.iterdir())


@pytest.mark.parametrize('wire', ['chat', 'responses'])
def test_dual_api_codex_uses_responses_even_when_preference_is_chat(manager, wire):
    request = setup()
    request['provider'].update(wire_api=wire, supported_endpoints=['chat/completions', 'responses'])
    value = manager.setup_provider(request)
    plan = manager.plan({'host_id': 'codex', 'provider_id': value['provider']['id'], 'activate_provider': True, 'model_id': value['model']['id']})
    manager.apply(plan['id'], plan['version'], True)
    native = tomllib.loads((manager.home / '.codex/config.toml').read_text())
    current = native['model_providers'][native['model_provider']]
    assert current['wire_api'] == 'responses'
    assert current['http_headers']['Authorization'] == 'Bearer ' + request['secret']
    assert manager.host_state('codex')['selection']['status'] == 'verified'


@pytest.mark.parametrize('wire,package', [('chat', '@ai-sdk/openai-compatible'), ('responses', '@ai-sdk/openai')])
def test_opencode_package_matches_preferred_api(manager, wire, package):
    request = setup('opencode')
    request['provider']['wire_api'] = wire
    value = manager.setup_provider(request)
    plan = manager.plan({'host_id': 'opencode', 'provider_id': value['provider']['id'], 'activate_provider': True, 'model_id': value['model']['id']})
    manager.apply(plan['id'], plan['version'], True)
    native = json.loads((manager.home / '.config/opencode/opencode.json').read_text())
    assert native['provider'][value['provider']['id']]['npm'] == package


def test_dispatch_rejects_wrong_path_and_preserves_responses_tools():
    provider = {'protocol': 'openai', 'wire_api': 'responses', 'base_url': 'https://fixture.example/v1'}
    with pytest.raises(HTTPException, match='support for this endpoint'):
        _target(provider, 'chat/completions', {'model': 'm', 'messages': []})
    payload = {'model': 'm', 'input': [{'type': 'function_call_output', 'call_id': 'call1', 'output': 'ok'}], 'tools': [{'type': 'function', 'name': 'read', 'parameters': {'type': 'object'}}], 'reasoning': {'effort': 'high'}, 'stream': True}
    url, _, outgoing, _ = _target(provider, 'responses', payload)
    assert url.endswith('/responses') and outgoing == payload


def test_responses_paid_check_uses_declared_path_and_refreshes_metadata_cache(env):
    client, _, value, calls, secrets = env
    transport(env, lambda _: httpx.Response(200, json={'data': [{'id': 'm'}]}))
    assert client.post('/api/manage/providers/p/models/catalog', json={}).status_code == 200
    value.update(wire_api='responses', supported_endpoints=['responses'])
    assert client.get('/api/manage/providers/p/models/catalog').json()['status'] == 'not_fetched'
    calls.clear(); secrets.clear()
    def respond(request):
        assert request.url.path == '/v1/responses'
        assert json.loads(request.content) == {'model': 'selected', 'input': 'Reply OK.', 'stream': False, 'max_output_tokens': 16}
        return httpx.Response(200, json={'id': 'fixture-response', 'object': 'response', 'status': 'completed', 'model': 'selected', 'output': [], 'usage': {'input_tokens': 5, 'output_tokens': 1, 'input_tokens_details': {'cached_tokens': 2}}})
    transport(env, respond)
    assert client.post('/api/manage/providers/p/check', json={'mode': 'inference', 'model': 'selected'}).status_code == 422
    assert calls == secrets == []
    result = client.post('/api/manage/providers/p/check', json={'mode': 'inference', 'model': 'selected', 'confirmed_paid': True}).json()
    assert result['status'] == 'succeeded' and result['endpoint'] == 'responses'
    assert result['usage']['input_tokens'] == 5 and result['usage']['cache_read_tokens'] == 2
    assert len(calls) == len(secrets) == 1
    assert usage_service.requests()['total'] == 1


def test_invalid_endpoint_check_does_not_read_vault_or_send_request(env):
    client, _, value, calls, secrets = env
    value.update(wire_api='responses', supported_endpoints=['chat/completions'])
    response = client.post('/api/manage/providers/p/check', json={'mode': 'inference', 'model': 'm', 'confirmed_paid': True})
    assert response.status_code == 422 and calls == secrets == []
    assert usage_service.requests()['total'] == 0
