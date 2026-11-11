"""Regression coverage for bounded content, conversation, and import APIs.

All rows and documents in this module are synthetic and live in pytest's temp DB.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import create_schema, db, encode, now_iso
from app.routers.content import router as content_router
from app.routers.conversations import router as conversations_router
from app.services import content_service, conversation_service, retrieval_service
from app.services import project_service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "bounded-content.sqlite"))
    create_schema()
    app = FastAPI()
    app.include_router(content_router)
    app.include_router(conversations_router)
    with TestClient(app) as test_client:
        yield test_client


def _new_content(title: str, body: str, tags: list[str] | None = None) -> dict:
    return content_service.create_content(title=title, content=body, tags=tags or [])


def test_single_delete_requires_version_and_does_not_delete_new_revision(client):
    item = _new_content("CAS delete", "v1 body")
    content_service.update_content(item["id"], content="v2 body", expected_version=1)

    assert client.delete(f"/api/content/{item['id']}").status_code == 422
    stale = client.delete(f"/api/content/{item['id']}?expected_version=1")
    assert stale.status_code == 409
    still_current = client.get(f"/api/content/{item['id']}").json()
    assert still_current["version"] == 2 and still_current["content"] == "v2 body"

    deleted = client.delete(f"/api/content/{item['id']}?expected_version=2")
    assert deleted.status_code == 200
    assert client.get(f"/api/content/{item['id']}").status_code == 404


def test_batch_delete_checks_all_versions_before_mutating_any(client):
    first = _new_content("batch one", "body one")
    second = _new_content("batch two", "body two")
    content_service.update_content(second["id"], content="body two v2", expected_version=1)

    stale = client.post("/api/content/batch", json={
        "ids": [first["id"], second["id"]],
        "action": "delete",
        "expected_versions": {first["id"]: 1, second["id"]: 1},
    })
    assert stale.status_code == 409
    assert client.get(f"/api/content/{first['id']}").status_code == 200
    assert client.get(f"/api/content/{second['id']}").status_code == 200

    missing_map = client.post("/api/content/batch", json={
        "ids": [first["id"]], "action": "delete",
    })
    assert missing_map.status_code == 422

    success = client.post("/api/content/batch", json={
        "ids": [first["id"], second["id"]],
        "action": "delete",
        "expected_versions": {first["id"]: 1, second["id"]: 2},
    })
    assert success.status_code == 200 and success.json()["count"] == 2


def test_content_index_is_paged_metadata_with_bounded_preview_and_sql_tag_filter(client, monkeypatch):
    long_body = "正文🙂" * 20_000
    rows = [_new_content(f"index-{i:03}", long_body, ["keep"] if i % 2 else ["skip"]) for i in range(24)]
    traces: list[str] = []

    @contextmanager
    def traced_db():
        with db() as conn:
            conn.set_trace_callback(traces.append)
            yield conn

    monkeypatch.setattr(content_service, "db", traced_db)
    response = client.get("/api/content/index?tag=keep&limit=5&offset=2")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 12 and payload["limit"] == 5 and payload["offset"] == 2
    assert len(payload["items"]) == 5
    assert all(len(row["content_preview"]) <= 240 for row in payload["items"])
    assert all(row["tags"] == ["keep"] for row in payload["items"])
    assert "content" not in payload["items"][0] and "🙂" in payload["items"][0]["content_preview"]
    select_sql = [sql for sql in traces if sql.lstrip().upper().startswith("SELECT")]
    assert any("json_each" in sql.lower() for sql in select_sql), "tag membership must be filtered in SQLite"
    assert any("LIMIT 5 OFFSET 2" in sql for sql in select_sql)
    assert not any("SELECT C.*" in sql.upper() or "SELECT * FROM CONTENTS" in sql.upper() for sql in select_sql)


def test_detail_defaults_to_no_history_and_versions_have_metadata_pages(client):
    item = _new_content("Versioned", "original body")
    content_service.update_content(item["id"], content="second body", expected_version=1)
    content_service.update_content(item["id"], content="third body", expected_version=2)

    detail = client.get(f"/api/content/{item['id']}")
    assert detail.status_code == 200
    assert detail.json()["content"] == "third body"
    assert detail.json()["versions"] in (None, [])

    explicit_history = client.get(f"/api/content/{item['id']}?include_versions=true").json()
    assert [v["version"] for v in explicit_history["versions"]] == [1, 2, 3]

    page = client.get(f"/api/content/{item['id']}/versions?limit=2&offset=1")
    assert page.status_code == 200
    assert page.json()["total"] == 3 and page.json()["limit"] == 2 and page.json()["offset"] == 1
    assert [v["version"] for v in page.json()["items"]] == [2, 3]
    assert "content" not in page.json()["items"][0]
    version = client.get(f"/api/content/{item['id']}/versions/1")
    assert version.status_code == 200
    assert version.json()["content"] == "original body" and version.json()["version"] == 1


def test_conversation_detail_and_messages_page_avoid_decoding_full_transcript(client, monkeypatch):
    messages = [{"id": f"m{i:03}", "role": "user", "content": f"message-{i}🙂,[]\\\"",
                 "timestamp": f"2026-10-24T10:{i % 60:02}:00Z"} for i in range(105)]
    conv_id = "synthetic-long-conversation"
    with db() as conn:
        now = now_iso()
        conn.execute("""INSERT INTO conversations
            (id,agent,title,started_at,updated_at,messages,process_summary,processing_status,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (conv_id, "synthetic", "Long conversation", now, now, encode(messages), "large summary", "processed", now))

    traces: list[str] = []

    @contextmanager
    def traced_db():
        with db() as conn:
            conn.set_trace_callback(traces.append)
            yield conn

    monkeypatch.setattr(conversation_service, "db", traced_db)
    detail = client.get(f"/api/conversations/{conv_id}")
    assert detail.status_code == 200
    assert detail.json()["messages"] == []
    assert detail.json()["process_summary"] == ""

    page = client.get(f"/api/conversations/{conv_id}/messages?limit=30&offset=60")
    assert page.status_code == 200
    value = page.json()
    assert value["total"] == 105 and value["limit"] == 30 and value["offset"] == 60
    assert [m["id"] for m in value["items"]] == [f"m{i:03}" for i in range(60, 90)]
    assert value["items"][1]["content"] == 'message-61🙂,[]\\"'
    assert not any("json_each" in sql.lower() for sql in traces)
    assert any("conversation_message_offsets" in sql.lower() for sql in traces)
    assert not any("SELECT MESSAGES" in sql.upper() for sql in traces)


def test_legacy_content_and_conversation_lists_are_bounded_by_default(client):
    with db() as conn:
        now = now_iso()
        conn.executemany(
            """INSERT INTO contents
               (id,kind,category,title,content,tags,status,version,related_ids,created_at,updated_at)
               VALUES (?, 'memory','knowledge',?,?,'[]','active',1,'[]',?,?)""",
            [(f"legacy-content-{i:03}", f"Content {i:03}", f"body-{i:03}", now, now)
             for i in range(55)],
        )
        conn.executemany(
            """INSERT INTO conversations
               (id,agent,title,started_at,updated_at,messages,process_summary,processing_status,created_at)
               VALUES (?, 'synthetic', ?, ?, ?, ?, ?, 'processed', ?)""",
            [(f"legacy-conversation-{i:03}", f"Conversation {i:03}", now, now,
              encode([{"id": "m1", "role": "user", "content": f"body-{i:03}", "timestamp": ""}]),
              "summary", now) for i in range(55)],
        )

    content_page = client.get("/api/content")
    assert content_page.status_code == 200
    assert len(content_page.json()) == 50
    assert all(item["content"] == "" for item in content_page.json())
    content_detail_page = client.get("/api/content?summary=false&limit=2&offset=1")
    assert content_detail_page.status_code == 200
    assert len(content_detail_page.json()) == 2
    assert content_detail_page.json()[0]["content"].startswith("body-")

    conversation_page = client.get("/api/conversations")
    assert conversation_page.status_code == 200
    assert len(conversation_page.json()) == 50
    assert all(item["messages"] == [] and item["process_summary"] == "" for item in conversation_page.json())
    conversation_detail_page = client.get("/api/conversations?summary=false&limit=2&offset=1")
    assert conversation_detail_page.status_code == 200
    assert len(conversation_detail_page.json()) == 2
    assert conversation_detail_page.json()[0]["messages"][0]["content"].startswith("body-")

    assert client.get("/api/content?limit=201").status_code == 422
    assert client.get("/api/conversations?offset=-1").status_code == 422


def test_json_import_rejects_excessive_token_count_before_building_ast(monkeypatch):
    from fastapi import HTTPException
    from app.services import importer_service

    raw = b"[" + b"0," * 2_000_000 + b"0]"

    def forbidden_loads(*_args, **_kwargs):
        raise AssertionError("JSON AST must not be built before the node budget check")

    monkeypatch.setattr(importer_service.json, "loads", forbidden_loads)
    with pytest.raises(HTTPException) as rejected:
        importer_service.import_document(raw, "synthetic-many-values.json")
    assert rejected.value.status_code == 413
    assert "JSON" in rejected.value.detail and "limit" in rejected.value.detail.lower()


def test_json_import_keeps_valid_input_without_pretty_print_expansion(client):
    raw = '{ "items" : [1, 2, 3], "name" : "synthetic" }'
    response = client.post("/api/import", files={"file": ("verbatim.json", raw.encode(), "application/json")})
    assert response.status_code == 200
    assert response.json()["content"] == raw


def test_message_offset_index_tracks_upsert_and_direct_transcript_replacement(client):
    created = conversation_service.upsert_conversation(
        agent="synthetic", title="Offset index", external_id="offset-index",
        messages=[{"id": "m1", "role": "user", "content": "first🙂", "timestamp": ""}],
    )
    conversation_id = created["id"]
    with db() as conn:
        stored_count = conn.execute(
            "SELECT message_count FROM conversation_message_index_state WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()["message_count"]
    assert stored_count == 1
    assert conversation_service.conversation_messages_page(conversation_id)["items"][0]["content"] == "first🙂"

    with db() as conn:
        conn.execute(
            "UPDATE conversations SET messages=? WHERE id=?",
            (encode([{"id": "m2", "role": "assistant", "content": "replacement🧪", "timestamp": ""}]), conversation_id),
        )
        assert conn.execute(
            "SELECT 1 FROM conversation_message_index_state WHERE conversation_id=?", (conversation_id,),
        ).fetchone() is None

    page = conversation_service.conversation_messages_page(conversation_id)
    assert page["total"] == 1
    assert page["items"][0]["id"] == "m2" and page["items"][0]["content"] == "replacement🧪"


def test_message_paging_reports_unsupported_python_before_attribute_error(client, monkeypatch):
    from contextlib import contextmanager
    from fastapi import HTTPException

    with db() as conn:
        now = now_iso()
        conn.execute(
            """INSERT INTO conversations
               (id,agent,title,started_at,updated_at,messages,process_summary,processing_status,created_at)
               VALUES ('legacy-python','synthetic','Legacy runtime',?,?,?,'[]','processed',?)""",
            (now, now, encode([]), now),
        )

    class WithoutBlobOpen:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            if name == "blobopen":
                raise AttributeError(name)
            return getattr(self.connection, name)

    @contextmanager
    def legacy_db():
        with db() as conn:
            yield WithoutBlobOpen(conn)

    monkeypatch.setattr(conversation_service, "db", legacy_db)
    with pytest.raises(HTTPException) as unsupported:
        conversation_service.conversation_messages_page("legacy-python")
    assert unsupported.value.status_code == 503
    assert "Python 3.11" in unsupported.value.detail


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1"])
def test_message_page_rejects_unbounded_or_negative_pagination(client, query):
    response = client.get("/api/conversations/missing/messages?" + query)
    assert response.status_code == 422


def test_merge_rejects_stale_source_revision_without_historicizing_any_source(client):
    project_id = project_service.create_project("synthetic merge project")["id"]
    first = content_service.create_content(title="merge one", content="body one", project_id=project_id)
    second = content_service.create_content(title="merge two", content="body two", project_id=project_id)
    content_service.update_content(second["id"], content="body two v2", expected_version=1)

    response = client.post("/api/content/merge", json={
        "ids": [first["id"], second["id"]], "title": "merged", "content": "combined",
        "project_id": project_id,
        "expected_versions": {first["id"]: 1, second["id"]: 1},
    })
    assert response.status_code == 409
    assert client.get(f"/api/content/{first['id']}").json()["status"] == "active"
    assert client.get(f"/api/content/{second['id']}").json()["status"] == "active"


def test_import_route_offloads_document_parser_from_event_loop(monkeypatch):
    from app.services import bounded_import
    import threading

    app = FastAPI()
    app.include_router(content_router)
    parser_thread_ids: list[int] = []
    parser_started, parser_finished, allow_finish = threading.Event(), threading.Event(), threading.Event()

    def fake_import(*, file_bytes: bytes, filename: str, project_id: str | None = None):
        parser_thread_ids.append(threading.get_ident())
        parser_started.set()
        # A deadline bounds a broken implementation; it is not a latency target.
        allow_finish.wait(timeout=5)
        parser_finished.set()
        return {"id": "synthetic", "project_id": None, "kind": "document", "category": "knowledge",
                "title": filename, "content": file_bytes.decode(), "created_at": "2026-10-24",
                "updated_at": "2026-10-24"}

    monkeypatch.setattr(bounded_import.importer_service, "import_document", fake_import)

    @app.get('/health')
    async def health():
        return {"ok": True, "parser_waiting": parser_started.is_set() and not parser_finished.is_set()}

    async def check_concurrency():
        import httpx

        async def wait_for_parser():
            while not parser_started.is_set():
                await asyncio.sleep(.001)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            upload = asyncio.create_task(client.post("/api/import", files={"file": ("synthetic.txt", b"small", "text/plain")}))
            try:
                await asyncio.wait_for(wait_for_parser(), timeout=5)
                assert not parser_finished.is_set(), 'Parser finished before the request loop could proceed'
                response = await asyncio.wait_for(client.get('/health'), timeout=5)
                assert response.status_code == 200 and response.json()['parser_waiting'] is True
                assert not upload.done(), 'Health must complete while parsing is still blocked'
            finally:
                allow_finish.set()
                result = await asyncio.wait_for(upload, timeout=5)
            assert result.status_code == 200

    asyncio.run(check_concurrency())
    assert parser_thread_ids and parser_thread_ids[0] != threading.get_ident()


def test_upload_limit_returns_413_before_parser(monkeypatch):
    from app.services import bounded_import, importer_service

    app = FastAPI()
    app.include_router(content_router)
    monkeypatch.setattr(importer_service, "MAX_UPLOAD_BYTES", 4)
    parser_called = False

    def fake_import(**_kwargs):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("oversized input must be rejected before parser dispatch")

    monkeypatch.setattr(bounded_import.importer_service, "import_document", fake_import)
    with TestClient(app) as client:
        response = client.post("/api/import", files={"file": ("synthetic.txt", b"12345", "text/plain")})
    assert response.status_code == 413
    assert parser_called is False


def test_import_capacity_covers_buffer_readers_and_cancelled_workers(monkeypatch):
    import threading
    from app.services import bounded_import

    monkeypatch.setattr(bounded_import.importer_service, "MAX_IMPORT_CONCURRENCY", 1)
    parser_started = threading.Event()
    release_parser = threading.Event()

    def fake_import(**_kwargs):
        parser_started.set()
        release_parser.wait(timeout=2)
        return {"ok": True}

    monkeypatch.setattr(bounded_import.importer_service, "import_document", fake_import)

    class SyntheticUpload:
        size = None

        def __init__(self):
            self.read_calls = 0

        async def read(self, _size):
            self.read_calls += 1
            return b"synthetic" if self.read_calls == 1 else b""

    first = SyntheticUpload()
    second = SyntheticUpload()

    async def run():
        task_one = asyncio.create_task(bounded_import.import_upload(first, "one.txt"))
        assert await asyncio.to_thread(parser_started.wait, 1)
        task_two = asyncio.create_task(bounded_import.import_upload(second, "two.txt"))
        await asyncio.sleep(0.05)
        assert second.read_calls == 0, "waiting upload must not allocate its in-memory buffer"
        task_one.cancel()
        await asyncio.sleep(0.05)
        assert second.read_calls == 0, "cancelled parser must retain its capacity token until its thread exits"
        release_parser.set()
        outcomes = await asyncio.gather(task_one, task_two, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert outcomes[1] == {"ok": True}

    asyncio.run(run())
    assert first.read_calls == 2 and second.read_calls == 2


def test_docx_expansion_and_extracted_text_are_bounded(tmp_path):
    import zipfile
    from fastapi import HTTPException
    from app.services import importer_service

    bomb_path = tmp_path / "synthetic-expansion.docx"
    with zipfile.ZipFile(bomb_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * 4096)
    with pytest.raises(HTTPException) as expanded:
        importer_service.parse_docx(bomb_path.read_bytes(), max_expanded_bytes=1024)
    assert expanded.value.status_code == 413

    from docx import Document
    doc = Document()
    doc.add_paragraph("synthetic oversized extracted text")
    input_path = tmp_path / "synthetic-text.docx"
    doc.save(input_path)
    with pytest.raises(HTTPException) as extracted:
        importer_service.parse_docx(input_path.read_bytes(), max_extracted_bytes=8)
    assert extracted.value.status_code == 413


def test_pdf_decompression_limits_are_context_local_under_parallel_parses(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import HTTPException
    import pypdf
    from pypdf import PdfWriter
    from app.services import importer_service

    pdf_bytes = bytearray()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    import io
    output = io.BytesIO()
    writer.write(output)
    pdf_bytes.extend(output.getvalue())

    original_apply = pypdf.apply_configuration
    configured_limits: list[int] = []

    @contextmanager
    def tracking_apply(**overwrites):
        with original_apply(**overwrites):
            configured_limits.append(pypdf.get_configuration().zlib_maximum_output_length)
            yield

    monkeypatch.setattr(pypdf, "apply_configuration", tracking_apply)
    default_limit = pypdf.get_configuration().zlib_maximum_output_length

    def parse_with_limit(maximum: int):
        try:
            importer_service.parse_pdf(pdf_bytes, max_extracted_bytes=maximum)
        except HTTPException as exc:
            assert exc.status_code == 422
        assert pypdf.get_configuration().zlib_maximum_output_length == default_limit

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(parse_with_limit, (1024, 2048)))
    assert sorted(configured_limits) == [1024, 2048]


def test_cold_retrieval_index_has_bounded_python_peak(tmp_path, monkeypatch):
    import tracemalloc

    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "retrieval-memory.sqlite"))
    create_schema()
    body = "large synthetic retrieval corpus " * 2048
    for i in range(256):
        _new_content(f"retrieval-{i:03}", body)

    tracemalloc.start()
    retrieval_service.search("synthetic", limit=5)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 12 * 1024 * 1024, f"cold index peak was {peak / (1024 * 1024):.2f} MiB"

    traces: list[str] = []

    @contextmanager
    def traced_db():
        with db() as conn:
            conn.set_trace_callback(traces.append)
            yield conn

    monkeypatch.setattr(retrieval_service, "db", traced_db)
    traces.clear()
    result = retrieval_service.search("synthetic", limit=5)
    body_reads = [sql for sql in traces if sql.lstrip().upper().startswith("SELECT * FROM CONTENTS")]
    assert len(result["items"]) == 5
    assert len(body_reads) <= 5, f"warm search fetched {len(body_reads)} full source bodies for 5 results"


def test_batch_retag_and_recategorize_cas_and_missing_ids(client):
    item1 = _new_content("retag item 1", "body 1", ["alpha"])
    item2 = _new_content("retag item 2", "body 2", ["alpha"])

    # 1. Missing ID in retag must return 404, not silently continue
    res = client.post("/api/content/batch", json={
        "ids": [item1["id"], "cnt_nonexistent"],
        "action": "retag",
        "tags": ["beta"],
    })
    assert res.status_code == 404

    # 2. Key-set mismatch for expected_versions in retag must return 422
    res = client.post("/api/content/batch", json={
        "ids": [item1["id"], item2["id"]],
        "action": "retag",
        "tags": ["beta"],
        "expected_versions": {item1["id"]: 1},
    })
    assert res.status_code == 422

    # 3. Version conflict in retag must return 409 and not modify any row
    res = client.post("/api/content/batch", json={
        "ids": [item1["id"], item2["id"]],
        "action": "retag",
        "tags": ["beta"],
        "expected_versions": {item1["id"]: 1, item2["id"]: 99},
    })
    assert res.status_code == 409
    assert client.get(f"/api/content/{item1['id']}").json()["tags"] == ["alpha"]
    assert client.get(f"/api/content/{item2['id']}").json()["tags"] == ["alpha"]

    # 4. Successful retag with expected_versions CAS increments version
    res = client.post("/api/content/batch", json={
        "ids": [item1["id"], item2["id"]],
        "action": "retag",
        "tags": ["beta"],
        "expected_versions": {item1["id"]: 1, item2["id"]: 1},
    })
    assert res.status_code == 200
    assert res.json()["count"] == 2
    c1 = client.get(f"/api/content/{item1['id']}").json()
    c2 = client.get(f"/api/content/{item2['id']}").json()
    assert c1["tags"] == ["beta"] and c1["version"] == 2
    assert c2["tags"] == ["beta"] and c2["version"] == 2

    # 5. Missing ID in recategorize must return 404
    res = client.post("/api/content/batch", json={
        "ids": [item1["id"], "cnt_nonexistent"],
        "action": "recategorize",
        "category": "decision",
    })
    assert res.status_code == 404

    # 6. Version conflict in recategorize must return 409
    res = client.post("/api/content/batch", json={
        "ids": [item1["id"], item2["id"]],
        "action": "recategorize",
        "category": "decision",
        "expected_versions": {item1["id"]: 2, item2["id"]: 1},
    })
    assert res.status_code == 409

    # 7. Successful recategorize with expected_versions CAS increments version
    res = client.post("/api/content/batch", json={
        "ids": [item1["id"], item2["id"]],
        "action": "recategorize",
        "category": "decision",
        "expected_versions": {item1["id"]: 2, item2["id"]: 2},
    })
    assert res.status_code == 200
    assert res.json()["count"] == 2
    c1 = client.get(f"/api/content/{item1['id']}").json()
    c2 = client.get(f"/api/content/{item2['id']}").json()
    assert c1["category"] == "decision" and c1["version"] == 3
    assert c2["category"] == "decision" and c2["version"] == 3


def test_create_and_merge_content_project_lookup_candidates(client):
    # create_content with bogus project_id must return 404
    res = client.post("/api/content", json={
        "title": "Invalid Proj Item",
        "content": "some text",
        "project_id": "proj_nonexistent_xyz",
    })
    assert res.status_code == 404
    assert "Target project 'proj_nonexistent_xyz' not found" in res.json()["detail"]

    # merge_content with bogus project_id must return 404
    item1 = _new_content("Merge A", "text A")
    item2 = _new_content("Merge B", "text B")
    res = client.post("/api/content/merge", json={
        "ids": [item1["id"], item2["id"]],
        "expected_versions": {item1["id"]: item1["version"], item2["id"]: item2["version"]},
        "title": "Merged Title",
        "content": "Merged text",
        "project_id": "proj_nonexistent_abc",
    })
    assert res.status_code == 404
    assert "Target project 'proj_nonexistent_abc' not found" in res.json()["detail"]


def test_content_like_escaping_special_chars(client):
    item1 = _new_content("Discount 100% Promo", "body with 100% rate")
    item2 = _new_content("Discount 1000 Promo", "body with 1000 rate")

    # Search '%' should match only item1
    res = client.get("/api/content?query=%")
    assert res.status_code == 200
    matched = [it["id"] for it in res.json()]
    assert item1["id"] in matched
    assert item2["id"] not in matched

    # Search '_' should match exact underscore
    item3 = _new_content("prefix_suffix", "body with underscore")
    item4 = _new_content("prefixXsuffix", "body with X")
    res_under = client.get("/api/content?query=_")
    assert res_under.status_code == 200
    matched_under = [it["id"] for it in res_under.json()]
    assert item3["id"] in matched_under
    assert item4["id"] not in matched_under
