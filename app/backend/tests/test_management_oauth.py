from __future__ import annotations

import asyncio
import hashlib
import base64
import json
import sqlite3
import time
from urllib.parse import urlsplit, parse_qs

import httpx
import pytest

from app.services.management_service import ManagementService
from app.services.oauth_service import OAuthService
from app.integrations.config_transactions import ConfigurationError
from tests.test_management import FakeVault


@pytest.fixture
def oauth_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    manager = ManagementService(tmp_path / "test.db", home, FakeVault())
    config = {"enabled": True, "authorization_url": "https://official.example.test/authorize", "token_url": "https://official.example.test/token", "client_id": "user-registered-client", "scopes": ["profile", "offline_access"], "redirect_uri": "http://127.0.0.1:9999/api/manage/oauth/callback", "token_endpoint_auth_method": "none"}
    provider = manager.save("provider", {"name": "Official", "base_url": "https://api.example.test/v1", "protocol": "openai", "oauth": config})
    account = manager.save("account", {"name": "User account", "kind": "oauth", "provider_id": provider["id"]})
    return manager, OAuthService(manager), provider, account


def authorize(oauth_env):
    manager, oauth, provider, account = oauth_env
    result = oauth.authorize(account["id"], "http://127.0.0.1:9999/api/manage/oauth/callback")
    params = parse_qs(urlsplit(result["authorization_url"]).query)
    return result, params


def test_account_expiration_is_current_on_detail_and_bounded_lists_without_mutating_evidence(oauth_env, monkeypatch):
    manager, oauth, provider, account = oauth_env
    _, params = authorize(oauth_env)
    asyncio.run(oauth.callback(params['state'][0], 'code', transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'access_token': 'expiring-synthetic', 'expires_in': 1}))))
    stored = manager.get('account', account['id'])
    from types import SimpleNamespace
    from app.services import provider_credentials
    monkeypatch.setattr(provider_credentials, 'time', SimpleNamespace(time=lambda: stored['expires_at'] + 1))
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('Reading expiry must not open the vault'))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.manage import router
    from app.services import management_service
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    app = FastAPI(); app.include_router(router)
    client = TestClient(app)
    for path in ('/api/manage/accounts/' + account['id'], '/api/manage/accounts', '/api/manage/accounts?summary=true'):
        response = client.get(path)
        assert response.status_code == 200
        value = response.json()
        value = value[0] if isinstance(value, list) else value['items'][0] if 'items' in value else value
        assert value['status'] == 'expired'
        assert value['expires_at'] == stored['expires_at']
    assert manager.get('account', account['id']) == stored
    assert oauth.cancel(account['id'])['status'] == 'expired'


@pytest.mark.parametrize('legacy', [False, True])
def test_abandoned_authorization_deadline_is_visible_without_a_callback_or_automatic_cleanup(oauth_env, monkeypatch, legacy):
    manager, oauth, provider, account = oauth_env
    flow, _ = authorize(oauth_env)
    stored = manager.get('account', account['id'])
    if legacy:
        with manager.connect() as conn:
            conn.execute("UPDATE management_entities SET data=json_remove(data,'$.authorization_expires_at') WHERE id=?", (account['id'],))
        stored = manager.get('account', account['id'])
    from types import SimpleNamespace
    from app.services import provider_credentials
    monkeypatch.setattr(provider_credentials, 'time', SimpleNamespace(time=lambda: flow['expires_at'] + 1))
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('Reading a flow deadline must not open the vault'))
    assert manager.list('account')[0]['status'] == 'expired'
    value = manager.catalog_page('account')['items'][0]
    assert value['status'] == 'expired' and value['authorization_expires_at'] == flow['expires_at']
    assert manager.get('account', account['id']) == stored
    assert len(manager.vault.values) == 1


@pytest.mark.parametrize('expiry', [False, 'later', {'body': 'must_not_enter_summary' * 5000}])
def test_invalid_account_expiry_is_bounded_and_never_claims_authorized(oauth_env, expiry):
    manager, oauth, provider, account = oauth_env
    _, params = authorize(oauth_env)
    asyncio.run(oauth.callback(params['state'][0], 'code', transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'access_token': 'synthetic'}))))
    with manager.connect() as conn:
        conn.execute("UPDATE management_entities SET data=json_set(data,'$.expires_at',json(?)) WHERE id=?", (json.dumps(expiry), account['id']))
    assert manager.list('account')[0]['status'] == 'error'
    page = manager.catalog_page('account')
    assert page['items'][0]['status'] == 'error'
    assert 'expires_at' not in page['items'][0] and len(json.dumps(page)) < 2000


def test_pkce_one_use_no_plaintext_token_or_code_in_database(oauth_env):
    manager, oauth, provider, account = oauth_env
    result, params = authorize(oauth_env)
    state = params["state"][0]
    assert result["flow"] == "oauth2_pkce_s256"
    assert params["code_challenge_method"] == ["S256"]
    assert manager.get("account", account["id"])["status"] == "authorizing"
    requests = []
    def endpoint(request):
        assert request.url == httpx.URL(provider["oauth"]["token_url"])
        data = parse_qs(request.content.decode())
        requests.append(data)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        assert challenge == params["code_challenge"][0]
        assert data["code"] == ["opaque-auth-code"]
        return httpx.Response(200, json={"access_token": "opaque-access-token", "refresh_token": "opaque-refresh-token", "expires_in": 3600, "token_type": "Bearer"})
    response = asyncio.run(oauth.callback(state, "opaque-auth-code", transport=httpx.MockTransport(endpoint)))
    assert response["status"] == "authorized" and response["quota"]["status"] == "unknown"
    assert len(manager.vault.values) == 1  # verifier removed; token bundle remains
    stored = manager.get("account", account["id"])
    assert json.loads(manager.vault.get(stored["credential_ref"]))["access_token"] == "opaque-access-token"
    database = manager.db_path.read_bytes().decode("utf-8", errors="ignore")
    for secret in (state, "opaque-auth-code", "opaque-access-token", "opaque-refresh-token", requests[0]["code_verifier"][0]):
        assert secret not in database
        assert secret not in json.dumps(manager.list("account"))
        assert secret not in json.dumps(manager.export())
    with pytest.raises(ConfigurationError, match="consumed"):
        asyncio.run(oauth.callback(state, "same-code", transport=httpx.MockTransport(endpoint)))
    assert len(requests) == 1


def test_registered_redirect_matches_actual_port(oauth_env):
    manager, oauth, provider, account = oauth_env
    with pytest.raises(ConfigurationError, match="actual local"):
        oauth.authorize(account["id"], "http://127.0.0.1:8420/api/manage/oauth/callback")
    assert not manager.vault.values
    with pytest.raises(ConfigurationError):
        OAuthService.validate_config({**provider["oauth"], "redirect_uri": "https://evil.test/callback"})


def test_changed_official_endpoint_never_receives_old_code_or_verifier(oauth_env):
    manager, oauth, provider, account = oauth_env
    _, params = authorize(oauth_env)
    altered = {**provider["oauth"], "token_url": "https://other.example.test/token"}
    manager.save("provider", {"oauth": altered}, provider["id"], provider["version"])
    calls = []
    def endpoint(request):
        calls.append(request)
        return httpx.Response(200, json={"access_token": "not-adopted"})
    with pytest.raises(ConfigurationError):
        asyncio.run(oauth.callback(params["state"][0], "old-code", transport=httpx.MockTransport(endpoint)))
    assert not calls and not manager.vault.values


def test_provider_edit_during_exchange_prevents_token_adoption(oauth_env):
    manager, oauth, provider, account = oauth_env
    _, params = authorize(oauth_env)
    def endpoint(request):
        manager.save("provider", {"oauth": {**provider["oauth"], "client_id": "different-client"}}, provider["id"], provider["version"])
        return httpx.Response(200, json={"access_token": "discarded-token"})
    with pytest.raises(ConfigurationError):
        asyncio.run(oauth.callback(params["state"][0], "code", transport=httpx.MockTransport(endpoint)))
    assert not manager.vault.values
    assert manager.get("account", account["id"])["status"] == "error"


def test_expiry_cancel_and_invalid_state_never_hit_token_endpoint(oauth_env):
    manager, oauth, provider, account = oauth_env
    _, params = authorize(oauth_env)
    with manager.connect() as conn:
        conn.execute("UPDATE management_oauth_flows SET expires_at=?", (time.time() - 1,))
    called = []
    def endpoint(request):
        called.append(request)
        return httpx.Response(200, json={"access_token": "never"})
    with pytest.raises(ConfigurationError):
        asyncio.run(oauth.callback(params["state"][0], "code", transport=httpx.MockTransport(endpoint)))
    assert not called and not manager.vault.values
    _, params = authorize(oauth_env)
    oauth.cancel(account["id"])
    with pytest.raises(ConfigurationError):
        asyncio.run(oauth.callback(params["state"][0], "code", transport=httpx.MockTransport(endpoint)))
    assert not called and not manager.vault.values
    with pytest.raises(ConfigurationError):
        asyncio.run(oauth.callback("unrelated-state-is-not-accepted", "code", transport=httpx.MockTransport(endpoint)))


def test_refresh_rotates_keychain_refs_and_logout_is_local(oauth_env):
    manager, oauth, provider, account = oauth_env
    _, params = authorize(oauth_env)
    first = httpx.MockTransport(lambda request: httpx.Response(200, json={"access_token": "first-access", "refresh_token": "first-refresh", "expires_in": 3600}))
    asyncio.run(oauth.callback(params["state"][0], "code", transport=first))
    old_ref = manager.get("account", account["id"])["credential_ref"]
    def endpoint(request):
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["first-refresh"]
        return httpx.Response(200, json={"access_token": "second-access", "refresh_token": "second-refresh", "expires_in": 3600})
    refreshed = asyncio.run(oauth.refresh(account["id"], httpx.MockTransport(endpoint)))
    assert refreshed["status"] == "authorized"
    new_ref = manager.get("account", account["id"])["credential_ref"]
    assert new_ref != old_ref and old_ref not in manager.vault.values
    assert json.loads(manager.vault.get(new_ref))["refresh_token"] == "second-refresh"
    assert "second-refresh" not in manager.db_path.read_bytes().decode("utf-8", errors="ignore")
    assert oauth.logout(account["id"])["remote_session_revocation"] == "not_attempted"
    assert not manager.vault.values
    assert manager.get("account", account["id"])["status"] == "logged_out"


def test_refresh_singleflight_and_unknown_does_not_replay(oauth_env):
    manager, oauth, provider, account = oauth_env
    _, params = authorize(oauth_env)
    first = httpx.MockTransport(lambda request: httpx.Response(200, json={"access_token": "first-access", "refresh_token": "first-refresh"}))
    asyncio.run(oauth.callback(params["state"][0], "code", transport=first))
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def endpoint(request):
            entered.set()
            await release.wait()
            raise httpx.ReadTimeout("unknown after sending refresh")
        transport = httpx.MockTransport(endpoint)
        active = asyncio.create_task(oauth.refresh(account["id"], transport))
        await entered.wait()
        with pytest.raises(ConfigurationError, match="active or its outcome is unknown"):
            await oauth.refresh(account["id"], transport)
        release.set()
        with pytest.raises(ConfigurationError, match="unknown"):
            await active
        with pytest.raises(ConfigurationError, match="unknown"):
            await oauth.refresh(account["id"], transport)
    asyncio.run(run())


def test_account_cannot_claim_authorized_through_crud(oauth_env):
    manager, oauth, provider, account = oauth_env
    with pytest.raises(ConfigurationError, match="official flow"):
        manager.save("account", {"status": "authorized"}, account["id"], account["version"])


def test_oauth_without_official_enabled_client_stays_unsupported(oauth_env):
    manager, oauth, provider, account = oauth_env
    manager.save("provider", {"oauth": {**provider["oauth"], "enabled": False}}, provider["id"], provider["version"])
    result = oauth.authorize(account["id"], "http://127.0.0.1:9999/api/manage/oauth/callback")
    assert result["status"] == "unsupported"
    assert result["authorization_url"] is None
    assert not manager.vault.values
    assert manager.get("account", account["id"])["status"] == "unsupported"


def test_official_revocation_is_opt_in_and_distinct_from_local_logout(oauth_env):
    manager, oauth, provider, account = oauth_env
    _, params = authorize(oauth_env)
    first = httpx.MockTransport(lambda request: httpx.Response(200, json={"access_token": "access-to-revoke", "refresh_token": "refresh-to-revoke"}))
    asyncio.run(oauth.callback(params["state"][0], "code", transport=first))
    assert asyncio.run(oauth.revoke(account["id"], True, first))["remote_session_revocation"] == "not_attempted"
    provider = manager.save("provider", {"oauth": {**provider["oauth"], "revoke_endpoint": "https://official.example.test/revoke"}}, provider["id"], provider["version"])
    calls = []
    def endpoint(request):
        calls.append(request)
        assert request.url == httpx.URL(provider["oauth"]["revoke_endpoint"])
        assert parse_qs(request.content.decode())["token"] == ["refresh-to-revoke"]
        return httpx.Response(200)
    with pytest.raises(ConfigurationError, match="Confirm"):
        asyncio.run(oauth.revoke(account["id"], False, httpx.MockTransport(endpoint)))
    assert not calls
    result = asyncio.run(oauth.revoke(account["id"], True, httpx.MockTransport(endpoint)))
    assert result["status"] == "logged_out" and result["remote_session_revocation"] == "accepted"
    assert not manager.vault.values
    assert "refresh-to-revoke" not in manager.db_path.read_bytes().decode("utf-8", errors="ignore")
