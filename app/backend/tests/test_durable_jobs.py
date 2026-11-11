"""Synthetic SQLite jobs: stable identities, restart recovery and lease fencing."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.database import create_schema, db, seed_data
from app.services import conversation_service as conversations
from app.services import distillation_service as distillation
from app.services import processing_jobs as jobs


@pytest.fixture
def durable_env(tmp_path, monkeypatch):
    path = tmp_path / 'durable.db'
    monkeypatch.setenv('CONTEXTCORE_DB', str(path))
    create_schema(path)
    seed_data(path)
    return path


def source():
    return conversations.upsert_conversation(agent='omp', title='Synthetic source', external_id='stable-native-id',
        messages=[{'id':'u1','role':'user','content':'Use local data.'}])


def test_event_replay_and_changed_payload_conflict(durable_env):
    conv = source()
    first = distillation.queue_distillation(conv['id'], event_key='omp:session1:turn1', event_payload_hash='payload1')
    second = distillation.queue_distillation(conv['id'], event_key='omp:session1:turn1', event_payload_hash='payload1')
    assert first['id'] == second['id']
    with pytest.raises(HTTPException) as exc:
        distillation.queue_distillation(conv['id'], event_key='omp:session1:turn1', event_payload_hash='changed')
    assert exc.value.status_code == 409
    assert len(jobs.list_processing_jobs()) == 1


def test_source_updates_coalesce_pending_and_keep_event_receipts(durable_env):
    conv = source()
    first = distillation.queue_distillation(conv['id'], event_key='turn1', event_payload_hash='one')
    conversations.upsert_conversation(agent='omp', title='Synthetic source', external_id='stable-native-id',
        messages=[{'id':'u1','role':'user','content':'Use revised local data.'}])
    rows = jobs.list_processing_jobs()
    assert len(rows) == 1
    assert rows[0]['id'] == first['id']
    assert rows[0]['source_revision'] > 1
    replay = distillation.queue_distillation(conv['id'], event_key='turn1', event_payload_hash='one')
    assert replay['id'] == first['id']


def test_restart_recovers_uninvoked_but_never_replays_invoked(durable_env):
    conv = source()
    q = distillation.queue_distillation(conv['id'])
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        claim = jobs.claim_job(conn, q['id'], lease_seconds=-1)
        assert claim
    recovered = jobs.recover_processing_jobs()
    assert recovered == {'pending':1,'unknown':0}
    with db() as conn:
        claim = jobs.claim_job(conn, q['id'])
        assert jobs.mark_invoked(conn,q['id'],claim['owner'])
        conn.execute("UPDATE processing_jobs SET lease_until='2000-01-01' WHERE id=?",(q['id'],))
    assert jobs.recover_processing_jobs() == {'pending':0,'unknown':1}
    assert distillation.get_pending_distillations() == []
    with db() as conn:
        assert jobs.claim_job(conn,q['id']) is None
    assert jobs.recover_processing_jobs() == {'pending':0,'unknown':0}


def test_running_source_revision_is_immutable_and_new_revision_has_separate_job(durable_env):
    conv = source()
    q = distillation.queue_distillation(conv['id'])
    with db() as conn:
        claimed = jobs.claim_job(conn,q['id'])
        assert jobs.mark_invoked(conn,q['id'],claimed['owner'])
    conversations.upsert_conversation(agent='omp', title='Synthetic source', external_id='stable-native-id',
        messages=[{'id':'u1','role':'user','content':'Changed while running.'}])
    rows = jobs.list_processing_jobs()
    assert len(rows) == 2
    old = next(r for r in rows if r['id'] == q['id'])
    assert old['source_fingerprint'] == claimed['source_fingerprint']
    assert old['state'] == 'running'
    assert next(r for r in rows if r['id'] != q['id'])['state'] == 'pending'


def test_two_workers_cannot_claim_same_job(durable_env):
    conv = source()
    q = distillation.queue_distillation(conv['id'])
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        first = jobs.claim_job(conn,q['id'])
    with db() as conn:
        assert jobs.claim_job(conn,q['id']) is None
        assert not jobs.mark_invoked(conn,q['id'],'another-owner')
        assert not jobs.finish_job(conn,q['id'],'succeeded',owner='another-owner')
    assert jobs.list_processing_jobs()[0]['owner'] == first['owner']


def test_deleted_source_cancels_jobs_without_retaining_body(durable_env):
    conv = source()
    q = distillation.queue_distillation(conv['id'])
    conversations.delete_conversation(conv['id'])
    row = jobs.list_processing_jobs()[0]
    assert row['state'] == 'cancelled'
    assert row['receipt'] is None
    assert 'Use local data' not in str(row)
    with db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM distillation_queue').fetchone()[0] == 0


def test_legacy_processing_migrates_to_unknown(durable_env):
    conv = source()
    q = distillation.queue_distillation(conv['id'])
    with db() as conn:
        conn.execute('DELETE FROM processing_jobs')
        conn.execute("UPDATE distillation_queue SET status='processing' WHERE id=?",(q['id'],))
    assert jobs.initialize_processing_jobs()['migrated'] == 1
    assert jobs.list_processing_jobs()[0]['state'] == 'unknown'
    assert distillation.get_pending_distillations() == []


def test_ensure_schema_does_not_commit_ingestion_transaction(durable_env):
    with pytest.raises(RuntimeError):
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES('rollback','rollback','now','now')")
            jobs.ensure_schema(conn)
            raise RuntimeError('rollback all')
    with db() as conn:
        assert not conn.execute("SELECT id FROM projects WHERE id='rollback'").fetchone()


def test_migration_preserves_legacy_ids_and_completed_receipts(durable_env):
    conv = source()
    q = distillation.queue_distillation(conv['id'])
    with db() as conn:
        conn.execute('DELETE FROM processing_jobs')
        conn.execute("UPDATE distillation_queue SET status='completed',result='[\"receipt-id\"]' WHERE id=?",(q['id'],))
        conn.execute("""INSERT INTO distillation_queue(id,conversation_id,status,payload,created_at,updated_at)
            SELECT 'legacy-duplicate',conversation_id,'pending',payload,created_at,updated_at
            FROM distillation_queue WHERE id=?""",(q['id'],))
    assert jobs.initialize_processing_jobs()['migrated'] == 2
    with db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM distillation_queue').fetchone()[0] == 2
    states = {r['id']:r for r in jobs.list_processing_jobs()}
    assert states[q['id']]['state'] == 'succeeded'
    assert states[q['id']]['receipt'] == '["receipt-id"]'
    assert states['legacy-duplicate']['state'] == 'cancelled'
    assert distillation.get_pending_distillations() == []


def test_native_event_identity_cannot_be_reused_across_sources(durable_env):
    first = source()
    second = conversations.upsert_conversation(agent='omp',title='Other',external_id='other',
        messages=[{'role':'user','content':'Other source.'}])
    distillation.queue_distillation(first['id'],event_key='native:event',event_payload_hash='same')
    with pytest.raises(HTTPException) as exc:
        distillation.queue_distillation(second['id'],event_key='native:event',event_payload_hash='same')
    assert exc.value.status_code == 409


def test_distinct_native_turns_with_same_text_are_not_discarded(durable_env):
    conv = source()
    updated = conversations.upsert_conversation(agent='omp',title='Synthetic source',external_id='stable-native-id',
        messages=[{'id':'u2','role':'user','content':'Use local data.'}],incremental=True)
    assert [m['id'] for m in updated['messages']] == ['u1','u2']
    replayed = conversations.upsert_conversation(agent='omp',title='Synthetic source',external_id='stable-native-id',
        messages=[{'id':'u2','role':'user','content':'Use local data.'}],incremental=True)
    assert replayed['_changed'] is False
    assert len(replayed['messages']) == 2
