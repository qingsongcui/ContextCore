"""Project composition must not pretend an unselected provider is active."""
import tomllib

import pytest

from app.integrations.config_transactions import ConfigurationError
from tests.test_management import manager


def provider(manager, name, host='codex'):
    return manager.setup_provider({'host_id': host, 'request_id': 'bundle-provider-' + name.replace(' ', '-'),
        'provider': {'name': name, 'protocol': 'anthropic' if host == 'claude-code' else 'openai', 'base_url': 'https://fixture.example/v1', 'wire_api': None if host == 'claude-code' else 'responses' if host == 'codex' else 'chat'},
        'model_id': name.replace(' ', '-') + '-native', 'secret': 'synthetic-bundle-key'})


def apply(manager, body):
    plan = manager.plan(body)
    return manager.apply(plan['id'], plan['version'], True)


@pytest.mark.parametrize('with_other_provider', [False, True])
def test_bundle_install_without_activation_retains_the_actual_selected_provider_and_model(manager, with_other_provider):
    current = provider(manager, 'Current')
    other = provider(manager, 'Other')
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    before = tomllib.loads((manager.home / '.codex/config.toml').read_text())
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Project configuration', 'asset_ids': [asset['id']], 'host_ids': ['codex'],
        **({'provider_id': other['provider']['id']} if with_other_provider else {})})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    native = tomllib.loads((manager.home / '.codex/config.toml').read_text())
    assert native['model_provider'] == before['model_provider'] and native['model'] == before['model']
    state = manager.host_state('codex')
    assert state['selection']['provider_id'] == current['provider']['id']
    assert state['selection']['model_id'] == 'Current-native'
    assert state['selection']['status'] == 'verified'
    assert state['resources'][asset['id']]['status'] == 'verified'


def test_bundle_install_without_activation_does_not_invent_a_first_model_selection(manager):
    other = provider(manager, 'Other')
    bundle = manager.save('bundle', {'name': 'Unselected provider', 'provider_id': other['provider']['id'], 'asset_ids': [], 'host_ids': ['codex']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    native = tomllib.loads((manager.home / '.codex/config.toml').read_text())
    assert 'model_provider' not in native and 'model' not in native
    state = manager.host_state('codex')
    assert state['selection']['provider_id'] is None and state['selection']['model_id'] is None
    assert state['selection']['status'] == 'not_managed'


def test_bundle_owned_removal_preserves_an_unrelated_active_selection(manager):
    current = provider(manager, 'Current')
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Tools only', 'asset_ids': [asset['id']], 'host_ids': ['codex']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id'], 'operation': 'uninstall'})
    state = manager.host_state('codex')
    assert state['selection']['provider_id'] == current['provider']['id'] and state['selection']['status'] == 'verified'
    assert state['resources'][asset['id']]['status'] == 'removed'


@pytest.mark.parametrize('host', ['claude-code', 'opencode'])
def test_tools_only_bundle_preserves_the_host_selected_model_and_its_readback(manager, host):
    current = provider(manager, 'Current', host)
    apply(manager, {'host_id': host, 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Tools only', 'asset_ids': [asset['id']], 'host_ids': [host]})
    apply(manager, {'host_id': host, 'bundle_id': bundle['id']})
    state = manager.host_state(host)
    assert state['selection']['provider_id'] == current['provider']['id']
    assert state['selection']['model_id'] == 'Current-native' and state['selection']['status'] == 'verified'


def test_first_claude_tools_bundle_does_not_claim_a_missing_managed_model_selection(manager):
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Tools only', 'asset_ids': [asset['id']], 'host_ids': ['claude-code']})
    apply(manager, {'host_id': 'claude-code', 'bundle_id': bundle['id']})
    state = manager.host_state('claude-code')
    assert state['selection']['provider_id'] is None and state['selection']['status'] == 'not_managed'


def test_tools_bundle_keeps_the_routed_gateway_selection_and_profile(manager):
    from tests.test_host_routing import preview, apply as apply_plan
    current = provider(manager, 'Current')
    apply_plan(manager, preview(manager, current))
    initial = manager.host_state('codex')['selection']
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Routed tools', 'asset_ids': [asset['id']], 'host_ids': ['codex']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    selection = manager.host_state('codex')['selection']
    assert selection['provider_id'] == initial['provider_id'] and selection['model_id'] == initial['model_id']
    assert selection['connection_mode'] == 'routing' and selection['status'] == 'verified'
    assert manager.connection_mode('codex')['mode'] == 'routing'


def test_changed_bundle_invalidates_the_whole_preview_before_credentials_or_files(manager, monkeypatch):
    current = provider(manager, 'Current')
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    original = {str(path): path.read_bytes() for path in manager.home.rglob('*') if path.is_file()}
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Project tools', 'asset_ids': [asset['id']], 'host_ids': ['codex']})
    plan = manager.plan({'host_id': 'codex', 'bundle_id': bundle['id']})
    manager.save('bundle', {'asset_ids': []}, bundle['id'], bundle['version'])
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('Stale bundle preview must not open the vault'))
    with pytest.raises(ConfigurationError, match='changed since preview'):
        manager.apply(plan['id'], plan['version'], True)
    assert {str(path): path.read_bytes() for path in manager.home.rglob('*') if path.is_file()} == original


def test_bundle_preview_describes_real_resources_and_the_selected_model_without_credentials(manager):
    import json
    value = provider(manager, 'Project provider')
    tool = manager.save('asset', {'name': 'My research tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    skill = manager.save('asset', {'name': 'My writing skill', 'kind': 'skill', 'files': {'SKILL.md': '# Synthetic skill'}})
    bundle = manager.save('bundle', {'name': 'Writing project', 'provider_id': value['provider']['id'], 'asset_ids': [tool['id'], skill['id']], 'host_ids': ['codex']})
    plan = manager.plan({'host_id': 'codex', 'bundle_id': bundle['id'], 'activate_provider': True, 'model_id': value['model']['id']})
    metadata = plan['payload']['metadata']
    assert [row['name'] for row in metadata['configuration_changes']] == ['My research tool', 'My writing skill', 'Project provider']
    assert [row['kind'] for row in metadata['configuration_changes']] == ['mcp', 'skill', 'provider']
    assert metadata['model_selection']['provider_name'] == 'Project provider'
    assert metadata['model_selection']['native_model'] == 'Project-provider-native'
    assert 'synthetic-bundle-key' not in json.dumps(plan)
    assert 'credential_ref' not in json.dumps(metadata['configuration_changes'])


def legacy_bundle_receipt(manager, provider_id=None, host='codex'):
    """Emulate the exact header overwrite made by pre-fix bundle application."""
    import json
    with manager.connect() as conn:
        row = conn.execute('SELECT data FROM management_activations WHERE host_id=?', (host,)).fetchone()
        value = json.loads(row[0])
        value.update(provider_id=provider_id, model_id=None)
        value.pop('selection_receipt_version', None)
        value.pop('selection_receipt_unknown', None)
        conn.execute('UPDATE management_activations SET data=? WHERE host_id=?', (json.dumps(value), host))
    return value


@pytest.mark.parametrize('prior', ['active', 'never', 'restored'])
def test_old_bundle_header_is_reconciled_from_explicit_selection_receipts_without_writes(manager, monkeypatch, prior):
    import json
    current, other = provider(manager, 'Current'), provider(manager, 'Other')
    if prior != 'never':
        apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    if prior == 'restored':
        apply(manager, {'host_id': 'codex', 'operation': 'restore'})
    bundle = manager.save('bundle', {'name': 'Old bundle', 'provider_id': other['provider']['id'], 'asset_ids': [], 'host_ids': ['codex']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    corrupt = legacy_bundle_receipt(manager, other['provider']['id'])
    native = (manager.home / '.codex/config.toml').read_bytes()
    monkeypatch.setattr(manager.vault, 'get', lambda *_: pytest.fail('Selection repair must not read credentials'))
    selection = manager.host_state('codex')['selection']
    assert selection['provider_id'] == (current['provider']['id'] if prior == 'active' else None)
    assert selection['model_id'] == ('Current-native' if prior == 'active' else None)
    assert (manager.home / '.codex/config.toml').read_bytes() == native
    with manager.connect() as conn:
        assert json.loads(conn.execute('SELECT data FROM management_activations WHERE host_id=?', ('codex',)).fetchone()[0]) == corrupt


def test_next_bundle_apply_preserves_the_reconciled_legacy_selection_in_its_receipt(manager):
    import json
    current = provider(manager, 'Current')
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Old bundle', 'asset_ids': [asset['id']], 'host_ids': ['codex']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    legacy_bundle_receipt(manager)
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    with manager.connect() as conn:
        active = json.loads(conn.execute('SELECT data FROM management_activations WHERE host_id=?', ('codex',)).fetchone()[0])
    assert active['provider_id'] == current['provider']['id'] and active['model_id'] == 'Current-native'


def test_routing_retains_the_correct_direct_selection_after_legacy_bundle_corruption(manager):
    from app.services.host_routing import HostRouting
    from tests.test_host_routing import preview, apply as apply_plan
    current, other = provider(manager, 'Current'), provider(manager, 'Other')
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    bundle = manager.save('bundle', {'name': 'Old bundle', 'provider_id': other['provider']['id'], 'asset_ids': [], 'host_ids': ['codex']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    legacy_bundle_receipt(manager, other['provider']['id'])
    apply_plan(manager, preview(manager, other))
    assert HostRouting(manager).private_state('codex')['direct'] == {'provider_id': current['provider']['id'], 'model_id': 'Current-native'}


def test_old_wrong_bundle_header_does_not_block_removing_an_unselected_named_provider(manager):
    current, other = provider(manager, 'Current'), provider(manager, 'Other')
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    bundle = manager.save('bundle', {'name': 'Old bundle', 'provider_id': other['provider']['id'], 'asset_ids': [], 'host_ids': ['codex']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    legacy_bundle_receipt(manager, other['provider']['id'])
    apply(manager, {'host_id': 'codex', 'provider_id': other['provider']['id'], 'operation': 'uninstall'})
    assert manager.host_state('codex')['selection']['provider_id'] == current['provider']['id']


def test_active_claude_provider_remains_protected_from_deletion_after_legacy_tools_bundle(manager):
    current = provider(manager, 'Current', 'claude-code')
    apply(manager, {'host_id': 'claude-code', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Old bundle', 'asset_ids': [asset['id']], 'host_ids': ['claude-code']})
    apply(manager, {'host_id': 'claude-code', 'bundle_id': bundle['id']})
    legacy_bundle_receipt(manager, host='claude-code')
    manager.delete('model', current['model']['id'], current['model']['version'])
    with pytest.raises(ConfigurationError, match='active'):
        manager.delete('provider', current['provider']['id'], current['provider']['version'])


def test_missing_legacy_selection_evidence_stays_unknown_until_explicit_activation(manager):
    from tests.test_host_routing import preview
    current = provider(manager, 'Current')
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    asset = manager.save('asset', {'name': 'Bundle tool', 'kind': 'mcp', 'config': {'command': 'synthetic-mcp'}})
    bundle = manager.save('bundle', {'name': 'Old bundle', 'asset_ids': [asset['id']], 'host_ids': ['codex']})
    apply(manager, {'host_id': 'codex', 'bundle_id': bundle['id']})
    legacy_bundle_receipt(manager)
    with manager.connect() as conn:
        conn.execute("DELETE FROM management_plans WHERE json_extract(payload,'$.metadata.activate_provider')=1")
    selection = manager.host_state('codex')['selection']
    assert selection['provider_id'] is None and selection['status'] == 'not_checked' and selection['receipt_unknown'] is True
    with pytest.raises(ConfigurationError, match='Legacy selection receipt'):
        preview(manager, current)
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    selection = manager.host_state('codex')['selection']
    assert selection['provider_id'] == current['provider']['id'] and selection['status'] == 'verified'
    assert selection['receipt_unknown'] is False


def test_status_reads_only_the_header_of_a_large_retained_restoration_ledger(manager):
    import json, tracemalloc
    current = provider(manager, 'Current')
    apply(manager, {'host_id': 'codex', 'provider_id': current['provider']['id'], 'model_id': current['model']['id'], 'activate_provider': True})
    with manager.connect() as conn:
        value = json.loads(conn.execute('SELECT data FROM management_activations WHERE host_id=?', ('codex',)).fetchone()[0])
        for i in range(6000):
            path = str(manager.home / '.codex/skills' / f'asset_{i:016x}' / 'SKILL.md')
            value['files'][path] = {'snapshot': f'synthetic_snapshot_{i:032x}', 'hash': '0' * 64}
            value['fields'].append({'path': path, 'kind': 'file', 'owner': f'asset_{i:016x}'})
        raw = json.dumps(value)
        assert len(raw) > 2 * 1024 * 1024
        conn.execute('UPDATE management_activations SET data=? WHERE host_id=?', (raw, 'codex'))
    tracemalloc.start()
    try:
        state = manager.host_state('codex')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert state['selection']['provider_id'] == current['provider']['id']
    assert len(json.dumps(state)) < 5000
    print('SELECTION_HEADER_SAMPLE ' + json.dumps({'retained_fields': len(value['fields']), 'ledger_bytes': len(raw), 'python_peak_bytes': peak, 'response_bytes': len(json.dumps(state))}))
    assert peak < 2 * 1024 * 1024, f'Restoration ledger was materialized in Python: {peak} bytes'
