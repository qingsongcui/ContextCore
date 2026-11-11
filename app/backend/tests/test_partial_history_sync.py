from app.adapters import PartialHistoryError
from app.database import create_schema, seed_data
from app.services import connection_service, conversation_service


def test_partial_history_commits_valid_sessions_and_reports_failed_files(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'partial.sqlite'))
    create_schema()
    seed_data()
    root = tmp_path / 'history'
    transcript_dir = root / 'session' / '.system_generated' / 'logs'
    transcript_dir.mkdir(parents=True)
    source = transcript_dir / 'transcript.jsonl'
    source.write_text('{"type":"USER_INPUT","content":"synthetic"}\n', encoding='utf-8')
    connection_service.patch_connection('antigravity', paths=[str(root)], enabled=True, confirmed=True)
    session = {
        'external_id': 'antigravity:valid', 'title': 'Readable source',
        'cwd': str(tmp_path / 'project'), 'source_path': str(source),
        'messages': [{'role': 'user', 'content': 'Preserve this actual message'}],
    }
    def partial(*args, **kwargs):
        raise PartialHistoryError(
            host='antigravity', code='partial_sync', sessions=[session],
                failures=[{'path': str(transcript_dir / 'broken.jsonl'), 'code': 'unsupported_format', 'detail': 'Malformed record'}],
        )
    monkeypatch.setattr('app.adapters.read_sessions', partial)
    result = connection_service.sync_connection('antigravity')
    assert str(result.get('error') or '').startswith('skipped_damaged:')
    assert 'broken.jsonl' in result['error']
    records = conversation_service.list_conversations(agent='antigravity')
    assert len(records) == 1
    assert records[0]['messages'][0]['content'] == 'Preserve this actual message'
    connection_service.sync_connection('antigravity')
    assert len(conversation_service.list_conversations(agent='antigravity')) == 1
