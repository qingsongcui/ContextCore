import os
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as tmpdir:
    db_file = str(Path(tmpdir) / "contextcore_smoke.db")
    os.environ["CONTEXTCORE_DB"] = db_file

    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import create_schema, seed_data

    create_schema()
    seed_data()

    client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))

    # 1. Health
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    # 2. Session / CSRF
    sess_res = client.get("/api/session")
    assert sess_res.status_code == 200, sess_res.text
    csrf_token = sess_res.json()["csrf_token"]
    assert csrf_token
    session_cookie = sess_res.cookies.get("cc_session")
    headers = {"X-CSRF-Token": csrf_token}

    # 3. Status
    status_res = client.get("/api/status")
    assert status_res.status_code == 200, status_res.text
    status_data = status_res.json()
    assert status_data["ok"] is True
    assert "counts" in status_data
    assert "service" in status_data

    # 4. Project creation
    proj_res = client.post(
        "/api/projects",
        json={"name": "Smoke Project", "description": "Smoke test project"},
        headers=headers,
    )
    assert proj_res.status_code == 200, proj_res.text
    project = proj_res.json()
    assert project["name"] == "Smoke Project"
    project_id = project["id"]

    # 5. Content creation (memory)
    cnt_res = client.post(
        "/api/content",
        json={
            "project_id": project_id,
            "kind": "memory",
            "category": "decision",
            "title": "Smoke Decision",
            "content": "Use FastAPI and SQLite for local backend",
            "tags": ["architecture"],
        },
        headers=headers,
    )
    assert cnt_res.status_code == 200, cnt_res.text
    content_item = cnt_res.json()
    assert content_item["version"] == 1
    content_id = content_item["id"]

    # 6. Content update with optimistic concurrency
    update_res = client.patch(
        f"/api/content/{content_id}",
        json={
            "content": "Updated decision text",
            "expected_version": 1,
        },
        headers=headers,
    )
    assert update_res.status_code == 200, update_res.text
    updated_item = update_res.json()
    assert updated_item["version"] == 2
    assert updated_item["content"] == "Updated decision text"

    # Stale version update must fail with 409
    stale_res = client.patch(
        f"/api/content/{content_id}",
        json={
            "content": "Stale update",
            "expected_version": 1,
        },
        headers=headers,
    )
    assert stale_res.status_code == 409

    # 7. Version restore
    restore_res = client.post(
        f"/api/content/{content_id}/restore",
        json={"version": 1, "expected_version": 2},
        headers=headers,
    )
    assert restore_res.status_code == 200, restore_res.text
    restored = restore_res.json()
    assert restored["version"] == 3
    assert restored["content"] == "Use FastAPI and SQLite for local backend"

    # 8. Connections & Setup
    conns_res = client.get("/api/connections")
    assert conns_res.status_code == 200, conns_res.text
    conns = conns_res.json()
    from app.integrations.constants import SUPPORTED_HOSTS
    assert len(conns) == len(SUPPORTED_HOSTS)

    setup_res = client.get("/api/connections/codex/setup")
    assert setup_res.status_code == 200, setup_res.text
    setup_data = setup_res.json()
    assert "config_preview" in setup_data

    # 9. Handoff creation and explicit confirmation
    handoff_res = client.post(
        "/api/handoffs",
        json={"project_id": project_id, "target_agent": "codex", "query": "Continue smoke work"},
        headers=headers,
    )
    assert handoff_res.status_code == 200, handoff_res.text
    handoff = handoff_res.json()
    assert handoff["status"] == "awaiting_confirmation"
    assert "goal" in handoff["card"]

    # Web confirmation route is removed (404)
    confirm_web = client.post(
        f"/api/handoffs/{handoff['id']}/confirm",
        json={"confirmed": True},
        headers=headers,
    )
    assert confirm_web.status_code == 404

    # Confirmation must occur via target-MCP identity
    from app.security import authenticated_agent
    from app import mcp_server
    tok = authenticated_agent.set("codex")
    try:
        conf_res = mcp_server.confirm_handoff(handoff["id"], confirmed=True)
        assert conf_res["ok"] is True
        assert conf_res["status"] == "confirmed"
    finally:
        authenticated_agent.reset(tok)
    # 10. Deletion and Tombstones
    del_res = client.delete(f"/api/content/{content_id}", headers=headers)
    assert del_res.status_code == 200, del_res.text
    assert del_res.json()["ok"] is True

    # Re-inserting exact tombstoned content without confirm_recreate fails with 410
    recreate_res = client.post(
        "/api/content",
        json={
            "project_id": project_id,
            "kind": "memory",
            "category": "decision",
            "title": "Smoke Decision",
            "content": "Use FastAPI and SQLite for local backend",
        },
        headers=headers,
    )
    assert recreate_res.status_code == 410

    # 11. Audit logs
    audit_res = client.get("/api/audit")
    assert audit_res.status_code == 200, audit_res.text
    assert len(audit_res.json()) > 0

    print("All ContextCore smoke tests passed successfully.")
