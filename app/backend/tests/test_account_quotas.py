"""Native sign-in, account isolation and empty-home quota acceptance, without live credentials."""
import asyncio
import json
import sys

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.config_transactions import ConfigurationError
from app.routers.account_quotas import router
from app.services import account_quotas as quotas, management_service, quota_readers as readers
from tests.test_management import FakeVault


@pytest.fixture
def manager(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return management_service.ManagementService(tmp_path / "state.db", home, FakeVault())


def sources(manager, **filters):
    return {item["id"]: item for item in quotas.list_quotas(manager, **filters)["items"]}


def refresh(manager, item, handler=None):
    transport = httpx.MockTransport(handler) if handler is not None else None
    return quotas.refresh_quota(manager, item["id"], item["revision"], True, transport=transport)


def provider(manager, url):
    return manager.save("provider", {"name": "Upstream", "protocol": "openai", "base_url": url, "agent_ids": ["codex"]})


def test_clean_home_cache_only_and_scoped_readback(manager, monkeypatch):
    monkeypatch.setattr(management_service, "service", lambda: manager)
    monkeypatch.setattr(manager.vault, "get", lambda *_: pytest.fail("GET read a secret"))
    monkeypatch.setattr(readers, "read_codex", lambda *_: pytest.fail("GET started Codex"))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    items = client.get("/api/account-quotas").json()["items"]
    assert [i["id"] for i in items] == ["native-codex", "native-claude"]
    assert [i["id"] for i in client.get("/api/account-quotas?host_id=codex").json()["items"]] == ["native-codex"]
    assert client.get("/api/account-quotas?host_id=opencode").json()["items"] == []
    assert {i["status"] for i in items} == {"native_only", "not_checked"}
    assert client.post("/api/account-quotas/native-codex/refresh",
                       json={"confirmed": "true", "revision": items[0]["revision"]}).status_code == 422
    assert client.post("/api/account-quotas/native-codex/refresh",
                       json={"confirmed": True, "revision": "stale"}).status_code == 409


def test_bound_account_currency_no_redirect_and_stale_receipts(manager):
    p = provider(manager, "https://api.deepseek.com/v1")
    accounts = []
    for suffix in ("A", "B"):
        a = manager.save("account", {"name": suffix, "kind": "api_key", "provider_id": p["id"]})
        manager.credential("account", a["id"], "synthetic-" + suffix)
        accounts.append(a["id"])
    item = sources(manager, account_id=accounts[0])[accounts[0]]
    assert item["host_ids"] == ["codex"] and item["source"] == "deepseek"
    assert item["account_id"] == accounts[0]

    def upstream(request):
        assert str(request.url) == readers.ENDPOINTS["deepseek"]
        assert request.headers["authorization"] == "Bearer synthetic-A"
        return httpx.Response(200, json={"balance_infos": [
            {"total_balance": "0.0011", "currency": "CNY"}, {"total_balance": "0.00", "currency": "USD"}]})

    success = refresh(manager, item, upstream)
    assert success["status"] == "available"
    assert success["snapshot"]["balances"] == [{"unit": "CNY", "amount": "0.0011"}, {"unit": "USD", "amount": "0.00"}]
    assert sources(manager)[accounts[1]]["snapshot"] is None
    for status, expected in [(429, "rate_limited"), (503, "offline"), (401, "unauthorized"), (307, "unreadable")]:
        observed = []
        def failure(request):
            observed.append(str(request.url))
            return httpx.Response(status, headers={"location": "https://nonprovider.example.test"}, text="secret-upstream-body")
        saved = refresh(manager, item, failure)
        assert observed == [readers.ENDPOINTS["deepseek"]]
        assert saved["status"] == expected and saved["last_success_at"] == success["last_success_at"]
        assert saved["last_attempt_at"] >= saved["last_success_at"] and saved["snapshot"] == success["snapshot"]
    assert all(s not in json.dumps(sources(manager)) for s in ("secret-upstream-body", "synthetic-A", "keychain:senrelay:"))
    manager.credential("account", accounts[0], "changed-key")
    assert sources(manager)[accounts[0]]["snapshot"] is None
    with pytest.raises(ConfigurationError, match="source has changed"):
        refresh(manager, item, lambda _: pytest.fail("stale click used another key"))
    manager.delete("account", accounts[0])
    with manager.connect() as conn:
        assert not conn.execute("SELECT * FROM account_quota_receipts WHERE id=?", (accounts[0],)).fetchone()


def test_custom_origins_unsupported_even_when_named_like_official(manager, monkeypatch):
    p = provider(manager, "https://api.deepseek.com.example.test/v1")
    manager.credential("provider", p["id"], "never-sent")
    item = sources(manager)[p["id"]]
    assert item["source"] == "custom" and item["status"] == "unsupported"
    monkeypatch.setattr(manager.vault, "get", lambda *_: pytest.fail("unsupported source read a secret"))
    with pytest.raises(ConfigurationError):
        refresh(manager, item)


@pytest.mark.parametrize("source,payload,expected", [
    ("openrouter", {"data": {"limit_remaining": 0, "label": "secret-prefix-suffix"}}, {"balances": [{"unit": "USD", "amount": "0"}]}),
    ("openrouter", {"data": {"limit_remaining": None, "limit": None}}, {"no_key_limit": True}),
    ("moonshot", {"code": 0, "status": True, "data": {"available_balance": 8.125}}, {"balances": [{"unit": "CNY", "amount": "8.125"}]}),
])
def test_public_balance_shapes(source, payload, expected):
    result = asyncio.run(readers.read_balance(source, "fake", httpx.MockTransport(lambda _: httpx.Response(200, json=payload))))
    assert all(result[key] == value for key, value in expected.items())
    assert "secret-prefix" not in json.dumps(result)


def test_unreadable_amounts_are_not_zero():
    for value in (None, True, "NaN", "1E999999999", {}):
        with pytest.raises(readers.QuotaUnavailable) as exc:
            asyncio.run(readers.read_balance("openrouter", "fake", httpx.MockTransport(
                lambda _: httpx.Response(200, json={"data": {"limit_remaining": value}}))))
        assert exc.value.status == "unreadable"


def test_app_server_protocol_and_signout(manager, monkeypatch):
    # This is a real subprocess speaking stdio JSON-RPC, with no account files or network.
    script = manager.home / "fixture-codex"
    script.write_text("#!" + sys.executable + '''
import json, os, sys
from pathlib import Path
assert sys.argv[1:] == ["app-server"]
assert Path(os.environ["CODEX_HOME"]).parent == Path.cwd()
assert "OPENAI_API_KEY" not in os.environ
account = {"type":"chatgpt","email":"sample@example.test","planType":"plus","workspaceName":"Synthetic"}
snap = {"limitId":"codex","primary":{"usedPercent":20,"windowDurationMins":300,"resetsAt":1800000000},
        "secondary":None,"credits":{"balance":"1.2","unlimited":False,"hasCredits":True}}
for line in sys.stdin:
    r = json.loads(line)
    m = r["method"]
    if m == "initialized":
        continue
    if m == "initialize":
        assert r["params"]["clientInfo"]["name"] == "senrelay"
        result = {}
    elif m == "account/read":
        assert r["params"] == {"refreshToken":False}
        result = {"account":None if Path("signed-out").exists() else account}
    elif m == "account/rateLimits/read":
        result = {"rateLimits":snap,"rateLimitsByLimitId":{"codex":snap}}
    else:
        raise ValueError(m)
    print(json.dumps({"id":r["id"],"result":result}),flush=True)
''')
    script.chmod(0o700)
    monkeypatch.setenv("OPENAI_API_KEY", "private-environment-marker")
    monkeypatch.setenv("CODEX_HOME", "/must-not-be-used-in-isolation")
    monkeypatch.setattr(readers, "codex_command", lambda _: str(script))
    item = sources(manager)["native-codex"]
    result = refresh(manager, item)
    assert result["snapshot"]["identity_label"] == "s…@example.test"
    assert "sample@example.test" not in json.dumps(result)
    assert result["snapshot"]["windows"] == [{
        "label": "codex", "rank": "primary", "used_percent": 20, "minutes": 300,
        "resets_at": "2027-01-15T08:00:00+00:00",
    }]
    assert result["snapshot"]["balances"] == [{"unit": "credits", "amount": "1.2"}]
    (manager.home / "signed-out").touch()
    failed = refresh(manager, item)
    assert failed["status"] == "unauthorized" and failed["snapshot"] is None


def test_rebinding_during_quota_read_discards_response(manager):
    p = provider(manager, "https://api.deepseek.com")
    manager.credential("provider", p["id"], "initial-fake")
    item = sources(manager)[p["id"]]
    def upstream(request):
        manager.credential("provider", p["id"], "next-fake")
        return httpx.Response(200, json={"balance_infos": [{"total_balance": "100", "currency": "CNY"}]})
    with pytest.raises(ConfigurationError, match="source has changed"):
        refresh(manager, item, upstream)
    assert sources(manager)[p["id"]]["snapshot"] is None


def test_management_key_credits_stay_distinct_from_the_key_budget():
    def official(request):
        assert request.headers["Authorization"] == "Bearer fixture"
        if request.url.path == "/api/v1/key":
            return httpx.Response(200, json={"data": {"is_management_key": True, "limit_remaining": 2.13}})
        assert request.url.path == "/api/v1/credits"
        return httpx.Response(200, json={"data": {"total_credits": 10.0, "total_usage": 1.236}})
    data = asyncio.run(readers.read_balance("openrouter", "fixture", httpx.MockTransport(official)))
    assert data["balances"] == [{"unit": "USD", "amount": "2.13"}, {"unit": "USD", "amount": "8.764", "scope": "account"}]
