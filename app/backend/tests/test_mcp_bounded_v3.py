from contextlib import contextmanager

import pytest

from app.database import create_schema, db, now_iso
from app.mcp_server import search_memory, delete_memory
from app.security import authenticated_agent
from fastapi import HTTPException
from app.services import content_service


def test_mcp_search_applies_sql_limit_before_loading_matching_bodies(monkeypatch):
    create_schema()
    with db() as conn:
        for index in range(30):
            conn.execute('''INSERT INTO contents(id,kind,category,title,content,created_at,updated_at)
                VALUES(?,'memory','knowledge','bounded fixture',?,?,?)''',
                (f'fixture-{index}', 'Synthetic body. '*4096, now_iso(), now_iso()))
    queries = []
    @contextmanager
    def traced():
        with db() as conn:
            conn.set_trace_callback(queries.append)
            yield conn
    monkeypatch.setattr(content_service, 'db', traced)
    result = search_memory('bounded fixture', limit=3)
    assert len(result['items']) == 3
    assert any('LIMIT 3' in query for query in queries if query.startswith('SELECT c.'))


@pytest.mark.parametrize('limit', [-1, 0, True, 101])
def test_mcp_search_rejects_unbounded_or_invalid_limit(limit):
    with pytest.raises(ValueError):
        search_memory('synthetic', limit=limit)


def test_mcp_permanent_delete_cannot_bypass_version_guard():
    create_schema()
    item = content_service.create_content(title='Synthetic delete guard', content='v1')
    identity = authenticated_agent.set('codex')
    try:
        # A legacy caller without a readback version must fail closed.
        assert delete_memory(item['id'], confirmed=True)['ok'] is False
        content_service.update_content(item['id'], content='v2', expected_version=1)
        with pytest.raises(HTTPException) as conflict:
            delete_memory(item['id'], confirmed=True, expected_version=1)
        assert conflict.value.status_code == 409
        assert content_service.get_content(item['id'])['content'] == 'v2'
        assert delete_memory(item['id'], confirmed=True, expected_version=2)['deleted'] is True
    finally:
        authenticated_agent.reset(identity)
