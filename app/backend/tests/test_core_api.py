import io
import json
import os
import sys
import tempfile
import pytest
from pathlib import Path
from fastapi.testclient import TestClient


@pytest.fixture
def client_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_contextcore.db")
        os.environ["CONTEXTCORE_DB"] = db_path

        from app.database import create_schema, seed_data
        from app.main import app

        create_schema()
        seed_data()

        client = TestClient(
            app,
            base_url="http://127.0.0.1:8420",
            client=("127.0.0.1", 50000),
        )

        # Obtain valid session & CSRF token
        sess_res = client.get("/api/session")
        assert sess_res.status_code == 200
        csrf_token = sess_res.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        yield client, headers, db_path


def test_health_and_status(client_env):
    client, headers, _ = client_env
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["ok"] is True

    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert "counts" in data
    assert "service" in data


def test_web_handoff_ambiguity_preserves_candidates_and_accepts_explicit_selection(client_env):
    client, headers, _ = client_env
    project = client.post("/api/projects", headers=headers, json={"name": "Web handoff selection"}).json()
    tasks = []
    for title in ("Repair deletion", "Verify desktop recovery"):
        response = client.post("/api/content", headers=headers, json={
            "project_id": project["id"], "kind": "memory", "category": "task",
            "title": title, "content": "Pending synthetic work", "tags": ["status:pending"],
        })
        assert response.status_code == 200
        tasks.append(response.json())
    body = {"project_id": project["id"], "target_agent": "claude-code"}
    response = client.post("/api/handoffs", headers=headers, json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["code"] == "ambiguous_task"
    assert {item["id"] for item in detail["candidates"]} == {item["id"] for item in tasks}
    assert client.get("/api/handoffs").json() == []
    selected = client.post("/api/handoffs", headers=headers, json={**body, "task_id": tasks[1]["id"]})
    assert selected.status_code == 200, selected.text
    card = selected.json()
    assert card["status"] == "awaiting_confirmation"
    assert tasks[1]["id"] in card["source_ids"]
    assert tasks[1]["title"] in str(card["card"]["goal"])


def test_project_crud_and_merge_split(client_env):
    client, headers, _ = client_env

    # 1. Create project
    p1 = client.post("/api/projects", json={"name": "Project Alpha", "description": "Alpha desc"}, headers=headers).json()
    assert p1["name"] == "Project Alpha"
    p1_id = p1["id"]

    p2 = client.post("/api/projects", json={"name": "Project Beta", "description": "Beta desc"}, headers=headers).json()
    p2_id = p2["id"]

    # 2. Add content to p1
    c1 = client.post(
        "/api/content",
        json={"project_id": p1_id, "kind": "memory", "category": "decision", "title": "Alpha Decision", "content": "Text A"},
        headers=headers,
    ).json()

    # 3. Merge projects: non-existent source must 404
    err_merge = client.post("/api/projects/merge", json={"source_ids": ["non_existent_id"], "target_id": p2_id}, headers=headers)
    assert err_merge.status_code == 404

    # Valid merge
    merged = client.post("/api/projects/merge", json={"source_ids": [p1_id], "target_id": p2_id}, headers=headers).json()
    assert merged["id"] == p2_id
    # Content moved to p2
    updated_c1 = client.get(f"/api/content/{c1['id']}").json()
    assert updated_c1["project_id"] == p2_id

    # 4. Split project: non-existent content must 404
    err_split = client.post(
        f"/api/projects/{p2_id}/split",
        json={"name": "Project Gamma", "content_ids": ["invalid_cnt_id"]},
        headers=headers,
    )
    assert err_split.status_code == 404

    # Valid split
    split_p = client.post(
        f"/api/projects/{p2_id}/split",
        json={"name": "Project Gamma", "content_ids": [c1["id"]]},
        headers=headers,
    ).json()
    assert split_p["name"] == "Project Gamma"
    c1_after_split = client.get(f"/api/content/{c1['id']}").json()
    assert c1_after_split["project_id"] == split_p["id"]


def test_content_optimistic_concurrency_and_restore(client_env):
    client, headers, _ = client_env

    proj = client.post("/api/projects", json={"name": "P-Concurrency"}, headers=headers).json()
    item = client.post(
        "/api/content",
        json={"project_id": proj["id"], "kind": "memory", "category": "knowledge", "title": "Arch Doc", "content": "v1 content"},
        headers=headers,
    ).json()
    assert item["version"] == 1

    # Missing expected_version must fail validation
    err_patch = client.patch(f"/api/content/{item['id']}", json={"content": "v2"}, headers=headers)
    assert err_patch.status_code == 422

    # Wrong expected_version must return 409 Conflict
    conflict_patch = client.patch(f"/api/content/{item['id']}", json={"content": "v2", "expected_version": 99}, headers=headers)
    assert conflict_patch.status_code == 409

    # Valid update
    updated = client.patch(f"/api/content/{item['id']}", json={"content": "v2 content", "expected_version": 1}, headers=headers).json()
    assert updated["version"] == 2
    assert updated["content"] == "v2 content"

    # Restore v1
    restored = client.post(
        f"/api/content/{item['id']}/restore",
        json={"version": 1, "expected_version": 2},
        headers=headers,
    ).json()
    assert restored["version"] == 3
    assert restored["content"] == "v1 content"



def test_deleted_content_cannot_be_recreated(client_env):
    client, headers, _ = client_env
    project = client.post("/api/projects", json={"name": "P-Delete"}, headers=headers).json()
    created = client.post(
        "/api/content",
        json={"project_id": project["id"], "kind": "memory", "category": "lesson", "title": "不要复活", "content": "删除后必须保持删除"},
        headers=headers,
    ).json()
    deleted = client.delete(f"/api/content/{created['id']}?expected_version={created['version']}", headers=headers)
    assert deleted.status_code == 200
    recreated = client.post(
        "/api/content",
        json={"project_id": project["id"], "kind": "memory", "category": "lesson", "title": "不要复活", "content": "删除后必须保持删除"},
        headers=headers,
    )
    assert recreated.status_code == 410
    audit = client.get("/api/audit").json()
    assert any(item["action"] == "content.delete" and item["target"] == created["id"] for item in audit)
    assert all("删除后必须保持删除" not in json.dumps(item, ensure_ascii=False) for item in audit)

def test_content_batch_and_merge(client_env):
    client, headers, _ = client_env

    proj = client.post("/api/projects", json={"name": "P-Batch"}, headers=headers).json()
    c1 = client.post(
        "/api/content",
        json={"project_id": proj["id"], "kind": "memory", "category": "background", "title": "Doc 1", "content": "Content 1"},
        headers=headers,
    ).json()
    c2 = client.post(
        "/api/content",
        json={"project_id": proj["id"], "kind": "memory", "category": "background", "title": "Doc 2", "content": "Content 2"},
        headers=headers,
    ).json()

    # Batch retag
    batch_res = client.post(
        "/api/content/batch",
        json={"ids": [c1["id"], c2["id"]], "action": "retag", "tags": ["tagged"]},
        headers=headers,
    ).json()
    assert batch_res["ok"] is True
    assert batch_res["count"] == 2

    # Merge content: non-existent id must 404
    err_merge = client.post(
        "/api/content/merge",
        json={"ids": [c1["id"], "fake_id"], "title": "Merged", "content": "Combined", "project_id": proj["id"],
              "expected_versions": {c1["id"]: 2, "fake_id": 1}},
        headers=headers,
    )
    assert err_merge.status_code == 404

    # Valid merge
    merged = client.post(
        "/api/content/merge",
        json={"ids": [c1["id"], c2["id"]], "title": "Merged Doc", "content": "Combined Text", "project_id": proj["id"],
              "expected_versions": {c1["id"]: 2, c2["id"]: 2}},
        headers=headers,
    ).json()
    assert merged["title"] == "Merged Doc"
    assert set(merged["related_ids"]) == {c1["id"], c2["id"]}
    assert client.get(f"/api/content/{c1['id']}", headers=headers).json()["status"] == "historical"


def test_document_import_formats(client_env):
    client, headers, _ = client_env

    proj = client.post("/api/projects", json={"name": "P-Import"}, headers=headers).json()

    # 1. Text / MD
    md_file = io.BytesIO(b"# Hello Markdown\nThis is content.")
    md_res = client.post(
        "/api/import",
        files={"file": ("notes.md", md_file, "text/markdown")},
        data={"project_id": proj["id"]},
        headers=headers,
    )
    assert md_res.status_code == 200
    assert md_res.json()["title"] == "notes.md"

    # 2. Valid JSON
    json_file = io.BytesIO(b'{"key": "value", "list": [1, 2, 3]}')
    json_res = client.post(
        "/api/import",
        files={"file": ("config.json", json_file, "application/json")},
        data={"project_id": proj["id"]},
        headers=headers,
    )
    assert json_res.status_code == 200

    # 3. Invalid JSON must fail with 422
    bad_json = io.BytesIO(b'{"key": incomplete')
    bad_res = client.post(
        "/api/import",
        files={"file": ("bad.json", bad_json, "application/json")},
        data={"project_id": proj["id"]},
        headers=headers,
    )
    assert bad_res.status_code == 422
    assert "Invalid JSON" in bad_res.json()["detail"]


def test_conversations_idempotency_and_tombstone(client_env):
    client, headers, _ = client_env

    # 1. Create conversation with external_id
    conv = client.post(
        "/api/conversations",
        json={
            "agent": "omp",
            "title": "OMP Session 1",
            "source_path": "/tmp/omp1.jsonl",
            "messages": [{"id": "m1", "role": "user", "content": "Hello", "timestamp": "2026-10-24T00:00:00Z"}],
            "process_summary": "已添加登录页面与路由配置",
        },
        headers=headers,
    ).json()
    assert conv["process_summary"] == "已添加登录页面与路由配置"
    conv_id = conv["id"]

    # 2. Delete conversation
    del_res = client.delete(f"/api/conversations/{conv_id}", headers=headers)
    assert del_res.status_code == 200

    # 3. Re-creating exact same conversation triggers tombstone 410
    recreate = client.post(
        "/api/conversations",
        json={
            "agent": "omp",
            "title": "OMP Session 1",
            "source_path": "/tmp/omp1.jsonl",
        },
        headers=headers,
    )
    assert recreate.status_code == 410


def test_handoff_validation_and_confirmation(client_env):
    client, headers, _ = client_env

    proj = client.post("/api/projects", json={"name": "P-Handoff"}, headers=headers).json()
    task = client.post(
        "/api/content",
        json={"project_id": proj["id"], "kind": "memory", "category": "task", "title": "Build feature X", "content": "Full detail of X"},
        headers=headers,
    ).json()

    # Reject invalid target_agent
    err_target = client.post(
        "/api/handoffs",
        json={"project_id": proj["id"], "target_agent": "unknown_bot", "task_id": task["id"]},
        headers=headers,
    )
    assert err_target.status_code == 422

    # Valid handoff creation
    handoff = client.post(
        "/api/handoffs",
        json={"project_id": proj["id"], "target_agent": "codex", "task_id": task["id"]},
        headers=headers,
    ).json()
    assert handoff["status"] == "awaiting_confirmation"
    assert "Build feature X" in handoff["card"]["goal"]
    assert handoff["card"]["evidence"] != ""

    # Delete the source task
    client.delete(f"/api/content/{task['id']}?expected_version={task['version']}", headers=headers)

    # Confirming handoff via web route is rejected with 404 (web confirm route removed)
    confirm_web = client.post(
        f"/api/handoffs/{handoff['id']}/confirm",
        json={"confirmed": True},
        headers=headers,
    )
    assert confirm_web.status_code == 404

    # Confirming handoff whose source item was deleted via target-MCP must fail
    from app.security import authenticated_agent
    from app import mcp_server
    tok = authenticated_agent.set("codex")
    try:
        import pytest
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            mcp_server.confirm_handoff(handoff["id"], confirmed=True)
        assert exc.value.status_code == 400
    finally:
        authenticated_agent.reset(tok)


def test_conflicts_and_resolution(client_env):
    client, headers, _ = client_env

    proj = client.post("/api/projects", json={"name": "P-Conflict"}, headers=headers).json()
    mem = client.post(
        "/api/content",
        json={"project_id": proj["id"], "kind": "memory", "category": "decision", "title": "Framework", "content": "Use FastAPI"},
        headers=headers,
    ).json()

    conflict = client.post(
        "/api/conflicts",
        json={
            "project_id": proj["id"],
            "existing_id": mem["id"],
            "proposed": {"title": "Framework", "content": "Use Django", "category": "decision"},
            "reason": "Proposed alternative framework",
        },
        headers=headers,
    ).json()
    assert conflict["status"] == "pending"

    # Resolve with 'replace'
    resolved = client.post(
        f"/api/conflicts/{conflict['id']}/resolve",
        json={"resolution": "replace", "expected_version": 1},
        headers=headers,
    ).json()
    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == "replace"

    updated_mem = client.get(f"/api/content/{mem['id']}").json()
    assert updated_mem["content"] == "Use Django"



def test_missing_history_root_is_not_success(client_env):
    client, headers, _ = client_env
    enabled = client.patch(
        "/api/connections/cursor",
        json={"enabled": True, "paths": ["/tmp/contextcore-synthetic-cursor"], "confirmed": True},
        headers=headers,
    )
    assert enabled.status_code == 200
    synced = client.post("/api/connections/cursor/sync", headers=headers)
    assert synced.status_code == 422
    listed = client.get("/api/connections").json()
    cursor = next(item for item in listed if item["id"] == "cursor")
    assert cursor["error"]
    assert cursor["last_sync"] in (None, "")


def test_enabled_omp_sync_ingests_synthetic_session(client_env, tmp_path):
    client, headers, _ = client_env
    session = tmp_path / "session.jsonl"
    rows = [
        {"type": "session", "version": 3, "id": "sess-synth", "cwd": str(tmp_path), "title": "合成同步", "timestamp": "2026-10-24T12:00:00Z"},
        {"type": "message", "id": "m1", "parentId": None, "timestamp": "2026-10-24T12:00:01Z", "message": {"role": "user", "content": "请只保留这句原话。"}},
        {"type": "message", "id": "m2", "parentId": "m1", "timestamp": "2026-10-24T12:00:02Z", "message": {"role": "toolResult", "toolName": "bash", "content": "first line\nSECRET_TOOL_DUMP", "isError": False}},
    ]
    session.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    enabled = client.patch(
        "/api/connections/omp",
        json={"enabled": True, "paths": [str(tmp_path)], "confirmed": True},
        headers=headers,
    )
    assert enabled.status_code == 200
    synced = client.post("/api/connections/omp/sync", headers=headers)
    assert synced.status_code == 200, synced.text
    assert synced.json()["error"] in (None, "")
    conversations = client.get("/api/conversations?summary=false&limit=200").json()
    match = next(item for item in conversations if item["title"] == "合成同步")
    assert any(message["content"] == "请只保留这句原话。" for message in match["messages"])
    assert "SECRET_TOOL_DUMP" not in json.dumps(match["messages"], ensure_ascii=False)
    assert "SECRET_TOOL_DUMP" not in match["process_summary"]
    assert "bash" in match["process_summary"]


def test_settings_reports_actual_autostart_state(client_env):
    client, headers, _ = client_env
    status = client.get("/api/settings")
    assert status.status_code == 200
    body = status.json()
    assert isinstance(body["autostart"], bool)
    assert isinstance(body["autostart_installed"], bool)
    assert body["platform"] == sys.platform

def test_connection_setup_does_not_grant_access(client_env):
    client, headers, _ = client_env
    before = client.get("/api/connections").json()
    response = client.get("/api/connections/codex/setup")
    assert response.status_code == 200
    assert response.json().get("token") is None
    assert client.get("/api/connections").json() == before
    assert all("token_hash" not in item for item in before)


@pytest.mark.parametrize('category', ['preference', 'background', 'decision', 'task', 'lesson', 'knowledge'])
def test_user_and_agent_share_versioned_memory_contract(client_env, category):
    from app.mcp_server import edit_memory, read_memory
    from app.security import authenticated_agent
    client, headers, _ = client_env
    project = client.post('/api/projects', json={'name': 'Synthetic shared maintenance'}, headers=headers).json()
    response = client.post('/api/content', json={'project_id': project['id'], 'kind': 'memory', 'category': category,
        'title': 'Synthetic ' + category, 'content': 'USER_ORIGINAL'}, headers=headers)
    assert response.status_code == 200
    original = response.json()
    identity = authenticated_agent.set('codex')
    try:
        edit_memory(original['id'], expected_version=original['version'], content='AGENT_REVISION')
        via_agent = read_memory(original['id'])
    finally:
        authenticated_agent.reset(identity)
    via_user = client.get('/api/content/' + original['id']).json()
    assert via_user['content'] == via_agent['content'] == 'AGENT_REVISION'
    assert via_user['category'] == category and via_user['version'] == original['version'] + 1
    stale = client.patch('/api/content/' + original['id'], json={'content': 'STALE_USER_EDIT', 'expected_version': original['version']}, headers=headers)
    assert stale.status_code == 409
    restored = client.post('/api/content/' + original['id'] + '/restore', json={'version': original['version'], 'expected_version': via_user['version']}, headers=headers)
    assert restored.status_code == 200
    assert restored.json()['content'] == 'USER_ORIGINAL'
    assert read_memory(original['id'])['version'] == restored.json()['version']


def test_actual_import_files_preserve_extracted_text_and_deduplicate(client_env, tmp_path):
    from docx import Document
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    client, headers, _ = client_env
    project = client.post('/api/projects', json={'name': 'Synthetic file imports'}, headers=headers).json()
    expected = 'SYNTHETIC_IMPORT_BODY'
    paths = []
    for suffix, body in [('md', expected), ('txt', expected), ('json', '{"body":"SYNTHETIC_IMPORT_BODY"}')]:
        path = tmp_path / ('sample.' + suffix)
        path.write_text(body)
        paths.append((path, body))
    doc = Document()
    doc.add_paragraph(expected)
    word = tmp_path / 'sample.docx'
    doc.save(word)
    paths.append((word, expected))
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 10 100 Td (SYNTHETIC_IMPORT_BODY) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    pdf = tmp_path / 'sample.pdf'
    writer.write(pdf)
    paths.append((pdf, expected))
    for path, text in paths:
        def upload():
            return client.post('/api/import', files={'file': (path.name, path.read_bytes())}, data={'project_id': project['id']}, headers=headers)
        first = upload()
        assert first.status_code == 200, first.text
        item = first.json()
        readback = client.get('/api/content/' + item['id']).json()
        assert readback['content'].strip() == text
        assert readback['kind'] == 'document' and readback['project_id'] == project['id']
        duplicate = upload()
        assert duplicate.status_code == 200 and duplicate.json()['id'] == item['id']


@pytest.mark.skipif(not Path('/usr/bin/textutil').exists(), reason='Legacy DOC conversion requires macOS textutil')
def test_real_legacy_doc_import_uses_system_converter(client_env, tmp_path):
    import subprocess
    client, headers, _ = client_env
    text = tmp_path / 'source.txt'
    text.write_text('SYNTHETIC_LEGACY_WORD_BODY')
    document = tmp_path / 'sample.doc'
    subprocess.run(['/usr/bin/textutil', '-convert', 'doc', '-output', str(document), str(text)], check=True, capture_output=True, timeout=15)
    assert document.read_bytes().startswith(bytes.fromhex('d0cf11e0a1b11ae1'))
    response = client.post('/api/import', files={'file': ('sample.doc', document.read_bytes())}, headers=headers)
    assert response.status_code == 200, response.text
    assert client.get('/api/content/' + response.json()['id']).json()['content'].strip() == 'SYNTHETIC_LEGACY_WORD_BODY'


def test_legacy_doc_reports_unavailable_converter(monkeypatch):
    from app.services import importer_service
    from fastapi import HTTPException
    original = importer_service.os.path.exists
    monkeypatch.setattr(importer_service.os.path, 'exists', lambda path: False if path == '/usr/bin/textutil' else original(path))
    with pytest.raises(HTTPException) as failure:
        importer_service.parse_doc(b'synthetic legacy document')
    assert failure.value.status_code == 422 and 'not available' in failure.value.detail
