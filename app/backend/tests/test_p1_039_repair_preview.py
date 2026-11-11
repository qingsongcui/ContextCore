"""Isolated preview-then-authorize drill for legacy review candidates.

This is not a repair command. It never accepts a database path, and the only
writes are the existing content delete and unproven-handoff cancel calls.
"""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.database import create_schema, db, row_to_dict
from app.services import content_service, conversation_service, handoff_service, project_service


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "assess_legacy_provenance.py"
_SPEC = importlib.util.spec_from_file_location("legacy_assessment", _SCRIPT)
_ASSESS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_ASSESS)

_CONSEQUENCE = {
    "handoff": "cancel_unproven_confirmation",
    "memory": "permanent_delete",
}
_ITEM_KEYS = frozenset({"entity", "id", "consequence"})
_IMPLICIT_TITLE = "Implicit automatic decision"
_IMPLICIT_BODY = "PRIVATE-IMPLICIT-DECISION-BODY"
_ORDINARY_TITLE = "Ordinary decision"
_ORDINARY_BODY = "PRIVATE-ORDINARY-DECISION-BODY"
_CONVERSATION_BODY = "PRIVATE-CONVERSATION-BODY"


def _assess():
    return _ASSESS.assess(settings.db_path)


def _preview(assessment):
    items = []
    for finding in assessment["findings"]:
        items.append({
            "entity": finding["entity"],
            "id": finding["id"],
            "consequence": _CONSEQUENCE[finding["entity"]],
        })
    return {"authorized": False, "items": items}


def _apply(preview, *, authorized):
    if authorized is not True:
        return {"applied": False, "reason": "authorization_required"}
    items = preview.get("items")
    if not isinstance(items, list):
        return {"applied": False, "reason": "preview_mismatch"}
    fresh = {
        (item["entity"], item["id"]): item["consequence"]
        for item in _preview(_assess())["items"]
    }
    offered = []
    for item in items:
        if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
            return {"applied": False, "reason": "preview_mismatch"}
        key = (item["entity"], item["id"])
        if key in offered or fresh.get(key) != item["consequence"]:
            return {"applied": False, "reason": "preview_mismatch"}
        offered.append(key)
    if set(offered) != set(fresh):
        return {"applied": False, "reason": "preview_mismatch"}
    for entity, identifier in offered:
        if entity == "memory":
            content_service.delete_content(identifier, actor="user")
        elif entity == "handoff":
            handoff_service.cancel_handoff(identifier)
        else:
            return {"applied": False, "reason": "preview_mismatch"}
    return {"applied": True, "count": len(offered)}


def _rows():
    with db() as conn:
        return {
            "handoffs": [tuple(row) for row in conn.execute(
                "SELECT id, status, card FROM handoffs ORDER BY id")],
            "contents": [tuple(row) for row in conn.execute(
                "SELECT id, status, category, content FROM contents ORDER BY id")],
            "conversations": [tuple(row) for row in conn.execute(
                "SELECT id, content_hash FROM conversations ORDER BY id")],
            "tombstones": conn.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0],
            "confirmations": conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='handoff.confirm'").fetchone()[0],
        }


def _stored_handoff(handoff_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
        return row_to_dict(row)


def test_synthetic_preview_keeps_permanent_delete_semantics(tmp_path):
    assert settings.db_path.is_relative_to(tmp_path)
    assert "Application Support" not in str(settings.db_path)
    create_schema()
    project = project_service.create_project(name="Synthetic repair drill")
    legacy_task = content_service.create_content(
        title="Legacy task", content="Synthetic legacy task", category="task",
        project_id=project["id"], agent="codex",
    )
    kept_task = content_service.create_content(
        title="Kept task", content="Synthetic kept task", category="task",
        project_id=project["id"], agent="codex",
    )
    legacy = handoff_service.create_handoff(
        project_id=project["id"], target_agent="omp", task_id=legacy_task["id"],
        source_agent="codex", actor="codex",
    )
    kept = handoff_service.create_handoff(
        project_id=project["id"], target_agent="omp", task_id=kept_task["id"],
        source_agent="codex", actor="codex",
    )
    handoff_service.confirm_handoff(kept["id"], confirmed=True, actor="omp")
    with db() as conn:
        conn.execute(
            "UPDATE handoffs SET status='confirmed', confirmed_at='2026-01-01' WHERE id=?",
            (legacy["id"],),
        )
    implicit = content_service.create_content(
        title=_IMPLICIT_TITLE, content=_IMPLICIT_BODY, kind="memory", category="decision",
        project_id=project["id"], tags=["implicit_auto", "role:assistant"], agent="codex",
    )
    ordinary = content_service.create_content(
        title=_ORDINARY_TITLE, content=_ORDINARY_BODY, kind="memory", category="decision",
        project_id=project["id"], tags=["role:assistant"], agent="codex",
    )
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Synthetic source",
        external_id="p1-039-synthetic", messages=[{"role": "user", "content": _CONVERSATION_BODY}],
    )

    before = _rows()
    assessment = _assess()
    preview = _preview(assessment)
    rendered = json.dumps({"assessment": assessment, "preview": preview}, ensure_ascii=False)
    assert _rows() == before
    assert assessment["mode"] == "read_only" and assessment["changes"] == 0
    assert {(item["entity"], item["id"]) for item in preview["items"]} == {
        ("handoff", legacy["id"]), ("memory", implicit["id"]),
    }
    for secret in (_IMPLICIT_BODY, _ORDINARY_BODY, _CONVERSATION_BODY):
        assert secret not in rendered
    reviewed = handoff_service.get_handoff(legacy["id"])
    assert reviewed["confirmation_review_required"] is True
    assert handoff_service.continuation_contract(
        _stored_handoff(legacy["id"]), "omp")["status"] == "confirmation_review_required"
    assert handoff_service.continuation_contract(
        handoff_service.get_handoff(kept["id"]), "omp")["status"] == "ready"

    assert _apply(preview, authorized=False)["reason"] == "authorization_required"
    assert _rows() == before
    tampered = {"authorized": True, "items": preview["items"] + [
        {"entity": "handoff", "id": kept["id"], "consequence": "cancel_unproven_confirmation"},
        {"entity": "memory", "id": ordinary["id"], "consequence": "permanent_delete"},
    ]}
    assert _apply(tampered, authorized=True)["reason"] == "preview_mismatch"
    assert _rows() == before
    with pytest.raises(HTTPException) as keeper_cancel:
        handoff_service.cancel_handoff(kept["id"])
    assert keeper_cancel.value.status_code == 409
    assert _rows() == before

    applied = _apply(preview, authorized=True)
    assert applied == {"applied": True, "count": 2}
    cancelled = _stored_handoff(legacy["id"])
    assert cancelled["status"] == "cancelled"
    assert "_confirmation" not in cancelled["card"]
    assert handoff_service.continuation_contract(cancelled, "omp")["status"] == "cancelled"
    assert handoff_service.continuation_contract(cancelled, "omp")["must_not_execute"] is True
    assert handoff_service.continuation_contract(
        handoff_service.get_handoff(kept["id"]), "omp")["status"] == "ready"
    with db() as conn:
        assert conn.execute("SELECT id FROM contents WHERE id=?", (implicit["id"],)).fetchone() is None
        assert conn.execute(
            "SELECT COUNT(*) FROM content_versions WHERE content_id=?", (implicit["id"],)).fetchone()[0] == 0
        assert conn.execute("SELECT status FROM contents WHERE id=?", (ordinary["id"],)).fetchone()[0] == "active"
        assert conn.execute("SELECT id FROM conversations WHERE id=?", (conversation["id"],)).fetchone()
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='handoff.confirm'").fetchone()[0] == 1
    with pytest.raises(HTTPException) as recreated:
        content_service.create_content(
            title=_IMPLICIT_TITLE, content=_IMPLICIT_BODY, kind="memory", category="decision",
            project_id=project["id"], tags=["implicit_auto", "role:assistant"], agent="codex",
        )
    assert recreated.value.status_code == 410
    again = _assess()
    assert {item["id"] for item in again["findings"]} == set()
    assert again["changes"] == 0
