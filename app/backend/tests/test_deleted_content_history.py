import pytest
from fastapi import HTTPException
from app.database import create_schema, db
from app.services import content_service as content, project_service as projects


@pytest.fixture
def store():
    create_schema()
    return projects.create_project('Original'), projects.create_project('Destination')


@pytest.mark.parametrize('batch', [False, True])
def test_deleted_edited_document_cannot_be_reimported_as_old_body(store, batch):
    source, _ = store
    original = dict(title='source.txt', content='original imported document', project_id=source['id'], kind='document')
    item = content.create_content(**original)
    current = content.update_content(item['id'], content='edited document', expected_version=1)
    if batch:
        content.batch_content([item['id']], 'delete', expected_versions={item['id']: current['version']})
    else:
        content.delete_content(item['id'], expected_version=current['version'])
    with pytest.raises(HTTPException) as error:
        content.create_content(**original)
    assert error.value.status_code == 410
    with db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM content_versions WHERE content_id=?', (item['id'],)).fetchone()[0] == 0


@pytest.mark.parametrize('batch', [False, True])
def test_deleted_moved_and_recategorized_memory_blocks_original_identity(store, batch):
    source, target = store
    original = dict(title='Old constraint', content='Keep this requirement', project_id=source['id'], category='background')
    item = content.create_content(**original)
    content.update_content(item['id'], category='decision', expected_version=1)
    projects.merge_projects([source['id']], target['id'])
    if batch:
        content.batch_content([item['id']], 'delete', expected_versions={item['id']:3})
    else:
        content.delete_content(item['id'], expected_version=3)
    with pytest.raises(HTTPException) as error:
        content.create_content(**original)
    assert error.value.status_code == 410


def test_legacy_body_versions_survive_schema_upgrade_as_deletion_identities(store):
    source, _ = store
    original = dict(title='legacy.txt', content='legacy original', project_id=source['id'], kind='document')
    item = content.create_content(**original)
    content.update_content(item['id'], content='legacy edited', expected_version=1)
    with db() as conn:
        conn.execute('DROP TRIGGER content_identity_insert')
        conn.execute('DROP TRIGGER content_identity_update')
        conn.execute('DROP TABLE content_identity_history')
    create_schema()
    create_schema()  # Upgrade is repeatable.
    content.delete_content(item['id'], expected_version=2)
    with pytest.raises(HTTPException) as error:
        content.create_content(**original)
    assert error.value.status_code == 410


@pytest.mark.parametrize('delete_kind', ['conversation', 'memory'])
def test_independent_conversation_memory_deletion_preserves_survivor(store, delete_kind):
    from app.services import conversation_service as conversations
    source, _ = store
    chat = conversations.upsert_conversation(agent='codex', title='Independent source', external_id='synthetic-independent', project_id=source['id'], messages=[{'role':'user','content':'Remember my requirement'}])
    memory = content.create_content(title='Surviving requirement', content='Keep the approved interface', project_id=source['id'], source_id=chat['id'], source_kind='conversation')
    if delete_kind == 'conversation':
        conversations.delete_conversation(chat['id'])
        surviving = content.get_content(memory['id'])
        assert surviving['content'] == memory['content'] and surviving['sources'][0]['deleted']
        with pytest.raises(HTTPException) as error:
            conversations.upsert_conversation(agent='codex', title='Changed title', external_id='synthetic-independent', project_id=source['id'])
        assert error.value.status_code == 410
    else:
        content.delete_content(memory['id'], expected_version=1)
        assert conversations.get_conversation(chat['id'])['messages'][0]['content'] == 'Remember my requirement'


def test_failed_batch_deletion_leaves_no_partial_tombstones(store):
    source, _ = store
    item = content.create_content(title='Keep live', content='Original', project_id=source['id'])
    content.update_content(item['id'], content='Updated', expected_version=1)
    other = content.create_content(title='Other', content='Other body', project_id=source['id'])
    with pytest.raises(HTTPException) as error:
        content.batch_content([other['id'], item['id']], 'delete', expected_versions={other['id']:1,item['id']:1})
    assert error.value.status_code == 409
    assert content.get_content(other['id'])['version'] == 1
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM tombstones WHERE entity_type='content'").fetchone()[0] == 0


def test_deleting_many_versions_streams_bodies_and_removes_identity_ledger(store):
    import tracemalloc
    source, _ = store
    item = content.create_content(title='Large history', content='a'*65536, project_id=source['id'])
    for version in range(1,25):
        content.update_content(item['id'], content=f'{version}'+('b'*65536), expected_version=version)
    tracemalloc.start()
    try:
        content.delete_content(item['id'], expected_version=25)
        _,peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024*1024, peak
    with db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM content_identity_history WHERE content_id=?',(item['id'],)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tombstones WHERE entity_type='content'").fetchone()[0] == 26


def test_deleted_memory_cannot_resurrect_via_import_version_or_relation(store):
    from app.services import importer_service, retrieval_service
    source, _ = store
    retrieval_service.update_settings({"relation_enabled": True})
    original = dict(title='constraint.md', content='Keep this approved constraint', project_id=source['id'])
    item = content.create_content(**original)
    survivor = content.create_content(
        title='constraint.md', content='unrelated live body', project_id=source['id'],
    )
    survivor = content.update_content(
        survivor['id'], content='Keep this approved constraint', expected_version=1,
    )
    survivor = content.update_content(
        survivor['id'], content='later unique body', expected_version=2,
    )
    handle = retrieval_service.evidence_id(survivor['id'], 3)
    retrieval_service.create_relation(
        survivor['id'], item['id'], 'references', handle, 'later unique body',
    )
    content.delete_content(item['id'], expected_version=1)
    with pytest.raises(HTTPException) as imported:
        importer_service.import_document(
            b'Keep this approved constraint', 'constraint.md', project_id=source['id'],
        )
    assert imported.value.status_code == 410
    with pytest.raises(HTTPException) as restored:
        content.restore_version(survivor['id'], version=2, expected_version=3)
    assert restored.value.status_code == 410
    with pytest.raises(HTTPException) as related:
        retrieval_service.create_relation(
            survivor['id'], item['id'], 'references', handle, 'later unique body',
        )
    assert related.value.status_code in (409, 410)
    assert retrieval_service.list_relations()['items'] == []
    with pytest.raises(HTTPException) as recreated:
        content.create_content(**original)
    assert recreated.value.status_code == 410


@pytest.mark.parametrize('action', ['delete', 'retag', 'recategorize'])
def test_batch_mutation_does_not_retain_all_selected_bodies(store, action):
    import tracemalloc
    source, _ = store
    items = [content.create_content(title=f'Batch {i}', content='x'*65536, project_id=source['id']) for i in range(70)]
    ids = [item['id'] for item in items]
    tracemalloc.start()
    try:
        result = content.batch_content(ids, action, tags=['checked'], category='background', expected_versions={cid:1 for cid in ids})
        _,peak=tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result['count'] == 70
    assert peak < 1024*1024, peak
    with db() as conn:
        rows = conn.execute('SELECT version,category,tags FROM contents WHERE project_id=?',(source['id'],)).fetchall()
    if action == 'delete':
        assert rows == []
    else:
        assert len(rows) == 70 and all(row['version']==2 for row in rows)
        if action == 'retag': assert all('checked' in row['tags'] for row in rows)
        else: assert all(row['category']=='background' for row in rows)
