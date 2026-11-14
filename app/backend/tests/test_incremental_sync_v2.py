import json
import sqlite3
import pytest
from app.database import create_schema, seed_data, db
from app.services import connection_service as service, conversation_service

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home')); monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'senrelay.db'))
    create_schema(); seed_data()
    return tmp_path

def line(uid, content, parent=None, role='user'):
    return json.dumps({'type': role, 'uuid': uid, 'parentUuid': parent, 'sessionId': 's', 'cwd': '/synthetic/project', 'message': {'role': role, 'content': content}}, ensure_ascii=False).encode() + b'\n'

def configure(root, host='claude-code'):
    service.patch_connection(host, paths=[str(root)], enabled=True, confirmed=True)

def cursor(host='claude-code'):
    with db() as conn:
        return [dict(r) for r in conn.execute('SELECT * FROM source_sync_cursors WHERE host=? ORDER BY source_path', (host,))]

def history(base):
    root = base / 'history'; root.mkdir(); return root

def test_only_changed_source_is_parsed_and_cursor_survives_restart(isolated, monkeypatch):
    root = history(isolated); a = root / 's.jsonl'; b = root / 'other.jsonl'
    a.write_bytes(line('u1', 'one')); b.write_bytes(line('b1', 'independent')); configure(root)
    calls = []
    import app.adapters
    reader = app.adapters.read_sessions
    def tracked(*args, **kwargs):
        assert kwargs['allow_execution'] is False
        calls.append(kwargs.get('source_paths')); return reader(*args, **kwargs)
    monkeypatch.setattr(app.adapters, 'read_sessions', tracked)
    service.sync_connection('claude-code', force=False)
    assert len(cursor()) == 2
    initial_calls = len(calls)
    assert initial_calls == 2  # bounded sync reads and releases one source at a time
    assert service.sync_connection('claude-code', force=False)['reason'] == 'source_unchanged'
    assert len(calls) == initial_calls
    a.write_bytes(a.read_bytes() + line('a1', 'reply', 'u1', 'assistant'))
    service.sync_connection('claude-code', force=False)
    assert calls[-1] == [str(a)]
    service._sync_locks.clear()
    assert service.sync_connection('claude-code', force=False)['status'] == 'skipped'
    assert len(conversation_service.list_conversations(agent='claude-code')) == 2

def test_complete_line_cursor_defers_valid_eof_and_split_utf8(isolated):
    root = history(isolated); source = root / 's.jsonl'; first = line('u1', 'first'); second = line('a1', '中文 reply', 'u1', 'assistant')
    source.write_bytes(first + second[:second.index('中'.encode()) + 1]); configure(root)
    service.sync_connection('claude-code', force=False)
    assert cursor()[0]['complete_offset'] == len(first)
    assert len(conversation_service.list_conversations(agent='claude-code')[0]['messages']) == 1
    source.write_bytes(first + second[:-1]); service.sync_connection('claude-code', force=False)
    assert cursor()[0]['complete_offset'] == len(first)
    source.write_bytes(first + second); service.sync_connection('claude-code', force=False)
    assert cursor()[0]['complete_offset'] == len(first + second)
    assert len(conversation_service.list_conversations(agent='claude-code')[0]['messages']) == 2

def test_corrupt_source_retries_and_preserves_valid_data(isolated):
    root = history(isolated); (root / 's.jsonl').write_bytes(line('u1', 'keep')); bad = root / 'bad.jsonl'; bad.write_bytes(b'{broken}\n'); configure(root)
    first = service.sync_connection('claude-code', force=False)
    assert str(first.get('error') or '').startswith('skipped_damaged:')
    assert 'unsupported_format' in first['error']
    assert len(conversation_service.list_conversations(agent='claude-code')) == 1
    assert next(c for c in cursor() if c['source_path'] == str(bad))['status'] == 'error'
    skipped = service.sync_connection('claude-code', force=False)
    assert skipped['reason'] == 'source_unchanged'
    assert str(skipped.get('error') or '').startswith('skipped_damaged:')
    assert 'unsupported_format' in skipped['error']
    bad.write_bytes(line('u2', 'repaired')); service.sync_connection('claude-code', force=False)
    assert len(conversation_service.list_conversations(agent='claude-code')) == 2
    assert all(c['status'] == 'ready' for c in cursor())

def test_failed_write_never_advances_cursor(isolated, monkeypatch):
    root = history(isolated); (root / 's.jsonl').write_bytes(line('u1', 'first')); configure(root)
    def fail(**kwargs): raise RuntimeError('simulated transaction interruption')
    monkeypatch.setattr(conversation_service, 'upsert_conversation', fail)
    with pytest.raises(Exception): service.sync_connection('claude-code', force=False)
    assert cursor()[0]['complete_offset'] == 0 and cursor()[0]['fingerprint'] == '' and cursor()[0]['status'] == 'error'

def test_rotation_same_size_same_mtime_is_detected(isolated):
    root = history(isolated); source = root / 's.jsonl'; source.write_bytes(line('u1', 'before')); configure(root)
    service.sync_connection('claude-code', force=False); old = cursor()[0]['fingerprint']; stat = source.stat()
    source.unlink(); source.write_bytes(line('u1', 'after!'))
    import os
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns)); service.sync_connection('claude-code', force=False)
    assert cursor()[0]['fingerprint'] != old
    assert conversation_service.list_conversations(agent='claude-code')[0]['messages'][0]['content'] == 'after!'

def test_wal_changes_trigger_read_without_writing_source(isolated):
    root = isolated / 'opencode'; root.mkdir(); source = root / 'opencode.db'; writer = sqlite3.connect(source)
    writer.executescript('PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER); CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT); CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);')
    writer.execute("INSERT INTO session VALUES('s','WAL session','/synthetic/project',NULL,1,1)")
    writer.execute("INSERT INTO message VALUES('u','s',1,?)", (json.dumps({'role': 'user'}),))
    writer.execute("INSERT INTO part VALUES('p','u','s',1,?)", (json.dumps({'type': 'text', 'text': 'before WAL'}),)); writer.commit()
    configure(root, 'opencode'); service.sync_connection('opencode', force=False); old = cursor('opencode')[0]['fingerprint']
    writer.execute("UPDATE part SET data=? WHERE id='p'", (json.dumps({'type': 'text', 'text': 'after WAL'}),)); writer.commit()
    service.sync_connection('opencode', force=False)
    assert cursor('opencode')[0]['fingerprint'] != old
    assert conversation_service.list_conversations(agent='opencode')[0]['messages'][0]['content'] == 'after WAL'
    assert writer.execute('SELECT count(*) FROM part').fetchone()[0] == 1
    writer.close()


def test_targeted_relay_does_not_certify_cached_identity_missing_from_shared_db(isolated):
    root = isolated / "opencode"
    root.mkdir()
    source = root / "opencode.db"
    writer = sqlite3.connect(source)
    writer.executescript("""
        CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
    """)
    writer.execute("INSERT INTO session VALUES('original','Original session','/synthetic/project',NULL,1,1)")
    writer.execute("INSERT INTO message VALUES('u','original',1,?)", (json.dumps({"role": "user"}),))
    writer.execute("INSERT INTO part VALUES('p','u','original',1,?)",
                   (json.dumps({"type": "text", "text": "Original retained dialogue"}),))
    writer.commit()
    configure(root, "opencode")
    service.sync_connection("opencode", force=False)
    selected = conversation_service.list_conversations(agent="opencode")[0]

    # The database is still readable, but the selected native identity vanished.
    writer.execute("UPDATE session SET id='replacement', title='Replacement session'")
    writer.execute("UPDATE message SET session_id='replacement'")
    writer.execute("UPDATE part SET session_id='replacement', data=?",
                   (json.dumps({"type": "text", "text": "Different native conversation"}),))
    writer.commit()
    writer.close()
    # A background pass already advanced the file cursor. A ready/unchanged
    # cursor must not certify presence of every historical row from that source.
    service.sync_connection("opencode", force=False)
    statuses = service.sync_conversation_for_relay(
        selected["id"], agent="opencode", source_path=selected["source_path"])
    assert statuses[0]["status"] == "partial"
    assert statuses[0]["reason"] == "target_not_observed"
    assert statuses[0]["target_observed"] is False
    assert conversation_service.get_conversation(selected["id"])["messages"][0]["content"] == "Original retained dialogue"
    replacement = next(item for item in conversation_service.list_conversations(agent="opencode")
                       if item["id"] != selected["id"])
    fresh = service.sync_conversation_for_relay(
        replacement["id"], agent="opencode", source_path=replacement["source_path"])
    assert fresh[0]["status"] == "ok"
    assert fresh[0]["target_observed"] is True

def test_tombstone_blocks_replay_after_source_change(isolated):
    root = history(isolated); source = root / 's.jsonl'; source.write_bytes(line('u1', 'do not resurrect')); configure(root)
    service.sync_connection('claude-code', force=False); record = conversation_service.list_conversations(agent='claude-code')[0]
    conversation_service.delete_conversation(record['id'])
    source.write_bytes(source.read_bytes() + line('a1', 'new reply', 'u1', 'assistant')); service.sync_connection('claude-code', force=False)
    assert conversation_service.list_conversations(agent='claude-code') == []

def test_changed_source_cannot_promote_symlink_to_new_grant(isolated):
    root = history(isolated); (root / 's.jsonl').write_bytes(line('u1', 'authorized'))
    outside = isolated / 'outside.jsonl'; outside.write_bytes(line('x1', 'not authorized'))
    (root / 'escape.jsonl').symlink_to(outside)
    configure(root)
    result = service.sync_connection('claude-code', force=False)
    assert str(result.get('error') or '').startswith('skipped_damaged:')
    assert 'symlink_escape' in result['error']
    assert len(conversation_service.list_conversations(agent='claude-code')) == 1
    assert 'not authorized' not in json.dumps(conversation_service.list_conversations(agent='claude-code'))


def test_mutation_during_parse_does_not_commit_moving_source(isolated, monkeypatch):
    root = history(isolated); source = root / 's.jsonl'; source.write_bytes(line('u1', 'first')); configure(root)
    import app.adapters
    reader = app.adapters.read_sessions
    def moving(*args, **kwargs):
        result = reader(*args, **kwargs)
        source.write_bytes(source.read_bytes() + line('a1', 'raced', 'u1', 'assistant'))
        return result
    monkeypatch.setattr(app.adapters, 'read_sessions', moving)
    result = service.sync_connection('claude-code', force=False)
    assert result['error'].startswith('partial_sync:')
    assert conversation_service.list_conversations(agent='claude-code') == []
    assert cursor()[0]['complete_offset'] == 0 and cursor()[0]['status'] == 'error'


def test_committed_corrupt_last_line_is_error_not_live_tail(isolated):
    root = history(isolated); source = root / 'rollout-x.jsonl'
    source.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'x', 'cwd': '/repo'}}) + '\n{broken}\n')
    from app.adapters import read_sessions, AgentAdapterError
    with pytest.raises(AgentAdapterError): read_sessions('codex', [str(root)])

def test_partial_database_keeps_readable_sessions_but_not_cursor(isolated, monkeypatch):
    root = isolated / 'db'; root.mkdir(); source = root / 'state.vscdb'; source.write_bytes(b'fixture database placeholder')
    configure(root, 'cursor')
    from app.adapters import PartialHistoryError
    def partial(*args, **kwargs):
        raise PartialHistoryError(host='cursor', code='partial_sync', sessions=[{
            'external_id': 'cursor:good', 'title': 'Readable', 'source_path': str(source),
            'messages': [{'role': 'user', 'content': 'valid row from shared database'}],
        }], failures=[{'path': str(source), 'code': 'unsupported_format', 'detail': 'another row failed'}])
    monkeypatch.setattr('app.adapters.read_sessions', partial)
    result = service.sync_connection('cursor', force=False)
    assert str(result.get('error') or '').startswith('skipped_damaged:')
    assert conversation_service.list_conversations(agent='cursor')[0]['messages'][0]['content'] == 'valid row from shared database'
    assert cursor('cursor')[0]['status'] == 'error' and cursor('cursor')[0]['fingerprint']
    skipped = service.sync_connection('cursor', force=False)
    assert skipped['reason'] == 'source_unchanged'
    assert str(skipped.get('error') or '').startswith('skipped_damaged:')

def test_cursor_transient_file_changed_clears_on_unchanged_skip(isolated, monkeypatch):
    root = isolated / 'db'; root.mkdir(); source = root / 'state.vscdb'; source.write_bytes(b'fixture database placeholder')
    configure(root, 'cursor')
    from app.adapters import AgentAdapterError
    def moving(*args, **kwargs):
        raise AgentAdapterError(host='cursor', code='file_changed', path=str(source.resolve()), detail='WAL grew', retryable=True)
    monkeypatch.setattr('app.adapters.read_sessions', moving)
    first = service.sync_connection('cursor', force=False)
    assert first.get('error') and 'partial_sync' in first['error']
    assert cursor('cursor')[0]['status'] == 'error'
    second = service.sync_connection('cursor', force=False)
    assert second['reason'] == 'source_unchanged'
    assert second.get('error') is None
    assert second.get('status') == 'skipped'


def test_new_incomplete_source_waits_without_false_parse_error(isolated):
    root = history(isolated); source = root / 's.jsonl'; source.write_bytes(line('u1', 'first')[:-1]); configure(root)
    result = service.sync_connection('claude-code', force=False)
    assert result['status'] == 'waiting' and result['reason'] == 'incomplete_source'
    assert conversation_service.list_conversations(agent='claude-code') == []
    assert cursor()[0]['complete_offset'] == 0
    source.write_bytes(source.read_bytes() + b'\n'); service.sync_connection('claude-code', force=False)
    assert conversation_service.list_conversations(agent='claude-code')[0]['messages'][0]['content'] == 'first'

def test_codex_native_cumulative_usage_is_ingested_once_on_reconcile(isolated):
    root = history(isolated); source = root / 'rollout-native.jsonl'
    rows = [
        {'type': 'session_meta', 'timestamp': '2026-10-24T01:00:00Z', 'payload': {'id': 'native', 'cwd': '/synthetic/project'}},
        {'type': 'turn_context', 'payload': {'model': 'fixture-model'}},
        {'type': 'response_item', 'timestamp': '2026-10-24T01:00:01Z', 'payload': {'role': 'user', 'content': 'DO_NOT_LEDGER'}},
        {'type': 'event_msg', 'timestamp': '2026-10-24T01:00:02Z', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 100, 'output_tokens': 10, 'SECRET': 'DO_NOT_LEDGER'}}}},
        {'type': 'event_msg', 'timestamp': '2026-10-24T01:00:03Z', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 180, 'output_tokens': 20}}}},
    ]
    source.write_text(''.join(json.dumps(r) + '\n' for r in rows)); configure(root, 'codex')
    service.sync_connection('codex', force=False); service.sync_connection('codex', force=True)
    from app.services import usage_service
    report = usage_service.summary({'host_id': 'codex'})
    assert report['input_tokens'] == 180 and report['output_tokens'] == 20 and report['attempts'] == 2
    with db() as conn:
        assert 'DO_NOT_LEDGER' not in ''.join(r[0] for r in conn.execute('SELECT data FROM usage_v2_observations'))
    rows.append({'type': 'event_msg', 'timestamp': '2026-10-24T01:00:04Z', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 200, 'output_tokens': 25}}}})
    source.write_text(''.join(json.dumps(r) + '\n' for r in rows)); service.sync_connection('codex', force=False)
    report = usage_service.summary({'host_id': 'codex'})
    assert report['input_tokens'] == 200 and report['output_tokens'] == 25


def test_claude_native_usage_retains_anthropic_cache_semantics(isolated):
    root = history(isolated); source = root / 's.jsonl'
    assistant = {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1', 'timestamp': '2026-10-24T02:00:00Z',
                 'message': {'id': 'msg-api', 'role': 'assistant', 'model': 'fixture-claude', 'content': 'DO_NOT_LEDGER',
                             'usage': {'input_tokens': 100, 'output_tokens': 10, 'cache_read_input_tokens': 50}}}
    source.write_bytes(line('u1', 'question') + json.dumps(assistant).encode() + b'\n'); configure(root)
    service.sync_connection('claude-code', force=False)
    from app.services import usage_service
    report = usage_service.summary({'host_id': 'claude-code'})
    assert report['input_tokens'] == 150 and report['output_tokens'] == 10
    from app.adapters import read_sessions
    native = read_sessions('claude-code', [str(root)])[0]['usage_observations']
    assert 'DO_NOT_LEDGER' not in json.dumps(native)


def test_omp_native_usage_metadata_excludes_message_body(isolated):
    root = history(isolated); source = root / 'session.jsonl'
    rows = [{'type': 'session', 'version': 3, 'id': 'omp-session', 'timestamp': '2026-10-24T03:00:00Z', 'cwd': '/synthetic/project'},
            {'type': 'message', 'id': 'm1', 'timestamp': '2026-10-24T03:00:01Z', 'message': {'role': 'assistant', 'model': 'fixture-omp', 'content': 'DO_NOT_LEDGER', 'usage': {'input': 40, 'output': 8, 'cacheRead': 20, 'cacheWrite': 0}}}]
    source.write_text(''.join(json.dumps(r) + '\n' for r in rows)); configure(root, 'omp')
    service.sync_connection('omp', force=False)
    from app.services import usage_service
    report = usage_service.summary({'host_id': 'omp'})
    assert report['input_tokens'] == 60 and report['output_tokens'] == 8
    from app.adapters import read_sessions
    assert 'DO_NOT_LEDGER' not in json.dumps(read_sessions('omp', [str(root)])[0]['usage_observations'])


def test_usage_import_error_keeps_history_and_retries_metadata(isolated, monkeypatch):
    root = history(isolated); source = root / 's.jsonl'
    assistant = {'type': 'assistant', 'uuid': 'a1', 'parentUuid': 'u1', 'timestamp': '2026-10-24T04:00:00Z',
                 'message': {'id': 'msg-api', 'role': 'assistant', 'content': 'history remains', 'usage': {'input_tokens': 3, 'output_tokens': 1}}}
    source.write_bytes(line('u1', 'question') + json.dumps(assistant).encode() + b'\n'); configure(root)
    from app.services import usage_service
    ingest = usage_service.ingest
    def unavailable(*args): raise RuntimeError('ledger unavailable')
    monkeypatch.setattr(usage_service, 'ingest', unavailable)
    result = service.sync_connection('claude-code', force=False)
    assert result['error'] is None and len(conversation_service.list_conversations(agent='claude-code')) == 1
    assert cursor()[0]['status'] == 'error'
    monkeypatch.setattr(usage_service, 'ingest', ingest); service.sync_connection('claude-code', force=False)
    assert cursor()[0]['status'] == 'ready' and usage_service.summary()['input_tokens'] == 3

def test_native_usage_only_session_does_not_create_empty_conversation(isolated):
    root = history(isolated); source = root / 'rollout-meter.jsonl'
    rows = [{'type': 'session_meta', 'timestamp': '2026-10-24T05:00:00Z', 'payload': {'id': 'meter'}},
            {'type': 'event_msg', 'timestamp': '2026-10-24T05:00:01Z', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 10, 'output_tokens': 2}}}}]
    source.write_text(''.join(json.dumps(r) + '\n' for r in rows)); configure(root, 'codex')
    service.sync_connection('codex', force=False)
    from app.services import usage_service
    assert usage_service.summary({'host_id': 'codex'})['input_tokens'] == 10
    assert conversation_service.list_conversations(agent='codex') == []


def test_opencode_native_usage_is_automatic_and_contains_no_body(isolated):
    root = isolated / 'opencode-native'; root.mkdir(); source = root / 'opencode.db'
    writer = sqlite3.connect(source)
    writer.executescript('CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER); CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT); CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);')
    writer.execute("INSERT INTO session VALUES('s','Native usage','/synthetic/project',NULL,1000,2000)")
    writer.execute("INSERT INTO message VALUES('a','s',2000,?)", (json.dumps({'role': 'assistant', 'modelID': 'fixture-open', 'providerID': 'fixture-provider', 'tokens': {'input': 20, 'output': 5, 'reasoning': 2, 'cache': {'read': 8, 'write': 0}}}),))
    writer.execute("INSERT INTO part VALUES('p','a','s',2000,?)", (json.dumps({'type': 'text', 'text': 'DO_NOT_LEDGER'}),)); writer.commit(); writer.close()
    configure(root, 'opencode'); service.sync_connection('opencode', force=False)
    from app.services import usage_service
    result = usage_service.summary({'host_id': 'opencode'})
    assert result['input_tokens'] == 28 and result['output_tokens'] == 5
    from app.adapters import read_sessions
    assert 'DO_NOT_LEDGER' not in json.dumps(read_sessions('opencode', [str(root)])[0]['usage_observations'])


def test_history_pause_resume_preserves_cursor_and_reconciles_once(isolated):
    from fastapi import HTTPException
    root = history(isolated)
    source = root / 's.jsonl'
    source.write_bytes(line('u1', 'synthetic before pause'))
    configure(root)
    service.sync_connection('claude-code', force=False)
    before = cursor()
    service.patch_connection('claude-code', paused=True, confirmed=True)
    source.write_bytes(source.read_bytes() + line('a1', 'synthetic while paused', 'u1', 'assistant'))
    with pytest.raises(HTTPException) as failure:
        service.sync_connection('claude-code', force=False)
    assert failure.value.status_code == 400
    assert cursor() == before
    service.patch_connection('claude-code', paused=False, confirmed=True)
    service.sync_connection('claude-code', force=False)
    records = conversation_service.list_conversations(agent='claude-code')
    assert len(records) == 1
    record = conversation_service.get_conversation(records[0]['id'])
    assert len(record['messages']) == 2
    assert record['messages'][1]['content'] == 'synthetic while paused'
    service.sync_connection('claude-code', force=False)
    assert len(conversation_service.get_conversation(records[0]['id'])['messages']) == 2


def test_sqlite_wal_after_read_keeps_snapshot_but_does_not_acknowledge_new_data(isolated, monkeypatch):
    import app.adapters
    root = isolated / "cursor-wal"
    root.mkdir()
    source = root / "state.vscdb"
    writer = sqlite3.connect(source)
    writer.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA wal_autocheckpoint=0;
        CREATE TABLE composerHeaders (composerId TEXT, value TEXT, isSubagent INTEGER, createdAt INTEGER, lastUpdatedAt INTEGER);
        CREATE TABLE cursorDiskKV (key TEXT, value TEXT);
    """)
    writer.execute("INSERT INTO composerHeaders VALUES('selected',?,0,1,1)",
                   (json.dumps({"name": "WAL conversation", "workspaceIdentifier": {"uri": {"fsPath": "/synthetic/project"}}}),))
    def append_bubble(bubble_id, text):
        writer.execute("INSERT INTO cursorDiskKV VALUES(?,?)", (
            f"bubbleId:selected:{bubble_id}",
            json.dumps({"type": 1, "bubbleId": bubble_id, "text": text, "createdAt": "2026-11-12T10:00:00Z"}),
        ))
        writer.commit()
    try:
        append_bubble("b1", "Previously imported")
        configure(root, "cursor")
        service.sync_connection("cursor", force=False)
        selected = conversation_service.list_conversations(agent="cursor")[0]
        append_bubble("b2", "Valid consistent read")
        before_read = service._snapshot_source("cursor", source, [str(root)])
        original = app.adapters.read_sessions
        def append_after_read(*args, **kwargs):
            batch = original(*args, **kwargs)
            append_bubble("b3", "Written after read")
            return batch
        monkeypatch.setattr(app.adapters, "read_sessions", append_after_read)
        statuses = service.sync_conversation_for_relay(
            selected["id"], agent="cursor", source_path=selected["source_path"])
        assert statuses[0]["status"] == "partial"
        assert statuses[0]["reason"] == "source_changed_during_read"
        assert statuses[0]["error"] is None
        assert statuses[0]["connection_error"] is None
        assert statuses[0]["target_observed"] is True
        messages = conversation_service.get_conversation(selected["id"])["messages"]
        assert {message["content"] for message in messages} == {"Previously imported", "Valid consistent read"}
        receipt = cursor("cursor")[0]
        # The consistent snapshot is acknowledged without certifying the newer
        # WAL: the cursor keeps the pre-read fingerprint so the next pass retries.
        assert receipt["status"] == "ready" and receipt["error"] is None
        assert receipt["fingerprint"] == before_read["fingerprint"]
        current_disk_fp = service._snapshot_source("cursor", source, [str(root)])["fingerprint"]
        assert receipt["fingerprint"] != current_disk_fp

        monkeypatch.setattr(app.adapters, "read_sessions", original)
        service.sync_connection("cursor", force=False)
        messages = conversation_service.get_conversation(selected["id"])["messages"]
        assert {message["content"] for message in messages} == {
            "Previously imported", "Valid consistent read", "Written after read",
        }
        assert cursor("cursor")[0]["status"] == "ready"
    finally:
        writer.close()


def test_codex_session_index_rename_invalidates_cached_relay_title(isolated):
    from app.services import handoff_service
    root = history(isolated)
    selected = conversation_service.upsert_conversation(
        agent="codex", external_id="codex:renamed", title="Cached before rename",
        source_path=str(root / "rollout-renamed.jsonl"),
        messages=[{"external_id": "q1", "role": "user", "content": "Explain the code path"}],
    )
    with db() as conn:
        assert conn.execute("SELECT 1 FROM conversation_relay_index WHERE conversation_id=?", (selected["id"],)).fetchone()
    (root / "session_index.jsonl").write_text(
        json.dumps({"id": "renamed", "thread_name": "Current native title", "updated_at": "2026-11-12T10:00:00Z"}) + "\n")
    configure(root, "codex")
    service.sync_connection("codex", force=False)
    with db() as conn:
        assert conn.execute("SELECT title FROM conversations WHERE id=?", (selected["id"],)).fetchone()[0] == "Current native title"
        assert conn.execute("SELECT 1 FROM conversation_relay_index WHERE conversation_id=?", (selected["id"],)).fetchone() is None
    listing = handoff_service.discover_relay_conversations(sync_first=False, link_unlinked=False, language="en")
    assert "Current native title" in json.dumps(listing)
    assert "Cached before rename" not in json.dumps(listing)


def test_opencode_wal_after_spooled_read_keeps_valid_history_and_retries(isolated, monkeypatch):
    from app.adapters import opencode
    root = isolated / "opencode-wal"
    root.mkdir()
    source = root / "opencode.db"
    writer = sqlite3.connect(source)
    writer.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA wal_autocheckpoint=0;
        CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        INSERT INTO session VALUES('selected','WAL session','/synthetic/project',NULL,1,1);
    """)
    def append_message(message_id, text, timestamp):
        writer.execute("INSERT INTO message VALUES(?,'selected',?,?)",
                       (message_id, timestamp, json.dumps({"role": "user"})))
        writer.execute("INSERT INTO part VALUES(?,?,'selected',?,?)",
                       ("p-" + message_id, message_id, timestamp, json.dumps({"type": "text", "text": text})))
        writer.commit()
    try:
        append_message("m1", "Earlier message", 1)
        configure(root, "opencode")
        service.sync_connection("opencode", force=False)
        selected = conversation_service.list_conversations(agent="opencode")[0]
        append_message("m2", "Consistent snapshot message", 2)
        before_read = service._snapshot_source("opencode", source, [str(root)])
        original = opencode.iter_opencode_sessions
        def moving(*args, **kwargs):
            yield from original(*args, **kwargs)
            append_message("m3", "New WAL message", 3)
        monkeypatch.setattr(opencode, "iter_opencode_sessions", moving)
        statuses = service.sync_conversation_for_relay(
            selected["id"], agent="opencode", source_path=selected["source_path"])
        assert statuses[0]["status"] == "partial"
        assert statuses[0]["reason"] == "source_changed_during_read"
        assert statuses[0]["error"] is None
        assert statuses[0]["target_observed"] is True
        messages = conversation_service.get_conversation(selected["id"])["messages"]
        assert {message["content"] for message in messages} == {"Earlier message", "Consistent snapshot message"}
        receipt = cursor("opencode")[0]
        # Spooled read succeeded; only the post-read WAL tail is still pending.
        assert receipt["status"] == "ready" and receipt["error"] is None
        assert receipt["fingerprint"] == before_read["fingerprint"]
        current_disk_fp = service._snapshot_source("opencode", source, [str(root)])["fingerprint"]
        assert receipt["fingerprint"] != current_disk_fp
        monkeypatch.setattr(opencode, "iter_opencode_sessions", original)
        service.sync_connection("opencode", force=False)
        messages = conversation_service.get_conversation(selected["id"])["messages"]
        assert {message["content"] for message in messages} == {"Earlier message", "Consistent snapshot message", "New WAL message"}
        assert cursor("opencode")[0]["status"] == "ready"
    finally:
        writer.close()
