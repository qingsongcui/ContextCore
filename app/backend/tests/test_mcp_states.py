import pytest
from fastapi import HTTPException

from app import mcp_server
from app.database import create_schema
from app.security import authenticated_agent
from app.services import content_service, handoff_service, project_service


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'mcp.db'))
    create_schema()
    return project_service.create_project(name='MCP state transitions')


@pytest.fixture
def omp_identity():
    identity = authenticated_agent.set('omp')
    yield
    authenticated_agent.reset(identity)


def test_task_progress_preserves_tags_and_rejects_lost_updates(project, omp_identity):
    task = content_service.create_content(title='Build', content='Pending', category='task', project_id=project['id'], tags=['area:backend', 'status:pending'])
    result = mcp_server.update_task_progress(task['id'], 'in_progress', expected_version=1, detail='Implementation started')
    stored = content_service.get_content(task['id'])
    assert result['version'] == 2
    assert stored['tags'] == ['area:backend', 'status:in_progress']
    assert stored['content'] == 'Implementation started'
    assert stored['agent'] == 'omp'
    with pytest.raises(HTTPException) as error:
        mcp_server.update_task_progress(task['id'], 'completed', expected_version=1)
    assert error.value.status_code == 409
    assert content_service.get_content(task['id'])['content'] == 'Implementation started'


def test_non_task_cannot_be_updated_as_task(project, omp_identity):
    decision = content_service.create_content(title='Architecture', category='decision', project_id=project['id'])
    with pytest.raises(ValueError):
        mcp_server.update_task_progress(decision['id'], 'completed', expected_version=1)
    assert content_service.get_content(decision['id'])['category'] == 'decision'


def test_agent_edit_is_visible_with_version_and_preserved_history(project, omp_identity):
    memory = content_service.create_content(title='Working context', content='Before', project_id=project['id'])
    edited = mcp_server.edit_memory(memory['id'], 1, content='After')
    viewed = mcp_server.read_memory(memory['id'])
    assert viewed['content'] == 'After'
    assert edited['version'] == 2
    assert viewed['versions'][0]['content'] in {'Before', 'After'}
    assert {v['content'] for v in viewed['versions']} == {'Before', 'After'}


def test_handoff_read_requires_confirmation_and_correct_recipient(project):
    memory = content_service.create_content(title='Build', content='Actual current task', category='task', agent='omp', project_id=project['id'])
    handoff = handoff_service.create_handoff(project_id=project['id'], target_agent='codex', source_agent='omp')
    identity = authenticated_agent.set('codex')
    try:
        unconfirmed = mcp_server.read_handoff(handoff['id'])
        assert unconfirmed['status'] == 'awaiting_confirmation'
        assert unconfirmed['must_not_execute'] is True
        assert unconfirmed['card'] == handoff['card']
        assert mcp_server.confirm_handoff(handoff['id'])['ok'] is False
        handoff_service.confirm_handoff(handoff['id'], True, actor="codex")
        assert mcp_server.read_handoff(handoff['id'])['status'] == 'confirmed'
        content_service.delete_content(memory['id'])
        with pytest.raises(ValueError):
            mcp_server.read_handoff(handoff['id'])
    finally:
        authenticated_agent.reset(identity)
    identity = authenticated_agent.set('cursor')
    try:
        with pytest.raises(ValueError):
            mcp_server.read_handoff(handoff['id'])
    finally:
        authenticated_agent.reset(identity)


def test_pending_conflict_is_a_live_handoff_source(project):
    from app.services import conflict_service
    decision = content_service.create_content(
        title='Architecture', content='A', category='decision', project_id=project['id'],
    )
    conflict_service.create_conflict(
        project['id'], decision['id'],
        {'title': 'Architecture', 'content': 'B', 'category': 'decision'},
        'Choose architecture before implementation',
    )
    card = handoff_service.create_handoff(project_id=project['id'], target_agent='codex')
    confirmed = handoff_service.confirm_handoff(card['id'], True, actor="codex")
    assert confirmed['status'] == 'confirmed'
    assert confirmed['card']['blockers'] == 'Choose architecture before implementation'
