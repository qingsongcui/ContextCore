import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.integrations.config_transactions import ConfigurationError
from app.routers import manage
from app.services import management_service, shared_batches
from tests.test_management import manager, mcp


def target(host, *assets):
    return {'host_id': host, 'asset_versions': {asset['id']: asset['version'] for asset in assets}}


def apply(manager, batch, **kwargs):
    return shared_batches.update(manager, batch['id'], {'confirmed': True, 'expected_version': batch['version'], **kwargs}, 'apply')


def retry(manager, batch, *hosts):
    return shared_batches.update(manager, batch['id'], {'expected_version': batch['version'], 'host_ids': list(hosts)}, 'repreview')


def test_http_bulk_write_restart_and_uninstall(manager, monkeypatch):
    app = FastAPI()
    app.include_router(manage.router)
    monkeypatch.setattr(management_service, 'service', lambda: manager)
    client = TestClient(app)
    assets = [mcp(manager), manager.save('asset', {'kind': 'skill', 'name': 'Team review', 'files': {'SKILL.md': '# Team review'}}),
              manager.save('asset', {'kind': 'prompt', 'name': 'Check', 'content': 'Check citations.'})]
    targets = [target(host, *assets) for host in ('codex', 'claude-code')]
    api = '/api/manage/shared-batches'
    for operation, expected in [('install', 'verified'), ('uninstall', 'removed')]:
        response = client.post(api, json={'operation': operation, 'targets': targets})
        assert response.status_code == 200, response.text
        batch = response.json()
        assert batch['status'] == 'planned' and batch['counts']['planned'] == 2, batch
        assert all(row['files'] and not row['error_code'] and row['asset_names'] for row in batch['targets'])
        path = api + '/' + batch['id']
        assert client.post(path + '/apply', json={'expected_version': 1}).status_code == 422
        result = client.post(path + '/apply', json={'expected_version': 1, 'confirmed': True}).json()
        assert result['status'] == 'applied' and result['counts']['applied'] == 2
        assert all(row['file_readback'] == 'verified' and row['host_reload'] == 'not_run' for row in result['targets'])
        for host in ('codex', 'claude-code'):
            assert all(manager.host_state(host)['resources'][asset['id']]['status'] == expected for asset in assets)
        restarted = management_service.ManagementService(manager.db_path, manager.home, manager.vault)
        assert shared_batches.detail(restarted, batch['id']) == result
        assert client.post(path + '/apply', json={'expected_version': 1, 'confirmed': True}).status_code == 409
    page = client.get(api, params={'limit': 1}).json()
    assert page['total'] == 2 and page['items'][0]['id'] == batch['id'] and 'targets' not in page['items'][0]


def test_partial_failure_only_retries_reviewed_target(manager):
    asset = mcp(manager)
    batch = shared_batches.create(manager, {'operation':'install','targets':[target(h, asset) for h in ('codex', 'claude-code')]})
    path = Path(batch['targets'][0]['files'][0]['path'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('unmanaged = true\n')
    partial = apply(manager, batch)
    assert partial['status'] == 'partial'
    failed, succeeded = partial['targets']
    assert failed['status'] == 'failed' and succeeded['status'] == 'applied'
    preserved = Path(succeeded['files'][0]['path'])
    stamp = preserved.stat().st_mtime_ns
    with pytest.raises(ConfigurationError):
        retry(manager, partial, 'claude-code')
    reviewed = retry(manager, partial, 'codex')
    assert reviewed['targets'][0]['plan_id'] != failed['plan_id'] and reviewed['version'] > partial['version']
    with pytest.raises(ConfigurationError, match='refresh'):
        apply(manager, partial)
    result = apply(manager, reviewed, host_ids=['codex'])
    assert result['status'] == 'applied' and result['targets'][1]['plan_id'] == succeeded['plan_id']
    assert preserved.stat().st_mtime_ns == stamp and 'unmanaged' in path.read_text()


def test_stale_assets_preview_and_shared_paths(manager):
    asset = mcp(manager)
    old = target('opencode', asset)
    current = manager.save('asset', {'name':'Updated definition'}, asset['id'], asset['version'])
    batch = shared_batches.create(manager, {'operation':'install','targets':[
        target('codex', current), target('claude-code', current), old]})
    assert [(r['status'],r['error_code']) for r in batch['targets']] == [
        ('planned',None),('planned',None),('failed','stale_asset')]
    updated = retry(manager, batch, 'opencode')
    assert updated['targets'][2]['asset_versions'] == {asset['id']: current['version']}
    result = apply(manager, updated)
    assert result['counts']['applied'] == 3 and result['counts']['failed'] == 0
    assert result['targets'][1]['plan_id'] is not None
    assert 'specs' not in json.dumps(result) and 'before_hash' not in json.dumps(result)


def test_concurrent_confirmations_and_interrupted_configuration(manager, monkeypatch):
    asset = mcp(manager)
    batch = shared_batches.create(manager, {'operation':'install','targets':[target('codex', asset)]})
    def confirm():
        try:
            return apply(manager, batch)['status']
        except ConfigurationError as error:
            return error.status_code
    with ThreadPoolExecutor(2) as pool:
        outcomes = [job.result() for job in [pool.submit(confirm) for _ in range(2)]]
    assert sorted(map(str,outcomes)) == ['409','applied']

    plan = shared_batches.create(manager, {'operation':'install','targets':[target('claude-code', asset)]})
    original = manager.transactions._atomic
    def fail(*args):
        raise OSError('sensitive test diagnostic')
    monkeypatch.setattr(manager.transactions, '_atomic', fail)
    interrupted = apply(manager, plan)
    row = interrupted['targets'][0]
    assert row['status'] == 'recovery_required' and not row['can_repreview'] and not row['can_apply']
    assert 'sensitive test diagnostic' not in json.dumps(interrupted)
    monkeypatch.setattr(manager.transactions, '_atomic', original)
    manager.recover_plan(row['plan_id'], True)
    after = shared_batches.detail(manager, plan['id'])
    assert after['targets'][0]['can_repreview']
    assert apply(manager, retry(manager, after, 'claude-code'))['status'] == 'applied'


def test_same_skill_in_one_shared_directory_cannot_be_written_as_two_folders(manager):
    skill = manager.save('asset', {'name':'Research', 'kind':'skill', 'files':{'SKILL.md':'# Research'}})
    batch = shared_batches.create(manager, {'operation':'install','targets':[
        target('codex', skill), target('omp', skill), target('claude-code', skill)]})
    assert [row['error_code'] for row in batch['targets']] == [None,None,None]
    assert apply(manager, batch)['counts']['applied'] == 3
    assert len(list((manager.home / '.codex/skills').glob('*/SKILL.md'))) == 1


def _run_batch_in_process(home, database, batch, result, entered=None, resume=None):
    from tests.test_management import FakeVault
    manager = management_service.ManagementService(database, home, FakeVault())
    if entered is not None:
        atomic = manager.transactions._atomic

        def paused_write(path, data):
            # Pause after the last hash check, before replacing the file.
            entered.set()
            if not resume.wait(15):
                raise TimeoutError("Timed out waiting to finish first write")
            return atomic(path, data)

        manager.transactions._atomic = paused_write
    result.put(apply(manager, batch))


def test_independent_databases_cannot_overwrite_a_confirmed_host_write(tmp_path):
    import multiprocessing
    import tomllib
    from tests.test_management import FakeVault

    context = multiprocessing.get_context('spawn')
    home = tmp_path.resolve() / 'shared-host-home'
    home.mkdir()
    managers = [
        management_service.ManagementService(tmp_path / name / 'test.db', home, FakeVault())
        for name in ('daily', 'alternate')
    ]
    first_asset, second_asset = [mcp(manager, name) for manager, name in zip(managers, ('First', 'Second'))]
    batches = [shared_batches.create(manager, {'operation': 'install', 'targets': [target('codex', asset)]})
               for manager, asset in zip(managers, (first_asset, second_asset))]
    assert all(batch['status'] == 'planned' for batch in batches)

    entered, resume = context.Event(), context.Event()
    results = [context.Queue(), context.Queue()]
    first = context.Process(target=_run_batch_in_process,
                            args=(home, managers[0].db_path, batches[0], results[0], entered, resume))
    second = context.Process(target=_run_batch_in_process,
                             args=(home, managers[1].db_path, batches[1], results[1]))
    try:
        first.start()
        assert entered.wait(10), 'First service did not reach its write'
        second.start()
        conflict = results[1].get(timeout=10)
        resume.set()
        applied = results[0].get(timeout=10)
    finally:
        resume.set()
        for process in (first, second):
            if process.pid is not None:
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join(5)
        for queue in results:
            queue.close()
            queue.join_thread()

    assert first.exitcode == second.exitcode == 0
    assert applied['targets'][0]['status'] == 'applied'
    assert conflict['targets'][0]['status'] == 'failed'
    assert conflict['targets'][0]['error_code'] == 'apply_conflict'
    replay = apply(managers[1], retry(managers[1], conflict, 'codex'))
    assert replay['targets'][0]['status'] == 'applied'
    config = tomllib.loads((home / '.codex' / 'config.toml').read_text())
    assert set(config['mcp_servers']) == {asset['id'].replace('asset_', 'senrelay_') for asset in (first_asset, second_asset)}
