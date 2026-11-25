"""User-profile resolution for managed native reads, writes and recovery."""
import json
from pathlib import Path
import tomllib

import pytest
from app.integrations.config_transactions import ConfigurationError
from app.services import management_undo, native_catalog
from app.services.management_service import ManagementService
from tests.test_management import FakeVault, mcp


def create(tmp_path, monkeypatch):
    home = (tmp_path / 'profiles-home').resolve()
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('CODEX_HOME', raising=False)
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)
    return ManagementService(tmp_path/'management/db', vault=FakeVault())


def apply(manager, plan):
    return manager.apply(plan['id'], plan['version'], True)


def test_codex_custom_root_native_switch_lookup_and_restore(tmp_path, monkeypatch):
    manager = create(tmp_path, monkeypatch)
    root = manager.home/'profiles/work'
    root.mkdir(parents=True)
    monkeypatch.setenv('CODEX_HOME', str(root))
    config = root/'config.toml'
    config.write_text('model = "original"\n')
    registered = {host['id']:host for host in manager.hosts()}
    assert registered['codex']['config_root']['path'] == str(root)
    assert registered['codex']['shared_configuration'] is False
    assert registered['codex']['shared_configuration'] is False
    assert registered['codex']['paths']['mcp_path'] == str(config)
    assert native_catalog.preview(manager, 'codex', 'providers')['source_path'] == str(config)
    provider = manager.save('provider', {'name':'Example', 'protocol':'openai','base_url':'https://fixture.example/v1'})
    model = manager.save('model', {'provider_id':provider['id'], 'name':'local', 'model_id':'model-a'})
    req = {'host_id':'codex','activate_provider':True,'provider_id':provider['id'],'model_id':model['id']}
    initial = manager.plan(req)
    assert not initial['payload']['metadata']['shared_configuration']
    apply(manager, initial)
    assert tomllib.loads(config.read_text())['model'] == 'model-a'
    assert manager.host_state('codex')['selection']['status'] == 'verified'
    monkeypatch.delenv('CODEX_HOME')
    host = next(h for h in manager.hosts() if h['id']=='codex')
    assert host['config_root']['reason'] == 'previous_root'
    assert host['config_root']['previous_path'] == str(root)
    with pytest.raises(ConfigurationError):
        manager.plan({'host_id':'codex','operation':'restore'})
    with pytest.raises(ConfigurationError):
        management_undo.preview(manager, initial['id'])
    monkeypatch.setenv('CODEX_HOME', str(root))
    apply(manager, manager.plan({'host_id':'codex','operation':'restore'}))
    assert tomllib.loads(config.read_text())['model'] == 'original'
    assert not (manager.home/'.codex/config.toml').exists()


@pytest.mark.parametrize('custom_default', [False, True])
def test_claude_native_mcp_does_not_use_default_global_json(tmp_path, monkeypatch, custom_default):
    manager = create(tmp_path, monkeypatch)
    root = manager.home/('.claude' if custom_default else 'profiles/personal')
    root.mkdir(parents=True)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(root))
    native = root/'.claude.json'
    native.write_text('{"mcpServers":{"original":{"command":"demo"}}}')
    (manager.home/'.claude.json').write_text('{"unmanaged":true}')
    assert native_catalog.preview(manager, 'claude-code', 'mcp')['source_path'] == str(native)
    asset = mcp(manager)
    plan = manager.plan({'host_id':'claude-code','asset_ids':[asset['id']]})
    monkeypatch.delenv('CLAUDE_CONFIG_DIR')
    with pytest.raises(ConfigurationError):
        apply(manager, plan)
    with pytest.raises(ConfigurationError):
        manager.recover_plan(plan['id'], True)
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(root))
    apply(manager, plan)
    assert len(json.loads(native.read_text())['mcpServers']) == 2
    assert (manager.home/'.claude.json').read_text() == '{"unmanaged":true}'


@pytest.mark.parametrize('override', ['../relative', '/tmp/external-agent-home', 'link'])
def test_blocked_root_prevents_writes_and_explicit_home_stays_isolated(tmp_path, monkeypatch, override):
    manager = create(tmp_path, monkeypatch)
    target = manager.home/override if override == 'link' else override
    if override == 'link':
        target.symlink_to(tmp_path)
    monkeypatch.setenv('CODEX_HOME', str(target))
    asset = mcp(manager)
    host = next(h for h in manager.hosts() if h['id']=='codex')
    assert host['config_root']['status'] == 'blocked'
    with pytest.raises(ConfigurationError):
        manager.quick_toggle_asset('codex', asset['id'], True, auto_apply=True)
    with pytest.raises(ConfigurationError):
        native_catalog.preview(manager, 'codex', 'mcp')
    assert not (manager.home/'.codex/config.toml').exists()
    isolated = ManagementService(tmp_path/'isolated/db', manager.home, vault=FakeVault())
    assert isolated.host_config('codex')['shared_configuration'] is True
    assert 'config_root' not in isolated.host_config('codex')


def test_shared_matrix_keeps_unaffected_hosts_available(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import manage
    manager = create(tmp_path, monkeypatch)
    asset = mcp(manager)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path/'outside'))
    monkeypatch.setattr(manage.management, 'service', lambda: manager)
    api = FastAPI()
    api.include_router(manage.router)
    response = TestClient(api).get('/api/manage/shared-resource-matrix?host_ids=codex,cursor&kind=mcp')
    assert response.status_code == 200
    row = next(item for item in response.json()['items'] if item['id'] == asset['id'])
    assert row['cells']['codex']['status'] == 'not_checked'
    assert row['cells']['cursor']['status'] == 'not_managed'


def test_codex_custom_root_plan_rechecks_scope_before_write(tmp_path, monkeypatch):
    manager = create(tmp_path, monkeypatch)
    monkeypatch.setenv('CODEX_HOME', str(manager.home/'separate-cli'))
    asset = mcp(manager)
    plan = manager.plan({'host_id':'codex','asset_ids':[asset['id']]})
    assert plan['payload']['metadata']['shared_configuration'] is False
    monkeypatch.delenv('CODEX_HOME')
    with pytest.raises(ConfigurationError, match='configuration root changed'):
        apply(manager, plan)
    assert not (manager.home/'.codex/config.toml').exists()
