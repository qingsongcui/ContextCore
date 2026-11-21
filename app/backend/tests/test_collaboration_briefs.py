import pytest
from fastapi.testclient import TestClient

from app.database import create_schema, db, seed_data
from app.main import app


@pytest.fixture
def api():
    create_schema()
    seed_data()
    # These route tests use the schema above; avoid starting/stopping the process-wide SSE service.
    client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
    try:
        client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
        yield client
    finally:
        client.close()


BASE = "/api/workbench/briefs"
PROFILE = "/api/workbench/profile"
CARD = {"revision": 0, "name": "Studio", "bio": "bio not for this offer",
        "assets": [{"id": "offer", "title": "Audit", "excerpt": "A public sample", "selected": True,
                    "notes": "secret note", "steps": "secret procedure"},
                   {"id": "hidden", "title": "private work"}]}
BODY = {"revision": 0, "asset_index": 0, "profile_revision": 1,
        "deliverable": "Draft a report", "terms": "Share anonymous data"}


def test_brief_survives_reopen_edit_conflict_and_deletion(api):
    path = BASE + "/example"
    assert api.put(PROFILE, json=CARD).status_code == 200
    first = api.put(path, json=BODY)
    assert first.status_code == 200, first.text
    assert first.json()["selection"]["title"] == "Audit"
    create_schema()  # Existing installations re-enter schema setup on restart.
    assert api.get(path).json() == first.json()
    assert api.get(BASE).json()["total"] == 1
    changed = api.put(path, json={**BODY, "revision": 1, "terms": "Revised terms"})
    assert changed.json()["revision"] == 2
    assert api.put(path, json={**BODY, "revision": 1}).status_code == 409
    assert api.post(BASE + "/export", json={**BODY, "id": "example", "revision": 1}).status_code == 409
    assert api.delete(path + "?revision=1").status_code == 409
    assert api.delete(path + "?revision=2").status_code == 204
    assert api.get(path).status_code == 404
    assert api.put(path, json={**BODY, "revision": 2}).status_code == 404
    assert api.get(BASE).json()["items"] == []
    with db() as conn:
        audit = [dict(row) for row in conn.execute("SELECT * FROM audit_logs WHERE action LIKE 'brief.%'")]
    assert len(audit) == 3
    assert "anonymous data" not in str(audit)


def test_brief_export_allowlist_and_withdrawn_offer(api):
    api.put(PROFILE, json=CARD).raise_for_status()
    api.put(BASE + "/abc", json=BODY).raise_for_status()
    exported = api.post(BASE + "/export", json={**BODY, "id": "abc", "revision": 1})
    assert exported.json() == {"title": "Audit", "profile_revision": 1, "provider": "Studio",
                               "excerpt": "A public sample", "deliverable": BODY["deliverable"], "terms": BODY["terms"]}
    api.put(PROFILE, json={**CARD, "revision": 1, "assets": []}).raise_for_status()
    stale = api.get(BASE + "/abc").json()
    assert stale["selection_stale"] and stale["selection"] is None
    assert stale["deliverable"] == BODY["deliverable"]
    assert "Audit" not in str(stale)
    for form in (BODY, {**BODY, "profile_revision": 2}):
        assert api.post(BASE + "/export", json=form).status_code == 409
    assert api.put(BASE + "/abc", json={**BODY, "revision": 1, "asset_index": None}).status_code == 200


def test_partial_drafts_pagination_and_input_validation(api):
    for i in range(3):
        assert api.put(BASE + f"/brief-{i}", json={"revision": 0, "terms": "Working terms"}).status_code == 200
    page = api.get(BASE + "?limit=1&offset=1").json()
    assert page["total"] == 3 and len(page["items"]) == 1 and page["offset"] == 1
    assert api.get(BASE + "?limit=1&offset=3").json()["items"] == []
    assert api.put(BASE + "/empty", json={"revision": 0, "terms": "  "}).status_code == 422
    assert api.post(BASE + "/export", json={"revision": 0, "terms": "Working terms"}).status_code == 422
    assert api.put(BASE + "/bad", json={**BODY, "asset_index": -1}).status_code == 422
