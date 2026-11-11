import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app.database import create_schema, db, get_app_setting, set_app_setting
from app.routers.provider_checks import router
from app.services import provider_checks as svc, usage_service


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "checks.db"))
    create_schema(tmp_path / "checks.db")
    value = {"id": "p", "version": 1, "base_url": "https://api.openai.com/v1", "protocol": "openai", "enabled": True}
    calls, secrets = [], []
    monkeypatch.setattr(svc, "provider_metadata", lambda pid: value.copy())
    def resolve(pid):
        secrets.append(pid)
        return {**value, "api_key": "never-return-or-persist-secret"}
    monkeypatch.setattr(svc, "resolve_provider", resolve)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    return client, monkeypatch, value, calls, secrets


def transport(env, handler):
    client, monkeypatch, value, calls, secrets = env
    def handle(request):
        calls.append(request)
        return handler(request)
    monkeypatch.setattr(svc, "client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False, trust_env=False))


def test_catalog_get_is_cache_only_and_explicit_refresh_sanitizes_metadata(env):
    client, monkeypatch, value, calls, secrets = env
    transport(env, lambda req: httpx.Response(200, json={"object": "list", "data": [{"id": "model-a", "name": "A", "context_window": 1234, "extra": "never-return-or-persist-secret"}]}))
    before = client.get("/api/manage/providers/p/models/catalog").json()
    assert before["status"] == "not_fetched" and calls == secrets == []
    result = client.post("/api/manage/providers/p/models/catalog", json={}).json()
    assert result["items"] == [{"id": "model-a", "name": "A", "context_window": 1234, "context_source_field": "context_window"}]
    assert result["authentication"] == "not_proven_by_catalog"
    assert calls[0].method == "GET" and str(calls[0].url) == "https://api.openai.com/v1/models"
    assert calls[0].headers["authorization"] == "Bearer never-return-or-persist-secret"
    assert client.get("/api/manage/providers/p/models/catalog").json()["status"] == "fresh"
    assert len(calls) == 1 and len(secrets) == 1
    cache = get_app_setting("provider_catalog_v2:p")
    assert "never-return-or-persist-secret" not in json.dumps(cache)
    cache["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    set_app_setting("provider_catalog_v2:p", cache)
    assert client.get("/api/manage/providers/p/models/catalog").json()["status"] == "stale"
    assert len(calls) == 1
    value["version"] = 2
    assert client.get("/api/manage/providers/p/models/catalog").json()["status"] == "not_fetched"


def test_network_sends_no_credentials_and_never_claims_auth(env):
    client, monkeypatch, value, calls, secrets = env
    transport(env, lambda req: httpx.Response(401))
    result = client.post("/api/manage/providers/p/check", json={"mode": "network"}).json()
    assert result["status"] == "reachable" and result["http_status"] == 401
    assert result["authentication"] == "unknown" and not result["inference_performed"]
    assert calls[0].method == "HEAD" and "authorization" not in calls[0].headers
    assert secrets == []


@pytest.mark.parametrize("base,protocol", [("https://api.openai.com/v1", "openai"), ("https://api.anthropic.com/v1", "anthropic")])
def test_auth_only_official_valid_models_can_prove_credential(env, base, protocol):
    client, monkeypatch, value, calls, secrets = env
    value.update(base_url=base, protocol=protocol)
    transport(env, lambda req: httpx.Response(200, json={"data": [{"id": "m"}]}))
    result = client.post("/api/manage/providers/p/check", json={"mode": "auth"}).json()
    assert result["status"] == result["authentication"] == "authenticated"
    assert calls[0].method == "GET" and not result["inference_performed"]
    assert usage_service.requests()["total"] == 0
    if protocol == "anthropic":
        assert calls[0].headers["x-api-key"] == "never-return-or-persist-secret"
        assert "authorization" not in calls[0].headers
        assert calls[0].headers["anthropic-version"] == "2023-06-01"


def test_public_compatible_models_and_403_do_not_prove_key_validity(env):
    client, monkeypatch, value, calls, secrets = env
    value["base_url"] = "https://compatible.example/v1"
    transport(env, lambda req: httpx.Response(200, json={"data": [{"id": "m"}]}))
    result = client.post("/api/manage/providers/p/check", json={"mode": "auth"}).json()
    assert result["status"] == "unknown" and result["authentication"] == "unknown"
    transport(env, lambda req: httpx.Response(403))
    assert client.post("/api/manage/providers/p/check", json={"mode": "auth"}).json()["status"] == "unknown"
    transport(env, lambda req: httpx.Response(401))
    assert client.post("/api/manage/providers/p/check", json={"mode": "auth"}).json()["authentication"] == "rejected"


def test_unknown_catalog_shape_not_guessed_and_redirect_never_followed(env):
    client, monkeypatch, value, calls, secrets = env
    transport(env, lambda req: httpx.Response(200, json={"models": [{"name": "do-not-guess"}]}))
    result = client.post("/api/manage/providers/p/models/catalog", json={}).json()
    assert result["status"] == "unknown" and result["items"] == []
    assert get_app_setting("provider_catalog_v2:p") is None
    transport(env, lambda req: httpx.Response(302, headers={"location": "https://evil.example"}, text="secret"))
    result = client.post("/api/manage/providers/p/check", json={"mode": "auth"}).json()
    assert result["status"] == "unknown" and len(calls) == 2
    assert "secret" not in json.dumps(result)


def test_anthropic_catalog_metadata_and_explicit_pagination(env):
    client, monkeypatch, value, calls, secrets = env
    value.update(base_url="https://api.anthropic.com/v1", protocol="anthropic")
    transport(env, lambda req: httpx.Response(200, json={"data": [{"id": "claude", "display_name": "Claude", "max_input_tokens": 200000}], "has_more": True, "last_id": "untrusted-other"}))
    result = client.post("/api/manage/providers/p/models/catalog", json={"after_id": "previous"}).json()
    assert calls[0].url.params["after_id"] == "previous"
    assert result["coverage"] == "page" and result["next_after_id"] == "claude"
    assert result["items"][0]["context_window"] == 200000
    value["protocol"] = "openai"
    assert client.post("/api/manage/providers/p/models/catalog", json={"after_id": "x"}).status_code == 422


def test_inference_needs_confirmation_and_explicit_model_before_vault(env):
    client, monkeypatch, value, calls, secrets = env
    transport(env, lambda req: (_ for _ in ()).throw(AssertionError("must not send")))
    assert client.post("/api/manage/providers/p/check", json={"mode": "inference", "model": "m"}).status_code == 422
    assert client.post("/api/manage/providers/p/check", json={"mode": "inference", "confirmed_paid": True}).status_code == 422
    assert calls == secrets == []
    assert usage_service.requests()["total"] == 0


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
def test_inference_low_output_one_attempt_and_usage_cache_golden(env, protocol):
    client, monkeypatch, value, calls, secrets = env
    value["protocol"] = protocol
    def handler(req):
        body = json.loads(req.content)
        assert body["model"] == "selected-model" and body["stream"] is False
        assert body["messages"] == [{"role": "user", "content": "Reply OK."}]
        if protocol == "anthropic":
            assert body["max_tokens"] == 1 and req.url.path.endswith("/messages")
            return httpx.Response(200, json={"id": "response", "type": "message", "model": "selected-model", "content": [{"type": "text", "text": "O"}], "usage": {"input_tokens": 3, "cache_read_input_tokens": 2, "output_tokens": 1}})
        assert body["max_completion_tokens"] == 1 and req.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={"id": "response", "model": "selected-model", "choices": [{"message": {"content": "O"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 2}}})
    transport(env, handler)
    result = client.post("/api/manage/providers/p/check", json={"mode": "inference", "model": "selected-model", "confirmed_paid": True}).json()
    assert result["status"] == "succeeded" and result["inference_performed"]
    assert result["usage"]["input_tokens"] == 5 and result["usage"]["cache_read_tokens"] == 2
    assert result["cost"]["amount"] is None and len(calls) == 1
    row = usage_service.requests()["items"][0]
    assert row["origins"] == ["provider_check"] and row["observations"] == 2
    with db() as conn:
        dump = str([tuple(row) for row in conn.execute("SELECT * FROM usage_v2_observations")])
    assert "Reply OK" not in dump and "never-return-or-persist-secret" not in dump


def test_unknown_inference_outcome_does_not_retry_and_error_body_hidden(env):
    client, monkeypatch, value, calls, secrets = env
    def error(req):
        raise httpx.ReadTimeout("private-upstream-error")
    transport(env, error)
    result = client.post("/api/manage/providers/p/check", json={"mode": "inference", "model": "chosen", "confirmed_paid": True}).json()
    assert result["status"] == "unknown" and len(calls) == 1
    assert result["inference_performed"] is None
    assert usage_service.requests()["items"][0]["status"] == "unknown"
    assert "private-upstream-error" not in json.dumps(result)


def test_expired_account_resolver_prevents_all_paid_requests(env):
    client, monkeypatch, value, calls, secrets = env
    def expired(pid):
        raise HTTPException(409, "Official account credentials are expired or unavailable")
    monkeypatch.setattr(svc, "resolve_provider", expired)
    transport(env, lambda req: (_ for _ in ()).throw(AssertionError("no network")))
    response = client.post("/api/manage/providers/p/check", json={"mode": "inference", "model": "m", "confirmed_paid": True})
    assert response.status_code == 409 and calls == []
    assert usage_service.requests()["total"] == 0


def test_unknown_protocol_is_explicitly_unsupported(env):
    client, monkeypatch, value, calls, secrets = env
    value["protocol"] = "gemini"
    assert client.get("/api/manage/providers/p/models/catalog").status_code == 422
    assert client.post("/api/manage/providers/p/check", json={"mode": "network"}).status_code == 422
    assert calls == []


@pytest.mark.parametrize("field", ["id", "name", "display_name"])
def test_gateway_known_key_echo_in_catalog_is_rejected_without_prefix(env, field):
    client, monkeypatch, value, calls, secrets = env
    key = "opaqueCredentialExactlyWithoutPrefix7654321"
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {**value, "api_key": key})
    item = {"id": "safe-model", field: "prefix-" + key + "-suffix"}
    transport(env, lambda req: httpx.Response(200, json={"data": [item]}))
    response = client.post("/api/manage/providers/p/models/catalog", json={})
    assert response.status_code == 200 and response.json()["status"] == "unknown"
    assert key not in response.text
    assert get_app_setting("provider_catalog_v2:p") is None
    assert client.get("/api/manage/providers/p/models/catalog").json()["status"] == "not_fetched"
    with db() as conn:
        assert key not in str([tuple(r) for r in conn.execute("SELECT * FROM app_settings")])
        assert key not in str([tuple(r) for r in conn.execute("SELECT * FROM audit_logs")])


@pytest.mark.parametrize("field", ["model", "id"])
def test_inference_echoed_key_cannot_reach_ledger_or_response(env, field):
    client, monkeypatch, value, calls, secrets = env
    key = "opaqueCredentialExactlyWithoutPrefix7654321"
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {**value, "api_key": key})
    payload = {"id": "safe-response", "model": "selected", "choices": [{"message": {"content": "O"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}, field: "prefix-" + key + "-suffix"}
    transport(env, lambda req: httpx.Response(200, json=payload))
    response = client.post("/api/manage/providers/p/check", json={"mode": "inference", "model": "selected", "confirmed_paid": True})
    assert response.status_code == 200 and response.json()["status"] == "unknown"
    assert key not in response.text
    assert usage_service.requests()["total"] == 1
    assert usage_service.requests()["items"][0]["status"] == "unknown"
    with db() as conn:
        for table in ("usage_v2_attempts", "usage_v2_observations", "audit_logs"):
            assert key not in str([tuple(r) for r in conn.execute("SELECT * FROM " + table)])


def test_legacy_catalog_cache_never_read_back_as_safe(env):
    client, monkeypatch, value, calls, secrets = env
    set_app_setting("provider_catalog_v2:p", {"fingerprint": svc._fingerprint(value), "expires_at": "2099-01-01T00:00:00+00:00", "items": [{"id": "legacy-unsafe-secret"}]})
    response = client.get("/api/manage/providers/p/models/catalog")
    assert response.json()["status"] == "not_fetched"
    assert "legacy-unsafe-secret" not in response.text and secrets == []


def test_credential_as_requested_model_is_rejected_before_attempt(env):
    client, monkeypatch, value, calls, secrets = env
    key = "opaqueCredentialExactlyWithoutPrefix7654321"
    monkeypatch.setattr(svc, "resolve_provider", lambda _: {**value, "api_key": key})
    response = client.post("/api/manage/providers/p/check", json={"mode": "inference", "model": key, "confirmed_paid": True})
    assert response.status_code == 422 and key not in response.text and calls == []
    assert usage_service.requests()["total"] == 0


def test_model_permission_denied_is_one_failed_attempt_without_fallback(env):
    client, monkeypatch, value, calls, secrets = env
    transport(env, lambda req: httpx.Response(403, json={'error': {'message': 'private denial detail'}}))
    response = client.post('/api/manage/providers/p/check', json={'mode': 'inference', 'model': 'restricted-model', 'confirmed_paid': True})
    result = response.json()
    assert response.status_code == 200
    assert result['status'] == 'failed' and result['http_status'] == 403
    assert result['error_code'] == 'inference_rejected'
    assert len(calls) == 1
    assert json.loads(calls[0].content)['model'] == 'restricted-model'
    assert 'private denial detail' not in json.dumps(result)
    assert usage_service.requests()['items'][0]['status'] == 'failed'


def test_empty_catalog_stays_empty_and_does_not_trigger_inference(env):
    client, monkeypatch, value, calls, secrets = env
    transport(env, lambda req: httpx.Response(200, json={'object': 'list', 'data': []}))
    response = client.post('/api/manage/providers/p/models/catalog', json={})
    assert response.status_code == 200
    assert response.json()['items'] == []
    assert client.get('/api/manage/providers/p/models/catalog').json()['items'] == []
    assert len(calls) == 1 and calls[0].method == 'GET'
    assert usage_service.requests()['total'] == 0
