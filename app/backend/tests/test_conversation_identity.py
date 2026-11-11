from app.database import create_schema
from app.services import conversation_service


def test_distinct_threads_share_sqlite_source_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'identity.sqlite'))
    create_schema()
    source = str(tmp_path / 'state.vscdb')
    first = conversation_service.upsert_conversation(
        agent='cursor', external_id='cursor:first', source_path=source, title='First task',
        messages=[{'role':'user','content':'Keep task A separate'}],
    )
    second = conversation_service.upsert_conversation(
        agent='cursor', external_id='cursor:second', source_path=source, title='Second task',
        messages=[{'role':'user','content':'Keep task B separate'}],
    )
    assert first['id'] != second['id']
    assert conversation_service.get_conversation(first['id'])['messages'][0]['content'] == 'Keep task A separate'
    repeated = conversation_service.upsert_conversation(
        agent='cursor', external_id='cursor:second', source_path=source, title='Second task',
        messages=[{'role':'user','content':'Keep task B separate'}],
    )
    assert repeated['id'] == second['id']
    assert repeated['_changed'] is False
    assert len(conversation_service.list_conversations(agent='cursor')) == 2


def test_shared_db_delete_one_session_never_blocks_siblings_or_new_sessions(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'shared_db.sqlite'))
    create_schema()
    source = str(tmp_path / 'state.vscdb')

    # Create two sibling sessions in Cursor shared database
    first = conversation_service.upsert_conversation(
        agent='cursor', external_id='cursor:first', source_path=source, title='First task',
        messages=[{'role': 'user', 'content': 'Keep task A separate'}],
    )
    second = conversation_service.upsert_conversation(
        agent='cursor', external_id='cursor:second', source_path=source, title='Second task',
        messages=[{'role': 'user', 'content': 'Keep task B separate'}],
    )
    assert first['id'] != second['id']

    # Deleting the first session must succeed
    del_res = conversation_service.delete_conversation(first['id'])
    assert del_res == {"ok": True}

    # Sibling session (second) must NOT be blocked and can be updated
    updated_second = conversation_service.upsert_conversation(
        agent='cursor', external_id='cursor:second', source_path=source, title='Second task updated',
        messages=[{'role': 'user', 'content': 'Keep task B separate and updated'}],
    )
    assert updated_second['id'] == second['id']

    # Brand new session (third) in the same shared DB must NOT be blocked
    third = conversation_service.upsert_conversation(
        agent='cursor', external_id='cursor:third', source_path=source, title='Third task',
        messages=[{'role': 'user', 'content': 'Task C'}],
    )
    assert third['id'] is not None

    # Attempting to resurrect the deleted exact session (first) MUST fail with 410
    with pytest.raises(HTTPException) as exc:
        conversation_service.upsert_conversation(
            agent='cursor', external_id='cursor:first', source_path=source, title='First task',
            messages=[{'role': 'user', 'content': 'Try resurrect'}],
        )
    assert exc.value.status_code == 410


def test_legacy_shared_db_tombstone_migration_semantics(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException
    from app.database import db, now_iso, new_id

    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'migration.sqlite'))
    create_schema()
    source = str(tmp_path / 'state.vscdb')

    # Manually inject legacy bug state: both exact session tombstone and bare shared DB tombstone
    privacy_key = conversation_service.compute_source_identity_key('cursor', source, 'Task A', 'cursor:deleted_session')
    overbroad_shared_key = conversation_service.compute_source_identity_key('cursor', source, 'Task A', None)

    now = now_iso()
    with db() as conn:
        conn.execute(
            "INSERT INTO tombstones (id, entity_type, dedupe_key, deleted_at, deleted_by, reason) VALUES (?, 'conversation', ?, ?, 'user', 'user_deleted')",
            (new_id("tomb"), privacy_key, now),
        )
        conn.execute(
            "INSERT INTO tombstones (id, entity_type, dedupe_key, deleted_at, deleted_by, reason) VALUES (?, 'conversation', ?, ?, 'user', 'user_deleted')",
            (new_id("tomb"), overbroad_shared_key, now),
        )

    # Upserting a sibling session must migrate/clean the overbroad shared DB tombstone without blocking
    sibling = conversation_service.upsert_conversation(
        agent='cursor', external_id='cursor:sibling_session', source_path=source, title='Sibling task',
        messages=[{'role': 'user', 'content': 'Sibling works'}],
    )
    assert sibling['id'] is not None

    # The privacy tombstone for the exact deleted session MUST remain intact and prevent resurrection
    with pytest.raises(HTTPException) as exc:
        conversation_service.upsert_conversation(
            agent='cursor', external_id='cursor:deleted_session', source_path=source, title='Task A',
            messages=[{'role': 'user', 'content': 'Resurrect attempt'}],
        )
    assert exc.value.status_code == 410


def test_conversation_project_validation_raises_404(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException

    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'proj_val.sqlite'))
    create_schema()

    with pytest.raises(HTTPException) as exc:
        conversation_service.upsert_conversation(
            agent='omp', title='Session Bogus', project_id='proj_nonexistent'
        )
    assert exc.value.status_code == 404
    assert "Target project 'proj_nonexistent' not found" in exc.value.detail


def test_conversation_like_escaping_special_chars(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'like_escape.sqlite'))
    create_schema()

    conversation_service.upsert_conversation(
        agent='omp', title='Discount 50% promo', external_id='omp:pct',
        messages=[{'role': 'user', 'content': '50% off everything'}],
    )
    conversation_service.upsert_conversation(
        agent='omp', title='Discount 500 promo', external_id='omp:num',
        messages=[{'role': 'user', 'content': '500 off everything'}],
    )

    # Searching for literal '%' should ONLY match 'Discount 50% promo', not 'Discount 500 promo'
    matches = conversation_service.list_conversations(query='%')
    assert len(matches) == 1
    assert matches[0]['external_id'] == 'omp:pct'
