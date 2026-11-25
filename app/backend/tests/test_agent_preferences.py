import pytest

from app.integrations.config_transactions import ConfigurationError
from app.services import agent_preferences
from tests.test_management_experience import manager


def test_shortcuts_are_device_local_and_rank_before_catalog_paging(manager):
    assert agent_preferences.read(manager)['favorite_hosts'] == ['claude-code', 'codex', 'opencode']
    agent_preferences.update(manager, 'codex', 'unpin')
    agent_preferences.update(manager, 'omp', 'pin')
    for host in ['codex', 'omp', 'codex']:
        prefs = agent_preferences.update(manager, host, 'visit')
    assert prefs['recent_hosts'] == ['codex', 'omp']
    assert prefs['favorite_hosts'] == ['claude-code', 'opencode', 'omp']
    p = [manager.save('provider', {'name': str(i), 'protocol': 'openai', 'base_url': 'https://fixture.example/v1',
                                   'agent_ids': ['codex', 'omp']}) for i in range(5)]
    for i in (4, 3):
        agent_preferences.update(manager, 'codex', 'pin', p[i]['id'])
    assert manager.catalog_page('provider', agent_id='codex', limit=1)['items'][0]['id'] == p[4]['id']
    assert manager.catalog_page('provider', agent_id='omp', limit=1)['items'][0]['id'] == p[0]['id']
    agent_preferences.update(manager, 'codex', 'before', p[3]['id'], p[4]['id'])
    assert manager.catalog_page('provider', agent_id='codex', limit=1)['items'][0]['id'] == p[3]['id']
    with pytest.raises(ConfigurationError):
        agent_preferences.update(manager, 'codex', 'after', p[3]['id'], p[1]['id'])


def test_new_provider_can_explicitly_use_an_existing_account(manager):
    account = manager.save('account', {'name': 'Work', 'kind': 'api_key'})
    body = {'request_id': 'repeatable-account-setup', 'host_id': 'codex', 'model_id': 'model-a',
            'provider': {'name': 'Local', 'base_url': 'https://fixture.example/v1', 'protocol': 'openai',
                         'account_id': account['id'], 'credential_source': 'account'}}
    assert manager.setup_provider(body)['provider']['account_id'] == account['id']
    assert manager.setup_provider(body)['provider']['credential_source'] == 'account'
    with pytest.raises(ConfigurationError):
        manager.setup_provider({**body, 'secret': 'test-credential'})


def test_preferences_ignore_unknown_legacy_hosts_and_reject_variant_updates(manager):
    with manager.connect() as conn:
        agent_preferences._write(conn, "hosts", ["hermes", "codex", "codex", "antigravity-cli"])
        agent_preferences._write(conn, "recent-hosts", ["codex", "opencode", "codex"])
    prefs = agent_preferences.read(manager)
    assert set(prefs["host_names"]) == {"codex", "claude-code", "opencode", "omp", "cursor", "antigravity"}
    assert prefs["favorite_hosts"] == ["codex"]
    assert prefs["recent_hosts"] == ["codex", "opencode"]
    assert agent_preferences.update(manager, "codex", "unpin")["favorite_hosts"] == []
    with pytest.raises(ConfigurationError):
        agent_preferences.update(manager, "antigravity-cli", "visit")


def test_shared_codex_lists_desktop_definitions_and_preserves_active_receipt(manager):
    provider = manager.save("provider", {"name": "Desktop setup", "protocol": "openai",
                                         "base_url": "https://fixture.example/v1", "agent_ids": ["codex"],
                                         "supported_endpoints": ["responses"]})
    model = manager.save("model", {"name": "Local", "provider_id": provider["id"], "model_id": "test-model"})
    assert [item["id"] for item in manager.catalog_page("provider", agent_id="codex")["items"]] == [provider["id"]]
    unassigned = manager.assign_agent("provider", provider["id"], "codex", False, provider["version"])
    assert unassigned["agent_ids"] == []
    assert manager.catalog_page("provider", agent_id="codex")["items"] == []
    plan = manager.plan({"host_id": "codex", "activate_provider": True,
                         "provider_id": provider["id"], "model_id": model["id"]})
    manager.apply(plan["id"], plan["version"], True)
    codex = next(item for item in manager.hosts() if item["id"] == "codex")
    assert codex["shared_provider_target"] == "codex"
    assert manager.host_state(codex["shared_provider_target"])["selection"]["provider_id"] == provider["id"]
