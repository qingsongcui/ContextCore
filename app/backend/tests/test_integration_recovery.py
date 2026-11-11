import json
import tomllib
from pathlib import Path
import pytest
from app.integrations.installer import apply_host_integration
from app.integrations.recovery import plan_recovery, apply_recovery, RecoveryConflict

@pytest.mark.parametrize('host', ['omp', 'opencode', 'antigravity', 'codex', 'cursor', 'claude-code'])
def test_detach_preserves_foreign_configuration_and_private_backup(tmp_path, host):
    install = apply_host_integration(host, token='synthetic-recovery-token', confirmed=True, home=tmp_path)
    manifest = json.loads(Path(install.manifest_path).read_text())
    assert 'synthetic-recovery-token' not in json.dumps(manifest)
    assert 'owned_mcp_entry' not in manifest
    assert len(manifest['owned_mcp_entry_sha256']) == 64
    config = Path(install.mcp_path)
    if host == 'codex':
        config.write_text(config.read_text()+'\n[user_models]\nkeep = [1, 2]\n')
    else:
        data = json.loads(config.read_text()); data['user_models'] = {'keep': [1, 2]}
        config.write_text(json.dumps(data))
    config.chmod(0o600)
    before = config.read_bytes()
    plan = plan_recovery(host, tmp_path)
    assert config.read_bytes() == before
    assert 'synthetic-recovery-token' not in json.dumps(plan.summary())
    result = apply_recovery(plan, tmp_path/'private/receipt', confirmed=True)
    assert result['status'] == 'detached'
    data = tomllib.loads(config.read_text()) if host == 'codex' else json.loads(config.read_text())
    assert data['user_models'] == {'keep': [1, 2]}
    assert not Path(install.skill_path).exists()
    assert not Path(install.token_path).exists()
    assert (tmp_path/'private/receipt/0.original').read_bytes() == before
    assert (tmp_path/'private/receipt/0.original').stat().st_mode & 0o077 == 0


def test_modified_antigravity_hook_refuses_all_changes(tmp_path):
    install = apply_host_integration('antigravity', token='synthetic', confirmed=True, home=tmp_path)
    path = Path(install.hooks_path)
    data = json.loads(path.read_text()); data['contextcore-session']['custom'] = True
    path.write_text(json.dumps(data)); before = Path(install.mcp_path).read_bytes()
    with pytest.raises(RecoveryConflict, match='Externally modified hook'): plan_recovery('antigravity', tmp_path)
    assert Path(install.mcp_path).read_bytes() == before
    assert Path(install.manifest_path).exists()


def test_stale_plan_writes_nothing(tmp_path):
    install = apply_host_integration('omp', token='synthetic', confirmed=True, home=tmp_path)
    plan = plan_recovery('omp', tmp_path)
    Path(install.hooks_path).write_text('// external edit')
    with pytest.raises(RecoveryConflict, match='changed after preview'):
        apply_recovery(plan, tmp_path/'private/receipt', confirmed=True)
    assert Path(install.token_path).exists()
    assert not (tmp_path/'private/receipt').exists()


def test_failed_second_write_restores_all_previous_changes(tmp_path, monkeypatch):
    from app.integrations import recovery
    install = apply_host_integration('antigravity', token='synthetic', confirmed=True, home=tmp_path)
    plan = plan_recovery('antigravity', tmp_path)
    real = recovery._atomic_write_file
    def fail(path, content, mode=0o600):
        if path == Path(install.hooks_path): raise OSError('synthetic disk failure')
        return real(path, content, mode)
    monkeypatch.setattr(recovery, '_atomic_write_file', fail)
    with pytest.raises(OSError): apply_recovery(plan, tmp_path/'private/receipt', confirmed=True)
    for c in plan.changes:
        assert (c.path.read_bytes() if c.path.exists() else None) == c.before
    assert json.loads((tmp_path/'private/receipt/receipt.json').read_text())['status'] == 'rolled_back'


def test_public_uninstaller_preserves_all_files_on_antigravity_conflict(tmp_path):
    from app.integrations.installer import uninstall_host_integration
    install = apply_host_integration('antigravity', token='synthetic', confirmed=True, home=tmp_path)
    path = Path(install.hooks_path)
    data = json.loads(path.read_text())
    data['contextcore-tool']['user_change'] = True
    path.write_text(json.dumps(data))
    paths = [Path(getattr(install, name)) for name in
             ['mcp_path', 'hooks_path', 'skill_path', 'token_path', 'manifest_path']]
    before = {p: p.read_bytes() for p in paths}
    result = uninstall_host_integration('antigravity', confirmed=True, home=tmp_path)
    assert result.status == 'conflict'
    assert all(p.read_bytes() == content for p, content in before.items())


def test_legacy_raw_entry_manifest_can_be_recovered_without_migration_write(tmp_path):
    install = apply_host_integration('omp', token='legacy-test-secret', confirmed=True, home=tmp_path)
    path = Path(install.manifest_path)
    manifest = json.loads(path.read_text())
    del manifest['owned_mcp_entry_sha256']
    manifest['version'] = '1.0.0'
    manifest['owned_mcp_entry'] = json.loads(Path(install.mcp_path).read_text())['mcpServers']['contextcore']
    path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    plan = plan_recovery('omp', tmp_path)
    assert path.read_bytes() == before
    assert 'legacy-test-secret' not in json.dumps(plan.summary())
    assert apply_recovery(plan, tmp_path/'private/receipt', confirmed=True)['status'] == 'detached'


@pytest.mark.parametrize('status', ['partial', 'conflict', 'error'])
def test_service_does_not_disable_or_report_success_after_incomplete_uninstall(monkeypatch, status):
    from fastapi import HTTPException
    from app.integrations import installer
    from app.services import connection_service
    monkeypatch.setattr(connection_service, 'get_connection', lambda _: {'id': 'omp', 'enabled': True})
    monkeypatch.setattr(installer, 'uninstall_host_integration',
                        lambda *a, **kw: installer.UninstallResult('omp', status))
    def unexpected_db():
        raise AssertionError('Incomplete uninstall must not disable connection or revoke token')
    monkeypatch.setattr(connection_service, 'db', unexpected_db)
    with pytest.raises(HTTPException) as exc:
        connection_service.uninstall_connection('omp', confirmed=True)
    assert exc.value.status_code == 409


@pytest.mark.parametrize('host', ['codex', 'cursor', 'claude-code'])
def test_modified_skill_prevents_any_other_uninstall_write(tmp_path, host):
    from app.integrations.installer import uninstall_host_integration
    install = apply_host_integration(host, token='synthetic', confirmed=True, home=tmp_path)
    Path(install.skill_path).write_text('User customized instructions')
    paths = [Path(getattr(install, name)) for name in
             ['mcp_path', 'hooks_path', 'skill_path', 'token_path', 'manifest_path']]
    before = {p: p.read_bytes() for p in paths}
    result = uninstall_host_integration(host, confirmed=True, home=tmp_path)
    assert result.status == 'conflict'
    assert all(p.read_bytes() == content for p, content in before.items())
