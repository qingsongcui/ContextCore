import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import security
from app.services import connection_service


@pytest.fixture
def client(monkeypatch):
    security._SESSIONS.clear()
    monkeypatch.setattr(connection_service, 'verify_agent_token', lambda token, *, purpose: 'omp' if token == 'granted-token' else None)
    app = FastAPI()
    app.add_middleware(security.SecurityMiddleware)

    @app.post('/api/content')
    def mutation():
        return {'saved': True, 'actor': security.authenticated_agent.get()}

    @app.post('/mcp/')
    @app.post('/api/connections/{agent_id}/hook')
    def agent_operation(request: Request):
        return {'actor': request.state.agent_id}

    @app.get('/api/status')
    def status():
        return {'ok': True}
    with TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000)) as test_client:
        yield test_client
    security._SESSIONS.clear()


def test_csrf_requires_matching_live_cookie(client):
    sid, token = security.create_session()
    other_sid, other_token = security.create_session()
    assert client.post('/api/content', headers={'X-CSRF-Token': token}).status_code == 403
    client.cookies.set(security.SESSION_COOKIE_NAME, sid)
    assert client.post('/api/content', headers={'X-CSRF-Token': other_token}).status_code == 403
    assert client.post('/api/content', headers={'X-CSRF-Token': token}).status_code == 200
    security._SESSIONS[sid]['expires_at'] = time.time() - 1
    assert client.post('/api/content', headers={'X-CSRF-Token': token}).status_code == 403
    assert security.get_csrf_token_for_session(other_sid) == other_token


def test_mcp_is_closed_without_enabled_credentials(client):
    assert client.post('/mcp/').status_code == 401
    assert client.post('/mcp/', headers={'Authorization': 'Bearer revoked-token'}).status_code == 401
    assert client.post('/mcp/', headers={'Authorization': 'Bearer granted-token'}).json() == {'actor': 'omp'}


def test_credential_store_failure_does_not_grant_access(client, monkeypatch):
    def unavailable(token, *, purpose):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(connection_service, 'verify_agent_token', unavailable)
    assert client.post('/mcp/', headers={'Authorization': 'Bearer granted-token'}).status_code == 503


def test_hook_token_cannot_impersonate_another_host(client):
    headers = {'Authorization': 'Bearer granted-token'}
    assert client.post('/api/connections/codex/hook', headers=headers).status_code == 401
    assert client.post('/api/connections/omp/hook', headers=headers).json() == {'actor': 'omp'}
    assert client.post('/api/content', headers=headers).status_code == 403


@pytest.mark.parametrize('headers', [
    {'Host': 'localhost.attacker.example'},
    {'Host': 'localhost@attacker.example'},
    {'Origin': 'https://attacker.example'},
    {'Origin': 'null'},
    {'Sec-Fetch-Site': 'cross-site'},
])
def test_browser_origin_and_rebinding_are_rejected(client, headers):
    assert client.get('/api/status', headers=headers).status_code == 403


def test_ipv6_loopback_host_is_accepted(client):
    assert client.get('/api/status', headers={'Host': '[::1]:8420', 'Origin': 'http://[::1]:8420'}).status_code == 200


def test_confirmation_requests_require_explicit_boolean():
    from app.models import ConnectionPatch, HandoffConfirmRequest, ServiceStopRequest
    from pydantic import ValidationError

    assert ConnectionPatch(enabled=True).confirmed is False
    for model in (HandoffConfirmRequest, ServiceStopRequest):
        with pytest.raises(ValidationError):
            model()
        with pytest.raises(ValidationError):
            model(confirmed='true')
        assert model(confirmed=True).confirmed is True


def test_internal_validation_typeerror_never_retries_without_purpose(client, monkeypatch):
    calls = []
    def broken(token, *, purpose=None):
        calls.append(purpose)
        if purpose:
            raise TypeError('synthetic internal validation failure')
        return 'omp'
    monkeypatch.setattr(connection_service, 'verify_agent_token', broken)
    assert client.post('/api/connections/omp/hook', headers={'Authorization': 'Bearer granted-token'}).status_code == 503
    assert calls == ['lifecycle']


@pytest.mark.parametrize('peer', ['192.0.2.10', '2001:db8::1'])
def test_proxy_headers_cannot_turn_remote_peer_into_loopback(peer):
    app = FastAPI()
    app.add_middleware(security.SecurityMiddleware)
    @app.get('/api/status')
    def endpoint():
        pytest.fail('Remote request must not reach endpoint')
    with TestClient(app, base_url='http://127.0.0.1:8420', client=(peer, 50000)) as remote:
        response = remote.get('/api/status', headers={'X-Forwarded-For': '127.0.0.1', 'Forwarded': 'for=127.0.0.1'})
        assert response.status_code == 403


def test_agent_identity_does_not_leak_into_next_browser_request(client):
    assert client.post('/mcp/', headers={'Authorization': 'Bearer granted-token'}).json()['actor'] == 'omp'
    sid, token = security.create_session()
    client.cookies.set(security.SESSION_COOKIE_NAME, sid)
    response = client.post('/api/content', headers={'X-CSRF-Token': token})
    assert response.status_code == 200 and response.json()['actor'] is None
    assert security.authenticated_agent.get() is None


def test_oauth_cross_site_exception_is_exact_get_callback_only(client):
    headers = {'Origin': 'https://provider.example', 'Sec-Fetch-Site': 'cross-site'}
    # No callback route in this fixture: 404 proves only the middleware exception.
    assert client.get('/api/manage/oauth/callback', headers=headers).status_code == 404
    assert client.post('/api/manage/oauth/callback', headers=headers).status_code == 403
    assert client.get('/api/manage/oauth/callback/extra', headers=headers).status_code == 403
    assert client.get('/api/manage/providers', headers=headers).status_code == 403
