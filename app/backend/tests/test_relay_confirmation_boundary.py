"""Relay preview cannot replace explicit target-Agent confirmation."""
import pytest
from fastapi.testclient import TestClient

from app import mcp_server
from app.database import create_schema, db
from app.main import app
from app.security import authenticated_agent
from app.services import content_service, handoff_service, project_service


@pytest.fixture
def pending():
    create_schema()
    project = project_service.create_project(name="Synthetic relay authorization")
    task = content_service.create_content(
        title="Implement selected task", content="Continue the synthetic implementation",
        category="task", project_id=project["id"], agent="codex",
    )
    card = handoff_service.create_handoff(
        project_id=project["id"], target_agent="omp", task_id=task["id"],
        source_agent="codex", actor="codex",
    )
    return project, task, card


def test_web_relay_retries_cannot_impersonate_target(pending):
    project, task, card = pending
    with TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000)) as client:
        headers = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
        request = {"project_id": project["id"], "task_id": task["id"],
                   "consumer": "omp", "max_tokens": 32000, "actor": "omp", "confirmed": True, "sync_first": False}
        for _ in range(2):
            response = client.post("/api/handoffs/relay", json=request, headers=headers)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["handoff"]["status"] == "awaiting_confirmation"
            assert result["continuation"]["must_not_execute"] is True
            assert result["continuation"]["resume_from"] is None
        for value in (True, False):
            response = client.post("/api/handoffs/relay", json={**request, "auto_confirm": value}, headers=headers)
            assert response.status_code == 422
    assert handoff_service.get_handoff(card["id"])["status"] == "awaiting_confirmation"
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='handoff.confirm'").fetchone()[0] == 0


def test_mcp_relay_requires_explicit_confirmation_by_target(pending):
    project, task, card = pending
    token = authenticated_agent.set("omp")
    try:
        result = mcp_server.relay_project(project_id=project["id"], task_id=task["id"], max_tokens=32000)
        assert result["continuation"]["must_not_execute"] is True
        assert mcp_server.confirm_handoff(card["id"], confirmed=False)["ok"] is False
    finally:
        authenticated_agent.reset(token)
    token = authenticated_agent.set("cursor")
    try:
        assert mcp_server.confirm_handoff(card["id"], confirmed=True)["ok"] is False
    finally:
        authenticated_agent.reset(token)
    token = authenticated_agent.set("omp")
    try:
        result = mcp_server.confirm_handoff(card["id"], confirmed=True)
        assert result["ok"] is True
        assert result["continuation"]["must_not_execute"] is False
        assert result["continuation"]["resume_from"]
    finally:
        authenticated_agent.reset(token)
    with db() as conn:
        rows = conn.execute("SELECT actor FROM audit_logs WHERE action='handoff.confirm'").fetchall()
        assert [row["actor"] for row in rows] == ["omp"]


def test_completed_task_is_not_an_open_selection(pending):
    from fastapi import HTTPException
    project, task_a, card_a = pending
    completed = content_service.create_content(
        title="Completed task", content="Do not resume this", category="task",
        project_id=project["id"], agent="codex", tags=["status:completed"],
    )
    with pytest.raises(HTTPException) as exc:
        handoff_service.create_handoff(
            project_id=project["id"], target_agent="omp", task_id=completed["id"],
            source_agent="codex", actor="omp",
        )
    assert exc.value.status_code == 422
    assert handoff_service.get_handoff(card_a["id"])["status"] == "awaiting_confirmation"
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1
    kept = handoff_service.create_handoff(
        project_id=project["id"], target_agent="omp", task_id=task_a["id"], source_agent="codex", actor="omp",
    )
    assert completed["id"] not in (kept.get("source_ids") or [])


def test_explicit_task_cannot_reuse_other_task_card(pending):
    project, task_a, card_a = pending
    task_b = content_service.create_content(
        title="Task B", content="B must not resume A", category="task",
        project_id=project["id"], agent="codex",
    )
    result = handoff_service.relay_project(
        project_id=project["id"], consumer="omp", task_id=task_b["id"], sync_first=False, max_tokens=32000,
    )
    assert result["handoff_id"] != card_a["id"]
    assert result["continuation"]["selected_task_id"] == task_b["id"]
    assert "Task B" in result["card"]["goal"]
    assert result["continuation"]["must_not_execute"] is True
    ambiguous = handoff_service.relay_project(project_id=project["id"], consumer="omp", sync_first=False)
    assert ambiguous["status"] == "selection_required"
    assert {c["id"] for c in ambiguous["candidates"]} == {task_a["id"], task_b["id"]}


@pytest.mark.parametrize("selection", ["missing-task", "foreign-task", "missing-conversation", "foreign-conversation"])
def test_invalid_explicit_selection_never_falls_back_to_existing_card(pending, selection):
    from fastapi import HTTPException
    from app.services import conversation_service
    project, _, card = pending
    other = project_service.create_project(name="Other project")
    task = content_service.create_content(title="Foreign task", content="Other work", category="task",
                                          project_id=other["id"], agent="codex")
    conv = conversation_service.upsert_conversation(agent="codex", project_id=other["id"], title="Other",
                                                    external_id="synthetic-other", messages=[])
    selector = {
        "missing-task": {"task_id": "missing"}, "foreign-task": {"task_id": task["id"]},
        "missing-conversation": {"conversation_id": "missing"}, "foreign-conversation": {"conversation_id": conv["id"]},
    }[selection]
    with pytest.raises(HTTPException) as exc:
        handoff_service.relay_project(project_id=project["id"], consumer="omp", sync_first=False, **selector)
    assert exc.value.status_code in (404, 422)
    assert handoff_service.get_handoff(card["id"])["status"] == "awaiting_confirmation"


def test_relay_discovery_reports_sync_failure_without_raw_exception(pending, monkeypatch):
    from app.services import connection_service
    def fail():
        raise RuntimeError('synthetic-private-detail-not-for-response')
    monkeypatch.setattr(connection_service, 'list_connections', fail)
    result = handoff_service.discover_relay_projects(sync_first=True)
    assert result['sync_failed'] is True
    assert result['synced_agents'][0]['status'] == 'error'
    assert 'synthetic-private-detail' not in str(result)
    assert result['candidates']


def test_service_confirmation_enforces_target_and_strict_boolean(pending):
    from fastapi import HTTPException
    _, _, card = pending
    for actor, confirmed, status in [('user', True, 403), ('codex', True, 403), ('omp', 'true', 422), ('omp', 1, 422)]:
        with pytest.raises(HTTPException) as failure:
            handoff_service.confirm_handoff(card['id'], confirmed=confirmed, actor=actor)
        assert failure.value.status_code == status
    assert handoff_service.get_handoff(card['id'])['status'] == 'awaiting_confirmation'
    assert handoff_service.confirm_handoff(card['id'], confirmed=True, actor='omp')['status'] == 'confirmed'


def test_legacy_confirmation_requires_review_without_rewriting_history(pending):
    project, task, card = pending
    with db() as conn:
        conn.execute("UPDATE handoffs SET status='confirmed', confirmed_at='2026-01-01' WHERE id=?", (card['id'],))
    reviewed = handoff_service.get_handoff(card['id'])
    assert reviewed['status'] == 'awaiting_confirmation'
    assert reviewed['confirmation_review_required'] is True
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.handoffs import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get('/api/handoffs/' + card['id'])
        assert response.status_code == 200
        assert response.json()['confirmation_review_required'] is True
        assert response.json()['status'] == 'awaiting_confirmation'
    assert handoff_service.continuation_contract(reviewed, 'omp')['must_not_execute'] is True
    with db() as conn:
        assert conn.execute('SELECT status FROM handoffs WHERE id=?', (card['id'],)).fetchone()[0] == 'confirmed'
    verified = handoff_service.confirm_handoff(card['id'], confirmed=True, actor='omp')
    assert handoff_service.continuation_contract(verified, 'omp')['status'] == 'ready'
    with db() as conn:
        conn.execute('DELETE FROM audit_logs WHERE id=?', (verified['confirmation']['audit_id'],))
    assert handoff_service.continuation_contract(verified, 'omp')['status'] == 'confirmation_review_required'
