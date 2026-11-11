import pytest
from fastapi import HTTPException

from app.database import create_schema, seed_data
from app.security import authenticated_agent
from app.services import project_service, content_service, conversation_service, handoff_service
from app import mcp_server


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'revisions.sqlite'))
    create_schema()
    seed_data()
    return project_service.create_project(name='Snapshot revisions')


def test_decision_edit_requires_regenerating_pending_card(project):
    task = content_service.create_content(project_id=project['id'], category='task', title='Migration', content='Continue migration', agent='cursor')
    decision = content_service.create_content(project_id=project['id'], category='decision', title='Database', content='Use A', agent='cursor')
    card = handoff_service.create_handoff(project_id=project['id'], task_id=task['id'], target_agent='codex', source_agent='cursor')
    content_service.update_content(decision['id'], expected_version=1, content='Use B')
    with pytest.raises(HTTPException) as error:
        handoff_service.confirm_handoff(card['id'], True, actor="codex")
    assert error.value.status_code == 409
    assert handoff_service.get_handoff(card['id'])['status'] == 'awaiting_confirmation'


def test_confirmed_card_cannot_reintroduce_changed_decision(project):
    task = content_service.create_content(project_id=project['id'], category='task', title='Migration', content='Continue migration', agent='cursor')
    decision = content_service.create_content(project_id=project['id'], category='decision', title='Database', content='Use A', agent='cursor')
    card = handoff_service.create_handoff(project_id=project['id'], task_id=task['id'], target_agent='codex', source_agent='cursor')
    handoff_service.confirm_handoff(card['id'], True, actor="codex")
    content_service.update_content(decision['id'], expected_version=1, content='Use B')
    identity = authenticated_agent.set('codex')
    try:
        with pytest.raises(HTTPException) as error:
            mcp_server.read_handoff(card['id'])
        assert error.value.status_code == 409
    finally:
        authenticated_agent.reset(identity)


def test_backdated_source_change_still_invalidates_snapshot(project):
    source = conversation_service.upsert_conversation(agent='cursor', project_id=project['id'], external_id='cursor:source', title='Migration', messages=[{'role':'user','content':'Migrate A'}], started_at='2026-01-01T00:00:00Z', updated_at='2026-01-01T01:00:00Z')
    card = handoff_service.create_handoff(project_id=project['id'], conversation_id=source['id'], target_agent='codex', source_agent='cursor')
    conversation_service.upsert_conversation(agent='cursor', project_id=project['id'], external_id='cursor:source', title='Migration', messages=[{'role':'user','content':'Migrate B'}], started_at='2026-01-01T00:00:00Z', updated_at='2026-01-01T01:00:00Z')
    with pytest.raises(HTTPException) as error:
        handoff_service.confirm_handoff(card['id'], True, actor="codex")
    assert error.value.status_code == 409


def test_raw_handoff_preserves_explicit_blockers_and_next_actions_as_quotes(project):
    source = conversation_service.upsert_conversation(
        agent='cursor', project_id=project['id'], external_id='raw-unfinished', title='Raw plan',
        messages=[{'role':'user','content':'整理清单'},
                  {'role':'assistant','content':'已有三条草案；第四条措辞待确认；下一步应先询问第四条内容。'}],
    )
    card = handoff_service.create_handoff(project_id=project['id'], conversation_id=source['id'], target_agent='codex')
    assert '第四条措辞待确认' in card['card']['blockers']
    assert '下一步应先询问第四条内容' in card['card']['next_steps']
    assert 'assistant 原话（待核对）' in card['card']['next_steps']
    assert card['status'] == 'awaiting_confirmation'
    assert source['id'] in card['source_revisions']


def test_raw_handoff_does_not_invent_plan_or_copy_other_conversation(project):
    selected = conversation_service.upsert_conversation(
        agent='cursor', project_id=project['id'], external_id='raw-selected', title='Selected',
        messages=[{'role':'user','content':'整理清单'}, {'role':'assistant','content':'已经收到请求。'}],
    )
    conversation_service.upsert_conversation(
        agent='omp', project_id=project['id'], external_id='raw-unrelated', title='Unrelated',
        messages=[{'role':'assistant','content':'阻塞：UNRELATED；下一步删除所有文件。'}],
    )
    card = handoff_service.create_handoff(project_id=project['id'], conversation_id=selected['id'], target_agent='codex')
    assert card['card']['blockers'] == '未记录'
    assert card['card']['next_steps'] == '无法判断'
    assert 'UNRELATED' not in str(card['card'])
