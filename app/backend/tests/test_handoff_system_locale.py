"""System handoff labels can change language; source text, card identity and confirmation cannot."""
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import mcp_server
from app.database import create_schema, db, decode
from app.routers.handoffs import router
from app.security import authenticated_agent
from app.services import content_service, conversation_service, handoff_service, project_service
from app.services.handoff_text import render


def _raw_card(handoff_id):
    with db() as conn:
        row = conn.execute("SELECT card, status, confirmed_at FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
    return row["card"], row["status"], row["confirmed_at"]


def _counts():
    with db() as conn:
        cards = conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]
        confirms = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='handoff.confirm'").fetchone()[0]
    return cards, confirms


@pytest.fixture
def card():
    create_schema()
    project = project_service.create_project(name="Locale boundary")
    task = content_service.create_content(
        title="KEEP_TITLE_未记录", content="KEEP_BODY", category="task",
        project_id=project["id"], agent="codex",
    )
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Captured source",
        external_id="locale-source", cwd="/synthetic/locale", source_path="/synthetic/locale/source.jsonl",
        messages=[{"role": "user", "content": "请保留 USER_QUOTE_未记录，不要改写。"}],
    )
    created = handoff_service.create_handoff(
        project_id=project["id"], target_agent="omp", task_id=task["id"],
        conversation_id=conversation["id"], source_agent="codex", actor="codex", language="zh",
    )
    return project, task, created


def test_marked_system_text_translates_without_touching_source_or_storage(card):
    project, task, created = card
    stored, status, confirmed_at = _raw_card(created["id"])
    assert status == "awaiting_confirmation"
    assert confirmed_at is None
    assert created["system_text_language"] == "zh"
    assert "KEEP_TITLE_未记录" in created["card"]["goal"]
    assert "选定任务" in created["card"]["progress"]
    assert "USER_QUOTE_未记录" in created["card"]["decisions"]

    english = handoff_service.get_handoff(created["id"], "en")
    assert english["id"] == created["id"]
    assert english["status"] == "awaiting_confirmation"
    assert english["system_text_language"] == "en"
    assert "KEEP_TITLE_未记录" in english["card"]["goal"]
    assert "KEEP_BODY" in english["card"]["goal"]
    assert "Selected task" in english["card"]["progress"]
    assert "KEEP_TITLE_未记录" in english["card"]["progress"]
    assert "Latest user instruction" in english["card"]["decisions"]
    assert "USER_QUOTE_未记录" in english["card"]["decisions"]
    assert "working directory" in english["card"]["evidence"]
    assert "/synthetic/locale" in english["card"]["evidence"]
    assert "Target assistant" in english["card"]["next_steps"]
    assert "KEEP_TITLE_未记录" in english["card"]["next_steps"]
    assert "选定任务" not in english["card"]["progress"]
    assert _raw_card(created["id"]) == (stored, "awaiting_confirmation", None)
    assert _counts() == (1, 0)

    api = FastAPI()
    api.include_router(router)
    with TestClient(api) as client:
        response = client.get(f"/api/handoffs/{created['id']}?language=en")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == created["id"]
        assert body["system_text_language"] == "en"
        assert "USER_QUOTE_未记录" in body["card"]["decisions"]
        assert body["status"] == "awaiting_confirmation"
    assert _counts() == (1, 0)
    assert project["id"] and task["id"]


def test_language_switch_reuses_the_selected_card_and_cannot_confirm_it(card):
    _, task, created = card
    before = _counts()
    switched = handoff_service.relay_project(
        project_id=created["project_id"], consumer="omp", task_id=task["id"],
        handoff_id=created["id"], language="en", max_tokens=32000, sync_first=False,
    )
    assert switched["status"] == "relayed"
    assert switched["handoff_id"] == created["id"]
    assert switched["handoff"]["status"] == "awaiting_confirmation"
    assert switched["continuation"]["must_not_execute"] is True
    assert switched["continuation"]["resume_from"] is None
    assert "KEEP_TITLE_未记录" in switched["card"]["next_steps"]
    assert "Selected task" in switched["context_text"]
    assert "USER_QUOTE_未记录" in switched["context_text"]
    assert _counts() == before

    other = content_service.create_content(
        title="OTHER_TASK", content="Do not attach this", category="task",
        project_id=created["project_id"], agent="codex",
    )
    with pytest.raises(HTTPException) as failure:
        handoff_service.relay_project(
            project_id=created["project_id"], consumer="omp", task_id=other["id"],
            handoff_id=created["id"], language="zh", max_tokens=32000, sync_first=False,
        )
    assert failure.value.status_code == 409
    assert handoff_service.get_handoff(created["id"])["status"] == "awaiting_confirmation"
    assert _counts() == before


def test_unmarked_or_corrupt_legacy_text_is_not_guessed(card):
    _, _, created = card
    with db() as conn:
        raw = decode(conn.execute("SELECT card FROM handoffs WHERE id=?", (created["id"],)).fetchone()["card"], {})
        legacy = {"goal": "未记录", "progress": "用户原文 LEGACY_KEEP", "decisions": "未记录",
                  "evidence": "现场原文", "blockers": "未记录", "next_steps": "下一步原文"}
        legacy["_system_text"] = {"goal": [[0, 3, "不是原文", "Not recorded"]]}
        conn.execute("UPDATE handoffs SET card=? WHERE id=?", (json.dumps(legacy, ensure_ascii=False), created["id"]))
    shown = handoff_service.get_handoff(created["id"], "en")
    assert shown["system_text_language"] == "en"
    assert shown["card"]["goal"] == "未记录"
    assert shown["card"]["progress"] == "用户原文 LEGACY_KEEP"
    assert shown["card"]["next_steps"] == "下一步原文"
    with db() as conn:
        conn.execute("UPDATE handoffs SET card=? WHERE id=?", (json.dumps({
            "goal": "完全没有标记", "progress": "p", "decisions": "d", "evidence": "e",
            "blockers": "b", "next_steps": "n",
        }, ensure_ascii=False), created["id"]))
    unmarked = handoff_service.get_handoff(created["id"], "en")
    assert unmarked["system_text_language"] == "original"
    assert unmarked["card"]["goal"] == "完全没有标记"
    assert render("未记录", [], "en") == "未记录"


@pytest.mark.parametrize("language", ["zh", "en"])
def test_either_language_budget_refuses_without_source_text_or_confirmation(card, language):
    _, task, created = card
    before = _counts()
    refused = handoff_service.relay_project(
        project_id=created["project_id"], consumer="omp", task_id=task["id"],
        handoff_id=created["id"], language=language, max_tokens=1, sync_first=False,
    )
    encoded = json.dumps(refused, ensure_ascii=False)
    assert refused["ok"] is False
    assert refused["status"] == "insufficient_budget"
    assert refused["must_not_execute"] is True
    assert refused["continuation"]["resume_from"] is None
    assert refused["continuation"]["must_not_execute"] is True
    assert "KEEP_TITLE_未记录" not in encoded
    assert "USER_QUOTE_未记录" not in encoded
    assert ("未输出上下文" in refused["message"]) if language == "zh" else ("No context emitted" in refused["message"])
    assert handoff_service.get_handoff(created["id"])["status"] == "awaiting_confirmation"
    assert _counts() == before


def test_mcp_confirmation_stays_strict_and_legacy_review_survives_translation(card):
    _, _, created = card
    assert mcp_server.confirm_handoff(created["id"], confirmed="true")["ok"] is False
    assert mcp_server.confirm_handoff(created["id"], confirmed=1)["ok"] is False
    assert _counts()[1] == 0
    with db() as conn:
        conn.execute("UPDATE handoffs SET status='confirmed', confirmed_at='2026-01-01' WHERE id=?", (created["id"],))
    reviewed = handoff_service.get_handoff(created["id"], "en")
    assert reviewed["status"] == "awaiting_confirmation"
    assert reviewed["confirmation_review_required"] is True
    assert handoff_service.continuation_contract(reviewed, "omp")["must_not_execute"] is True
    with db() as conn:
        assert conn.execute("SELECT status FROM handoffs WHERE id=?", (created["id"],)).fetchone()[0] == "confirmed"
    token = authenticated_agent.set("omp")
    try:
        denied = mcp_server.read_handoff(created["id"], language="en")
    except ValueError as exc:
        denied = str(exc)
    finally:
        authenticated_agent.reset(token)
    assert "not confirmed" in str(denied).lower() or denied.get("ok") is False
    assert _counts()[1] == 0
