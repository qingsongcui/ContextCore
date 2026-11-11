from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from app.database import create_schema, db
from app.services import conversation_service as conversations, project_service as projects, content_service as content, retrieval_service as retrieval


@pytest.fixture
def store():
    create_schema()
    source = projects.create_project('Source')
    target = projects.create_project('Target')
    item = content.create_content(title='Release constraints', content='Preserve approved interface', category='background', project_id=source['id'])
    return source, target, item


@pytest.mark.parametrize('operation', ['merge', 'split'])
def test_project_move_invalidates_prepared_context_and_old_editor_revision(store, operation):
    source, target, item = store
    pack = retrieval.assemble_context('Release constraints', project_id=source['id'], consumer_id='codex')
    assert item['id'] in {x['id'] for x in pack['entries']}
    if operation == 'merge':
        destination = projects.merge_projects([source['id']], target['id'])
    else:
        destination = projects.split_project(source['id'], 'Split', content_ids=[item['id']])
    with pytest.raises(HTTPException) as stale_pack:
        retrieval.record_injection(pack['id'], 'codex')
    assert stale_pack.value.status_code in (409, 410)
    with pytest.raises(HTTPException) as stale_edit:
        content.update_content(item['id'], title='Stale edit', project_id=source['id'], expected_version=item['version'])
    assert stale_edit.value.status_code == 409
    moved = content.get_content(item['id'])
    assert moved['project_id'] == destination['id']
    assert moved['version'] == item['version'] + 1
    assert moved['content'] == item['content']
    assert [v['version'] for v in moved['versions']] == [1, 2]
    fresh = retrieval.assemble_context('Release constraints', project_id=destination['id'], consumer_id='codex')
    assert retrieval.record_injection(fresh['id'], 'codex')['status'] == 'emitted'


def test_duplicate_split_selection_records_only_one_revision(store):
    source, _, item = store
    moved = projects.split_project(source['id'], 'Split', content_ids=[item['id'], item['id']])
    current = content.get_content(item['id'])
    assert current['project_id'] == moved['id']
    assert current['version'] == 2
    assert len(current['versions']) == 2


def test_merge_tombstone_refusal_rolls_back_prior_moves_and_revisions(store, monkeypatch):
    source, target, item = store
    deleted = content.create_content(title='Forbidden import', content='Deleted at destination', project_id=target['id'])
    content.delete_content(deleted['id'])
    # Fix iteration order without changing foreign-key identities after creation.
    original_id = content.new_id
    monkeypatch.setattr(content, 'new_id', lambda prefix: 'zz-forbidden' if prefix == 'cnt' else original_id(prefix))
    content.create_content(title='Forbidden import', content='Deleted at destination', project_id=source['id'])
    with pytest.raises(HTTPException) as error:
        projects.merge_projects([source['id']], target['id'])
    assert error.value.status_code == 410
    current = content.get_content(item['id'])
    assert current['project_id'] == source['id'] and current['version'] == 1
    assert len(current['versions']) == 1
    assert projects.get_project(source['id'])['status'] == 'active'


def test_large_project_merge_pages_ids_without_collecting_all_bodies(store):
    import tracemalloc
    source, target, _ = store
    for i in range(130):
        content.create_content(title=f'Large document {i}', content='x' * 65536, project_id=source['id'])
    tracemalloc.start()
    try:
        projects.merge_projects([source['id']], target['id'])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3 * 1024 * 1024, peak
    with db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM contents WHERE project_id=?', (source['id'],)).fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM contents WHERE project_id=? AND version=2', (target['id'],)).fetchone()[0] == 131
        assert conn.execute('SELECT COUNT(*) FROM content_versions WHERE version=2').fetchone()[0] == 131


def test_split_moves_conversation_ids_without_selecting_transcripts(store, monkeypatch):
    source, _, _ = store
    secret = "SECRET_TRANSCRIPT_BODY" * 200
    chat = conversations.create_conversation(
        agent="codex",
        title="Long chat",
        project_id=source["id"],
        messages=[{"role": "user", "content": secret, "timestamp": "2026-10-24T00:00:00Z"}],
    )
    traces: list[str] = []
    original_db = projects.db

    @contextmanager
    def traced():
        with original_db() as conn:
            conn.set_trace_callback(traces.append)
            yield conn

    monkeypatch.setattr(projects, "db", traced)
    moved = projects.split_project(source["id"], "Out", conversation_ids=[chat["id"]])
    joined = "\n".join(traces)
    assert secret not in joined
    selects = [sql for sql in traces if sql.lstrip().upper().startswith("SELECT") and "CONVERSATIONS" in sql.upper()]
    assert selects
    assert all("MESSAGES" not in sql.upper() for sql in selects)
    assert conversations.get_conversation(chat["id"], include_messages=False)["project_id"] == moved["id"]
