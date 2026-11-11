from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException

from app.database import create_schema, db
from app.services import conflict_service, content_service, handoff_service, project_service


@pytest.fixture
def assets(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'isolated.db'))
    create_schema()
    project = project_service.create_project(name='Atomic conflict')
    existing = content_service.create_content(title='Framework', content='Use A', project_id=project['id'], category='decision')
    conflict = conflict_service.create_conflict(project['id'], existing['id'], {'title': 'Framework', 'content': 'Use B', 'category': 'decision'}, 'Alternative')
    return project, existing, conflict


def test_parallel_both_resolution_creates_one_alternative(assets):
    project, existing, conflict = assets
    start = Barrier(2)

    def resolve():
        start.wait()
        return conflict_service.resolve_conflict(conflict['id'], 'both', expected_version=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: resolve(), range(2)))
    assert all(item['resolution'] == 'both' for item in outcomes)
    alternatives = [item for item in content_service.list_content(project_id=project['id']) if item['content'] == 'Use B']
    assert len(alternatives) == 1
    assert content_service.get_content(existing['id'])['content'] == 'Use A'


def test_resolution_write_and_status_roll_back_together(assets, monkeypatch):
    _, existing, conflict = assets
    real_update = content_service.update_content

    def interrupted(*args, **kwargs):
        real_update(*args, **kwargs)
        raise RuntimeError('interrupted after content write')

    monkeypatch.setattr(conflict_service, 'update_content', interrupted)
    with pytest.raises(RuntimeError):
        conflict_service.resolve_conflict(conflict['id'], 'replace', expected_version=1)
    assert content_service.get_content(existing['id'])['content'] == 'Use A'
    assert content_service.get_content(existing['id'])['version'] == 1
    assert conflict_service.get_conflict(conflict['id'])['status'] == 'pending'


def test_conflicting_repeated_resolution_is_rejected(assets):
    _, _, conflict = assets
    conflict_service.resolve_conflict(conflict['id'], 'keep')
    with pytest.raises(HTTPException) as error:
        conflict_service.resolve_conflict(conflict['id'], 'replace')
    assert error.value.status_code == 409


@pytest.mark.parametrize('batch', [False, True])
def test_deleted_memory_is_scrubbed_from_handoff_copies(assets, batch):
    project, existing, _ = assets
    handoff = handoff_service.create_handoff(project_id=project['id'], target_agent='codex')
    assert 'Use A' in handoff['card']['decisions']
    if batch:
        content_service.batch_content([existing['id']], 'delete')
    else:
        content_service.delete_content(existing['id'])
    remaining = handoff_service.get_handoff(handoff['id'])
    assert remaining['status'] == 'cancelled'
    assert all('Use A' not in text for text in remaining['card'].values())
    with pytest.raises(HTTPException):
        handoff_service.confirm_handoff(handoff['id'], confirmed=True, actor="codex")
    with db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM content_versions WHERE content_id = ?', (existing['id'],)).fetchone()[0] == 0


def test_recategorization_cannot_bypass_deleted_content_guard(assets):
    project, _, _ = assets
    original = content_service.create_content(title='Deleted', content='Original body', project_id=project['id'])
    content_service.delete_content(original['id'])
    alternate = content_service.create_content(title='Deleted', content='Original body', project_id=project['id'], category='lesson')
    with pytest.raises(HTTPException) as error:
        content_service.batch_content([alternate['id']], 'recategorize', category='knowledge')
    assert error.value.status_code == 410
    remaining = content_service.get_content(alternate['id'])
    assert remaining['category'] == 'lesson'
    assert remaining['version'] == 1
