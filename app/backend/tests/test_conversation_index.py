"""Conversation metadata pages never load/parse full transcript JSON."""
import json
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import create_schema, db, encode
from app.routers.conversations import router
from app.services import conversation_service as service


@pytest.fixture
def conversations(tmp_path):
    create_schema()
    text = "大型正文测试" * 4096
    body = encode([{"id": "m1", "role": "user", "content": text, "timestamp": "2026-10-24T10:00:00Z"}])
    with db() as conn:
        for project in ("p0", "p1"):
            conn.execute("INSERT INTO projects(id,name,status,created_at,updated_at) VALUES (?,?,'active','2026','2026')", (project, project))
        for i in range(320):
            # Ties intentionally test stable id ordering across pages.
            started = f"2026-10-{23 + i % 2:02}T10:00:00+00:00"
            updated = f"2026-10-24T10:{i % 60:02}:00+00:00"
            conn.execute("""INSERT INTO conversations(id,external_id,agent,title,project_id,cwd,source_path,started_at,updated_at,messages,process_summary,processing_status,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (f"c{i:04}", f"native-{i}", "cursor" if i % 3 else "codex", "中文标题" if i % 5 == 0 else f"Title {i}", f"p{i % 2}", "/synthetic/repo", f"/synthetic/{i}.jsonl", started, updated, body, "SUMMARY_MUST_NOT_BE_IN_INDEX", "pending", started))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), text


def test_index_sql_projection_limit_and_no_transcript_decode(conversations, monkeypatch):
    client, text = conversations
    traces = []
    @contextmanager
    def traced():
        with db() as conn:
            conn.set_trace_callback(traces.append)
            yield conn
    monkeypatch.setattr(service, "db", traced)
    monkeypatch.setattr(service, "row_to_dict", lambda *_: (_ for _ in ()).throw(AssertionError("Metadata index cannot decode message JSON")))
    result = client.get("/api/conversations/index?limit=13&offset=17")
    assert result.status_code == 200
    value = result.json()
    assert value["total"] == 320 and len(value["items"]) == 13
    assert value["limit"] == 13 and value["offset"] == 17
    assert "大型正文测试" not in result.text and "SUMMARY_MUST_NOT_BE_IN_INDEX" not in result.text
    for item in value["items"]:
        assert "messages" not in item and "process_summary" not in item
        assert {"id", "agent", "title", "project_id", "cwd", "source_path", "started_at", "updated_at", "processing_status"} <= set(item)
    projection = next(sql for sql in traces if sql.startswith("SELECT id,external_id"))
    assert "messages" not in projection and "process_summary" not in projection
    assert "LIMIT 13 OFFSET 17" in projection
    assert len(result.content) < 15_000


def test_index_reuses_legacy_filter_semantics_including_chinese_body_query(conversations):
    client, text = conversations
    filters = {"project_id": "p1", "agent": "cursor", "since": "2026-10-24", "until": "2026-10-24", "query": "大型正文"}
    legacy = service.list_conversations(**filters)
    page = service.conversation_index(**filters, limit=200)
    assert page["total"] == len(legacy) > 0
    assert {item["id"] for item in page["items"]} == {item["id"] for item in legacy}
    assert all("messages" not in item for item in page["items"])
    assert service.conversation_index(query="中文标题", limit=200)["total"] == 64
    assert service.conversation_index(query="没有这样的中文", limit=200)["total"] == 0
    assert client.get("/api/conversations/index?until=2026-13-40").status_code == 422
    assert service.conversation_index(since="2026-10-24T10:00:00Z", until="2026-10-24T10:00:00+00:00")["total"] == 160


def test_index_pages_are_disjoint_and_detail_and_old_list_remain_full(conversations):
    client, text = conversations
    first = client.get("/api/conversations/index?limit=11").json()
    second = client.get("/api/conversations/index?limit=11&offset=11").json()
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    record_id = first["items"][0]["id"]
    detail = client.get("/api/conversations/" + record_id + "?include_messages=true").json()
    assert detail["id"] == record_id and detail["messages"][0]["content"] == text
    legacy = client.get("/api/conversations", params={"query": "中文标题", "agent": "codex", "summary": "false", "limit": 200}).json()
    assert isinstance(legacy, list) and legacy and legacy[0]["messages"][0]["content"] == text


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "offset=-1"])
def test_index_rejects_unbounded_or_negative_page_parameters(conversations, query):
    client, _ = conversations
    assert client.get("/api/conversations/index?" + query).status_code == 422


def test_index_order_and_filters_have_supporting_indexes(conversations):
    with db() as conn:
        variants = [("", (), "idx_conversations_updated_id"), ("WHERE project_id=?", ("p1",), "idx_conversations_project_updated"), ("WHERE agent=?", ("cursor",), "idx_conversations_agent_updated")]
        for clause, params, expected in variants:
            plans = conn.execute(f"EXPLAIN QUERY PLAN SELECT id,agent,title FROM conversations {clause} ORDER BY updated_at DESC,id DESC LIMIT 10 OFFSET 10", params).fetchall()
            details = " ".join(row["detail"] for row in plans)
            assert expected in details and "TEMP B-TREE" not in details
