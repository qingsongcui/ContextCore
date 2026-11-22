import json
from pathlib import Path
import pytest

from app.integrations.config_transactions import ConfigurationError


@pytest.fixture
def plans(tmp_path, monkeypatch):
    from app.database import create_schema, seed_data
    from app.services.connection_plans import ConnectionPlans
    create_schema(); seed_data()
    return ConnectionPlans(home=Path.home())


def create(plans, capabilities=None, operation='install'):
    return plans.create('omp', {'operation': operation, 'capabilities': capabilities or ['context_tools'],
                                'surface': 'cli'}, 'http://127.0.0.1:8420')


def apply(plans, plan):
    return plans.apply('omp', plan['id'], plan['version'], True)


def test_preview_writes_no_host_files_or_credentials(plans):
    from app.services.connection_plans import public_plan
    before = set(plans.home.rglob('*'))
    plan = create(plans)
    assert set(plans.home.rglob('*')) == before
    assert plan['status'] == 'planned'
    assert not (plans.home/'.contextcore').exists()
    assert '<credential-created-after-confirmation>' not in json.dumps(plan)
    assert 'token_hash' not in json.dumps(public_plan(plan))
    assert all(not item['specs'] for item in plan['payload']['files'])


def test_mcp_grant_does_not_authorize_history_or_hooks(plans):
    from app.services.connection_service import get_connection, verify_agent_token
    plan = create(plans)
    applied = apply(plans, plan)
    token = (plans.home/'.contextcore/credentials/omp.token').read_text().strip()
    assert applied['status'] == 'applied'
    assert applied['payload']['readback'] == 'verified'
    state = get_connection('omp')
    assert state['enabled'] is False
    assert state['authorizations']['context_tools'] is True
    assert state['authorizations']['lifecycle'] is False
    assert verify_agent_token(token) == 'omp'
    assert verify_agent_token(token, purpose='lifecycle') is None
    assert not (plans.home/'.omp/agent/hooks').exists()
    assert not (plans.home/'.omp/agent/commands').exists()
    assert token not in json.dumps(plans.transactions.get(plan['id']))
    assert token not in (plans.home/'.contextcore/manifests/omp.json').read_text()


def test_skill_only_does_not_create_token_or_context_grant(plans):
    from app.services.connection_service import get_connection
    apply(plans, create(plans, ['continuation']))
    assert not (plans.home/'.contextcore/credentials/omp.token').exists()
    state = get_connection('omp')
    assert not state['authorizations']['context_tools']
    assert state['authorizations']['continuation']


@pytest.mark.parametrize(('host', 'rule_directory'), [
    ('claude-code', '.claude/rules'), ('omp', '.omp/agent/rules'), ('antigravity', '.gemini/config/rules'),
])
def test_daily_rule_is_reviewable_owned_and_preserves_user_rules(plans, host, rule_directory):
    user_rules = plans.home / rule_directory
    user_rules.mkdir(parents=True)
    foreign_rule = user_rules / 'user-style.md'
    foreign_rule.write_text('Existing user preference.')
    daily_rule = user_rules / 'senrelay-working-memory.md'
    def plan(operation):
        return plans.create(host, {'operation': operation, 'capabilities': ['continuation'],
                                   'surface': 'cli'}, 'http://127.0.0.1:8420')

    daily_rule.write_text('Existing rule at the same path.')
    with pytest.raises(ValueError, match='unowned or modified rule'):
        plan('install')
    daily_rule.unlink()
    preview = plan('install')
    assert str(daily_rule) in {entry['path'] for entry in preview['payload']['files']}
    assert not daily_rule.exists()
    assert plans.apply(host, preview['id'], preview['version'], True)['status'] == 'applied'
    installed = daily_rule.read_bytes()
    assert b'bootstrap_working_memory' in installed and b'cite_memory' in installed
    if host == 'omp':
        assert installed.startswith(b'---\nalwaysApply: true\n---\n')
    if host == 'antigravity':
        assert installed.startswith(b'---\ntrigger: always_on\n---\n')
    daily_rule.write_text('User edits this rule after installation.')
    with pytest.raises(ValueError, match='Owned file changed'):
        plan('uninstall')
    daily_rule.write_bytes(installed)
    removal = plan('uninstall')
    assert plans.apply(host, removal['id'], removal['version'], True)['status'] == 'applied'
    assert not daily_rule.exists()
    assert foreign_rule.read_text() == 'Existing user preference.'


def test_codex_rule_follows_active_file_with_exact_ownership(plans):
    from app.integrations.recovery import CODEX_RULE_START
    root = plans.home / '.codex'
    root.mkdir()
    regular = root / 'AGENTS.md'
    override = root / 'AGENTS.override.md'
    foreign_regular = b'Existing user notes without a trailing newline.'
    foreign_override = b'# Active user override\r\n'
    regular.write_bytes(foreign_regular)
    def plan(operation):
        return plans.create('codex', {'operation': operation, 'capabilities': ['continuation'],
                                      'surface': 'cli'}, 'http://127.0.0.1:8420')
    def apply_operation(prepared):
        return plans.apply('codex', prepared['id'], prepared['version'], True)

    stale = plan('install')
    assert {str(regular), str(override)} <= {entry['path'] for entry in stale['payload']['files']}
    override.write_bytes(foreign_override)
    with pytest.raises(ConfigurationError, match='changed since preview'):
        apply_operation(stale)
    assert apply_operation(plan('install'))['status'] == 'applied'
    assert regular.read_bytes() == foreign_regular
    before = override.read_bytes()
    assert before.startswith(foreign_override + CODEX_RULE_START)

    outside_suffix = b'\nForeign preference appended later.\n'
    override.write_bytes(before + outside_suffix)
    preview = plan('uninstall')
    override.write_bytes(before + outside_suffix.replace(b'later', b'today'))
    with pytest.raises(ConfigurationError, match='changed since preview'):
        apply_operation(preview)
    original = override.read_bytes()
    override.write_bytes(original.replace(b'cite_memory', b'user_modified'))
    with pytest.raises(ValueError, match='working-memory block was edited'):
        plan('uninstall')
    override.write_bytes(original)
    assert apply_operation(plan('uninstall'))['status'] == 'applied'
    assert override.read_bytes() == foreign_override + outside_suffix.replace(b'later', b'today')
    assert regular.read_bytes() == foreign_regular


def test_codex_rule_moves_from_created_default_file_to_new_override(plans):
    root = plans.home / '.codex'
    regular = root / 'AGENTS.md'
    override = root / 'AGENTS.override.md'
    def run(operation):
        plan = plans.create('codex', {'operation': operation, 'capabilities': ['continuation'],
                                      'surface': 'cli'}, 'http://127.0.0.1:8420')
        assert plans.apply('codex', plan['id'], plan['version'], True)['status'] == 'applied'
    run('install')
    assert regular.exists() and not override.exists()
    original = b'# New user override\n'
    override.write_bytes(original)
    run('update')
    assert not regular.exists()
    assert override.read_bytes().startswith(original)
    assert b'bootstrap_working_memory' in override.read_bytes()
    run('uninstall')
    assert override.read_bytes() == original


def test_stale_file_rejects_before_any_write(plans):
    plan = create(plans)
    mcp = plans.home/'.omp/agent/mcp.json'
    mcp.parent.mkdir(parents=True); mcp.write_text('{"user_setting": true}')
    with pytest.raises(ConfigurationError, match='changed since preview'):
        apply(plans, plan)
    assert mcp.read_text() == '{"user_setting": true}'
    assert not (plans.home/'.contextcore/credentials/omp.token').exists()


def test_host_binary_change_invalidates_plan(plans):
    binary = plans.home/'.local/bin/omp'
    binary.parent.mkdir(parents=True); binary.write_text('original'); binary.chmod(0o700)
    plan = create(plans)
    binary.write_text('new version')
    with pytest.raises(ConfigurationError, match='Host entry or version changed'): apply(plans, plan)
    assert not (plans.home/'.contextcore').exists()


def test_permission_change_invalidates_plan(plans):
    from app.services.connection_service import patch_connection
    plan = create(plans)
    patch_connection('omp', enabled=True, confirmed=True)
    with pytest.raises(ConfigurationError, match='permissions changed'): apply(plans, plan)
    assert not (plans.home/'.contextcore').exists()


def test_apply_requires_confirmation_correct_host_and_version(plans):
    plan = create(plans)
    with pytest.raises(ConfigurationError): plans.apply('omp', plan['id'], 1, False)
    with pytest.raises(ConfigurationError): plans.apply('cursor', plan['id'], 1, True)
    with pytest.raises(ConfigurationError): plans.apply('omp', plan['id'], 2, True)
    with pytest.raises(ConfigurationError): plans.transactions.apply(plan['id'], 1, True)
    assert not (plans.home/'.contextcore').exists()


def test_retry_reuses_same_applied_result_without_rotating_token(plans):
    plan = create(plans); apply(plans, plan)
    path = plans.home/'.contextcore/credentials/omp.token'; before = path.read_bytes()
    assert apply(plans, plan)['status'] == 'applied'
    assert path.read_bytes() == before


def test_partial_remove_preserves_other_grant_and_history_setting(plans):
    from app.services.connection_service import get_connection, patch_connection
    patch_connection('omp', enabled=True, confirmed=True)
    apply(plans, create(plans, ['context_tools', 'continuation']))
    apply(plans, create(plans, ['continuation'], 'uninstall'))
    state = get_connection('omp')
    assert state['enabled'] is True
    assert state['authorizations']['context_tools'] is True
    assert state['authorizations']['continuation'] is False
    assert (plans.home/'.contextcore/credentials/omp.token').exists()


def test_interruption_is_durable_and_recoverable_without_grant(plans, monkeypatch):
    from app.services.connection_service import get_connection
    plan = create(plans)
    original = plans.transactions._atomic
    count = 0
    def fail_second(path, data):
        nonlocal count
        count += 1
        if count == 2: raise OSError('synthetic disk error')
        return original(path, data)
    monkeypatch.setattr(plans.transactions, '_atomic', fail_second)
    with pytest.raises(ConfigurationError, match='recovery is required'): apply(plans, plan)
    assert plans.get('omp', plan['id'])['status'] == 'recovery_required'
    assert not get_connection('omp')['authorizations'].get('context_tools', False)
    monkeypatch.setattr(plans.transactions, '_atomic', original)
    assert plans.recover('omp', plan['id'], True)['status'] == 'recovered'
    assert not (plans.home/'.contextcore/credentials/omp.token').exists()
    assert not (plans.home/'.omp/agent/mcp.json').exists()


def test_recovery_refuses_new_external_edits(plans, monkeypatch):
    plan = create(plans)
    original = plans.transactions._atomic
    count = 0
    def fail_second(path, data):
        nonlocal count
        count += 1
        if count == 2: raise OSError('synthetic failure')
        return original(path, data)
    monkeypatch.setattr(plans.transactions, '_atomic', fail_second)
    with pytest.raises(ConfigurationError): apply(plans, plan)
    monkeypatch.setattr(plans.transactions, '_atomic', original)
    token = plans.home/'.contextcore/credentials/omp.token'; token.write_text('external new value')
    with pytest.raises(ConfigurationError, match='externally edited'): plans.recover('omp', plan['id'], True)
    assert token.read_text() == 'external new value'


@pytest.mark.parametrize('capabilities', [None, [], ['unknown'], ['context_tools', 'context_tools']])
def test_missing_or_ambiguous_capability_selection_rejected(plans, capabilities):
    with pytest.raises(ConfigurationError):
        plans.create('omp', {'operation':'install', 'surface':'cli', 'capabilities':capabilities}, 'http://127.0.0.1:8420')
    assert not (plans.home/'.contextcore').exists()


def test_pause_resume_scopes_permissions_without_changing_files(plans):
    from app.services.connection_service import get_connection, verify_agent_token
    apply(plans, create(plans, ['context_tools', 'lifecycle']))
    token = (plans.home/'.contextcore/credentials/omp.token').read_text().strip()
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in plans.home.rglob('*') if p.is_file()}
    apply(plans, create(plans, ['context_tools'], 'pause'))
    assert verify_agent_token(token) is None
    assert verify_agent_token(token, purpose='lifecycle') == 'omp'
    assert get_connection('omp')['enabled'] is False
    assert {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in plans.home.rglob('*') if p.is_file()} == before
    apply(plans, create(plans, ['context_tools'], 'resume'))
    assert verify_agent_token(token) == 'omp'
    assert verify_agent_token(token, purpose='lifecycle') == 'omp'
    assert {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in plans.home.rglob('*') if p.is_file()} == before


def test_http_plan_confirmation_readback_and_legacy_write_block(plans):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app, base_url='http://127.0.0.1:8420', client=('127.0.0.1', 50000))
    headers = {'X-CSRF-Token': client.get('/api/session').json()['csrf_token']}
    for operation in ('install', 'uninstall'):
        assert client.post(f'/api/connections/omp/{operation}', json={'confirmed': True}, headers=headers).status_code == 409
    body = {'operation':'install', 'capabilities':['context_tools'], 'surface':'cli'}
    preview = client.post('/api/connections/omp/plans', json=body, headers=headers)
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert not (plans.home/'.omp').exists()
    path = f"/api/connections/omp/plans/{plan['id']}"
    assert client.post(path+'/apply', json={'version':1}, headers=headers).status_code == 422
    applied = client.post(path+'/apply', json={'version':1, 'confirmed':True}, headers=headers)
    assert applied.status_code == 200, applied.text
    readback = client.get(path).json()
    assert readback['status'] == 'applied'
    assert readback['readback'] == 'verified'
    assert readback['host_reload'] == 'not_run'
    token = (plans.home/'.contextcore/credentials/omp.token').read_text().strip()
    assert token not in json.dumps(readback)
    assert client.get(path.replace('/omp/', '/cursor/')).status_code == 404
