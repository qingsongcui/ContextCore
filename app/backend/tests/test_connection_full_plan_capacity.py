"""Every supported component combination fits while invalid plans stay rejected."""
from itertools import combinations
from pathlib import Path
import pytest
from app.database import create_schema, seed_data
from app.integrations.constants import SUPPORTED_HOSTS, MAX_CONNECTION_FILES
from app.integrations.config_transactions import ConfigurationError
from app.services.connection_plans import ConnectionPlans, CAPABILITIES
from app.services.host_registry import HOST_DEFINITIONS


@pytest.mark.parametrize('host', SUPPORTED_HOSTS)
def test_all_component_combinations_prepare_and_full_plan_installs_and_uninstalls(host):
    create_schema(); seed_data()
    plans = ConnectionPlans(home=Path.home())
    surface = next(iter(HOST_DEFINITIONS[host].surfaces))
    for size in (1, 2, 3):
        for selected in combinations(CAPABILITIES, size):
            plan = plans.create(host, {'operation': 'install', 'surface': surface, 'capabilities': list(selected)}, 'http://127.0.0.1:18420')
            assert len(plan['payload']['files']) <= MAX_CONNECTION_FILES
            assert plan['status'] == 'planned'
    assert not (plans.home/'.contextcore').exists()
    if host in ('codex', 'cursor', 'antigravity', 'claude-code'):
        assert len(plan['payload']['files']) == 6
    result = plans.apply(host, plan['id'], plan['version'], True)
    assert result['status'] == 'applied'
    assert result['payload']['readback'] == 'verified'
    remove = plans.create(host, {'operation': 'uninstall', 'surface': surface, 'capabilities': list(CAPABILITIES)}, 'http://127.0.0.1:18420')
    result = plans.apply(host, remove['id'], remove['version'], True)
    assert result['status'] == 'applied'
    with plans.transactions.connect() as conn:
        state = plans._state(host, conn)
        assert not state['token_hash']


def test_capacity_does_not_allow_duplicates_escape_or_extra_files():
    create_schema(); seed_data()
    plans = ConnectionPlans(home=Path.home())
    meta = {'namespace': 'connections'}
    for paths in ([str(Path.home()/'a')] * 2, [str(Path.home()/'a'), str(Path.home()) + '/./a'], [str(Path.home()/'a'), str(Path.home()) + '//a'], ['/outside-home'], [str(Path.home()/str(i)) for i in range(7)]):
        with pytest.raises(ConfigurationError):
            plans.transactions.create_deferred(paths, meta)


@pytest.mark.parametrize('host', SUPPORTED_HOSTS)
@pytest.mark.parametrize('order', list(__import__('itertools').permutations(CAPABILITIES)))
def test_each_host_incremental_capability_order_preserves_unselected_files_and_grants(host, order):
    from app.services.connection_service import get_connection
    create_schema(); seed_data()
    plans = ConnectionPlans(home=Path.home())
    surface = next(iter(HOST_DEFINITIONS[host].surfaces))
    installed = set()
    for operation, sequence in [('install', order), ('uninstall', tuple(reversed(order)))]:
        for capability in sequence:
            before = {str(p): p.read_bytes() for p in plans.home.rglob('*') if p.is_file()}
            plan = plans.create(host, {'operation': operation, 'surface': surface, 'capabilities': [capability]}, 'http://127.0.0.1:18420')
            assert {str(p): p.read_bytes() for p in plans.home.rglob('*') if p.is_file()} == before
            targets = {item['path'] for item in plan['payload']['files']}
            receipt = plans.apply(host, plan['id'], plan['version'], True)
            assert receipt['status'] == 'applied' and receipt['payload']['readback'] == 'verified'
            for path, content in before.items():
                if path not in targets:
                    assert Path(path).read_bytes() == content
            if operation == 'install':
                installed.add(capability)
            else:
                installed.remove(capability)
            state = get_connection(host)
            assert state['enabled'] is False
            for key in CAPABILITIES:
                assert bool(state['authorizations'].get(key)) == (key in installed)
    with plans.transactions.connect() as conn:
        assert not plans._state(host, conn)['token_hash']


@pytest.mark.parametrize('host', SUPPORTED_HOSTS)
def test_component_lifecycle_update_pause_resume_conflict_and_restart_recovery(host, monkeypatch):
    from app.services.connection_service import get_connection
    create_schema(); seed_data()
    plans = ConnectionPlans(home=Path.home())
    surface = next(iter(HOST_DEFINITIONS[host].surfaces))
    def prepare(operation, selected=None):
        return plans.create(host, {'operation': operation, 'surface': surface, 'capabilities': selected or list(CAPABILITIES)}, 'http://127.0.0.1:18420')
    def apply(plan):
        return plans.apply(host, plan['id'], plan['version'], True)
    interrupted = prepare('install')
    atomic = plans.transactions._atomic
    calls = []
    def fail_second(path, content):
        calls.append(str(path))
        if len(calls) == 2:
            raise OSError('synthetic partial write')
        atomic(path, content)
    monkeypatch.setattr(plans.transactions, '_atomic', fail_second)
    with pytest.raises(ConfigurationError, match='recovery'):
        apply(interrupted)
    assert plans.get(host, interrupted['id'])['status'] == 'recovery_required'
    assert not any(get_connection(host)['authorizations'].get(c) for c in CAPABILITIES)
    # Reconstruct service: recovery cannot rely on in-memory staged state.
    plans = ConnectionPlans(home=Path.home())
    assert plans.recover(host, interrupted['id'], True)['status'] == 'recovered'
    assert apply(prepare('install'))['status'] == 'applied'
    for capability in CAPABILITIES:
        assert apply(prepare('update', [capability]))['payload']['readback'] == 'verified'
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in plans.home.rglob('*') if p.is_file()}
        assert apply(prepare('pause', [capability]))['status'] == 'applied'
        grants = get_connection(host)['authorizations']
        assert not grants[capability] and all(grants[c] for c in CAPABILITIES if c != capability)
        assert apply(prepare('resume', [capability]))['status'] == 'applied'
        assert {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in plans.home.rglob('*') if p.is_file()} == before
    conflict = prepare('update')
    target = Path(conflict['payload']['files'][0]['path'])
    original = target.read_bytes()
    target.write_bytes(original + b'\nsynthetic external change\n')
    with pytest.raises(ConfigurationError):
        apply(conflict)
    assert target.read_bytes().endswith(b'synthetic external change\n')
    target.write_bytes(original)
    assert apply(prepare('uninstall'))['status'] == 'applied'
    assert not any(get_connection(host)['authorizations'].get(c) for c in CAPABILITIES)
