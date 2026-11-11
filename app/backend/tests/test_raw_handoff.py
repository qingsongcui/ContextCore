from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def raw_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_raw_handoff.db")
        os.environ["CONTEXTCORE_DB"] = db_path

        from app.database import create_schema, seed_data, db
        from app.main import app

        create_schema()
        seed_data()

        tokens = {
            "codex": "test-token-codex",
            "omp": "test-token-omp",
            "cursor": "test-token-cursor",
            "antigravity": "test-token-antigravity",
        }
        with db() as conn:
            for agent, tok in tokens.items():
                tok_h = hashlib.sha256(tok.encode("utf-8")).hexdigest()
                conn.execute(
                    "UPDATE connections SET enabled = 1, paused = 0, token_hash = ? WHERE id = ?",
                    (tok_h, agent),
                )

        client = TestClient(
            app,
            base_url="http://127.0.0.1:8420",
            client=("127.0.0.1", 50000),
        )

        sess_res = client.get("/api/session")
        assert sess_res.status_code == 200
        csrf_token = sess_res.json()["csrf_token"]

        yield {
            "client": client,
            "csrf_token": csrf_token,
            "tokens": tokens,
            "tmpdir": tmpdir,
        }


def test_raw_conversation_handoff_when_zero_distilled_tasks(raw_env):
    """
    With no distilled tasks, an explicit target-MCP request can prepare a
    source-backed card. The preceding lifecycle notification remains silent.
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    tokens = raw_env["tokens"]
    proj_dir = os.path.join(raw_env["tmpdir"], "raw_proj")
    os.makedirs(proj_dir, exist_ok=True)

    from app.services import conversation_service

    # 1. Create project (zero tasks, zero memories)
    p_res = client.post(
        "/api/projects",
        json={"name": "Raw Conversation Project", "directories": [proj_dir]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert p_res.status_code == 200
    proj_id = p_res.json()["id"]

    # 2. Insert single raw conversation from predecessor agent 'codex'
    conv = conversation_service.upsert_conversation(
        agent="codex",
        title="Payment Gateway Integration",
        external_id="codex:conv-raw-1",
        project_id=proj_id,
        cwd=proj_dir,
        messages=[
            {"id": "m1", "role": "user", "content": "Integrate Stripe webhook event handlers"},
            {"id": "m2", "role": "assistant", "content": "Drafted handlers in payment/webhook.py with signature verification"},
        ],
        process_summary="Added signature validation; tested customer.subscription.updated",
    )
    conv_id = conv["id"]

    # The lifecycle captures the request but never creates or injects a card.
    hook_payload = {
        "event": "intent_detected",
        "sessionId": "omp-raw-test",
        "cwd": proj_dir,
        "prompt": "接着做",
    }
    hook_headers = {"Authorization": f"Bearer {tokens['omp']}", "X-CSRF-Token": csrf_token}
    res = client.post("/api/connections/omp/hook", json=hook_payload, headers=hook_headers)
    assert res.status_code == 200
    data = res.json()

    assert data["ok"] is True and data["context_type"] == "none"
    assert not {"injection", "card", "handoff_id"}.intersection(data)
    from app import mcp_server
    from app.security import authenticated_agent
    identity = authenticated_agent.set("omp")
    try:
        prepared = mcp_server.start_handoff(
            target_agent="omp", project_id=proj_id, conversation_id=conv_id, source_agent="codex",
        )
    finally:
        authenticated_agent.reset(identity)
    hid = prepared["handoff_id"]
    card = prepared["card"]
    assert "Integrate Stripe webhook event handlers" in card["goal"]
    assert "Drafted handlers" in card["progress"]
    assert prepared["continuation"]["must_not_execute"] is True
    assert prepared["continuation"]["resume_from"] is None

    # Verify conversation is tracked in source_ids
    h_res = client.get(f"/api/handoffs/{hid}", headers={"X-CSRF-Token": csrf_token})
    assert h_res.status_code == 200
    h_data = h_res.json()
    assert conv_id in h_data["source_ids"]
    assert h_data["source_agent"] == "codex"
    assert h_data["target_agent"] == "omp"


def test_multiple_raw_conversations_require_explicit_choice_before_readback(raw_env):
    """
    Multiple captured predecessors require explicit selection through relay,
    never unsolicited lifecycle context or a recency-selected executable card.
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    proj_dir = os.path.join(raw_env["tmpdir"], "multi_raw_proj")
    os.makedirs(proj_dir, exist_ok=True)

    from app.services import conversation_service

    p_res = client.post("/api/projects", json={"name": "Multi Raw Project", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token})
    assert p_res.status_code == 200
    proj_id = p_res.json()["id"]

    c1 = conversation_service.upsert_conversation(
        agent="codex",
        title="Refactor Auth Module",
        external_id="codex:c1",
        project_id=proj_id,
        cwd=proj_dir,
        messages=[{"id": "m1", "role": "user", "content": "Refactor tokens"}],
    )
    c2 = conversation_service.upsert_conversation(
        agent="codex",
        title="Optimize DB Indexes",
        external_id="codex:c2",
        project_id=proj_id,
        cwd=proj_dir,
        messages=[{"id": "m2", "role": "user", "content": "Optimize queries"}],
    )

    response = client.post("/api/handoffs/relay", headers={"X-CSRF-Token": csrf_token}, json={
        "project_id": proj_id, "consumer": "omp", "sync_first": False, "max_tokens": 32000,
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "selection_required"
    assert result["must_not_execute"] is True
    assert {item["id"] for item in result["candidates"]} == {c1["id"], c2["id"]}
    assert {item["title"] for item in result["candidates"]} == {"Refactor Auth Module", "Optimize DB Indexes"}
    assert "handoff_id" not in result
    from app import mcp_server
    from app.security import authenticated_agent
    identity = authenticated_agent.set("omp")
    try:
        selected = mcp_server.start_handoff(
            target_agent="omp", project_id=proj_id, conversation_id=c2["id"], source_agent="codex",
        )
    finally:
        authenticated_agent.reset(identity)
    assert c2["id"] in selected["source_ids"]
    assert c1["id"] not in selected["source_ids"]
    assert "Optimize queries" in selected["card"]["goal"]
    assert selected["continuation"]["must_not_execute"] is True


def test_mcp_start_handoff_by_target_agent_resolves_predecessor_source(raw_env):
    """
    When target agent initiates its own takeover via MCP start_handoff (target_agent == caller actor),
    predecessor conversation determines source_agent rather than rejecting same-agent.
    Audit log preserves actual caller actor.
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    proj_dir = os.path.join(raw_env["tmpdir"], "mcp_takeover_proj")
    os.makedirs(proj_dir, exist_ok=True)

    from app.services import conversation_service, handoff_service
    from app.database import db

    p_res = client.post("/api/projects", json={"name": "MCP Takeover Project", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token})
    assert p_res.status_code == 200
    proj_id = p_res.json()["id"]

    # Outgoing agent was 'omp'
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Data Migration Script",
        external_id="omp:c-takeover",
        project_id=proj_id,
        cwd=proj_dir,
        messages=[{"id": "m1", "role": "user", "content": "Migrate schema v1 to v2"}],
    )
    conv_id = conv["id"]

    # Target agent 'codex' is the caller taking over
    from app.security import authenticated_agent
    from app.mcp_server import start_handoff

    tok = authenticated_agent.set("codex")
    try:
        mcp_res = start_handoff(
            target_agent="codex",
            project_id=proj_id,
            conversation_id=conv_id,
        )
    finally:
        authenticated_agent.reset(tok)
    assert mcp_res["status"] == "awaiting_confirmation"
    assert mcp_res["must_confirm"] is True
    hid = mcp_res["handoff_id"]

    # In DB, source_agent must be 'omp' (from predecessor), target_agent must be 'codex'
    h = handoff_service.get_handoff(hid)
    assert h["source_agent"] == "omp"
    assert h["target_agent"] == "codex"
    assert conv_id in h["source_ids"]

    # In audit log, actor must be 'codex' (the actual caller)
    with db() as conn:
        audit_row = conn.execute("SELECT actor, action, target FROM audit_logs WHERE target = ? ORDER BY created_at DESC LIMIT 1", (hid,)).fetchone()
        assert audit_row is not None
        assert audit_row["actor"] == "codex"


def test_explicit_task_and_project_conflict_rejects_422_or_404(raw_env):
    """
    Explicit task_id, conversation_id, or project_id must validate strictly:
    - Missing IDs reject 404 (no silent latest fallback).
    - Project mismatch rejects 422 (no cross-project leakage).
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    proj_dir_a = os.path.join(raw_env["tmpdir"], "proj_a")
    proj_dir_b = os.path.join(raw_env["tmpdir"], "proj_b")
    os.makedirs(proj_dir_a, exist_ok=True)
    os.makedirs(proj_dir_b, exist_ok=True)

    from app.services import handoff_service, conversation_service

    pa = client.post("/api/projects", json={"name": "Project A", "directories": [proj_dir_a]}, headers={"X-CSRF-Token": csrf_token}).json()
    pb = client.post("/api/projects", json={"name": "Project B", "directories": [proj_dir_b]}, headers={"X-CSRF-Token": csrf_token}).json()

    # Task in Project A
    ta = client.post(
        "/api/content",
        json={"title": "Task In A", "category": "task", "kind": "memory", "agent": "codex", "project_id": pa["id"]},
        headers={"X-CSRF-Token": csrf_token},
    ).json()

    # Conversation in Project A
    ca = conversation_service.upsert_conversation(
        agent="codex",
        title="Conv In A",
        external_id="codex:in-a",
        project_id=pa["id"],
        cwd=proj_dir_a,
        messages=[{"id": "m1", "role": "user", "content": "Work in A"}],
    )

    # 1. Non-existent task_id -> 404
    post_res1 = client.post(
        "/api/handoffs",
        json={"target_agent": "omp", "task_id": "nonexistent_task"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert post_res1.status_code == 404

    # 2. Non-existent conversation_id -> 404
    post_res2 = client.post(
        "/api/handoffs",
        json={"target_agent": "omp", "conversation_id": "nonexistent_conv"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert post_res2.status_code == 404

    # 3. Non-existent project_id -> 404
    post_res3 = client.post(
        "/api/handoffs",
        json={"target_agent": "omp", "project_id": "nonexistent_proj"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert post_res3.status_code == 404

    # 4. Task in Project A, but project_id specifies Project B -> 422
    post_res4 = client.post(
        "/api/handoffs",
        json={"target_agent": "omp", "task_id": ta["id"], "project_id": pb["id"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert post_res4.status_code == 422
    assert "does not belong to project" in post_res4.json()["detail"]

    # 5. Conversation in Project A, but project_id specifies Project B -> 422
    post_res5 = client.post(
        "/api/handoffs",
        json={"target_agent": "omp", "conversation_id": ca["id"], "project_id": pb["id"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert post_res5.status_code == 422
    assert "does not belong to project" in post_res5.json()["detail"]


def test_handoff_confirmation_checks_conflict_and_conversation_sources(raw_env):
    """
    Preserve Parent fix: confirmation accepts sources in contents, conversations, or conflicts.
    Rejects with 400 if any referenced item was deleted.
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    proj_dir = os.path.join(raw_env["tmpdir"], "confirm_sources_repo")
    os.makedirs(proj_dir, exist_ok=True)

    from app.services import conversation_service, conflict_service, handoff_service
    from app.database import db

    p = client.post("/api/projects", json={"name": "Confirm Sources Repo", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token}).json()
    proj_id = p["id"]

    # Create conversation and conflict in project
    conv = conversation_service.upsert_conversation(
        agent="codex",
        title="Bug Investigation",
        external_id="codex:bug-inv",
        project_id=proj_id,
        cwd=proj_dir,
        messages=[{"id": "m1", "role": "user", "content": "Investigate bug"}],
    )
    cnt = client.post(
        "/api/content",
        json={"title": "Migration Lock Info", "category": "knowledge", "kind": "memory", "project_id": proj_id},
        headers={"X-CSRF-Token": csrf_token},
    ).json()

    conflict = conflict_service.create_conflict(
        project_id=proj_id,
        existing_id=cnt["id"],
        proposed={"title": "Proposed Migration Lock"},
        reason="Conflicting database migration lock",
    )
    # Create handoff from conversation
    h = handoff_service.create_handoff(
        project_id=proj_id,
        target_agent="omp",
        conversation_id=conv["id"],
        source_agent="codex",
    )
    hid = h["id"]

    # Confirm web confirm route is removed (404)
    c_web = client.post(f"/api/handoffs/{hid}/confirm", json={"confirmed": True}, headers={"X-CSRF-Token": csrf_token})
    assert c_web.status_code == 404

    # Confirm handoff via target agent MCP: both conversation and conflict are in source_ids and valid
    from app.security import authenticated_agent
    from app.mcp_server import confirm_handoff
    tok = authenticated_agent.set("omp")
    try:
        conf_res = confirm_handoff(handoff_id=hid, confirmed=True)
        assert conf_res["ok"] is True
        assert conf_res["status"] == "confirmed"
    finally:
        authenticated_agent.reset(tok)
    # Now delete the conversation and create new handoff referencing it
    conversation_service.delete_conversation(conv["id"])

    # Create handoff manually with deleted source_id to verify strict confirmation guard
    with db() as conn:
        from app.database import new_id, now_iso, encode
        h2_id = new_id("hnd")
        card = {"goal": "g", "progress": "p", "decisions": "d", "evidence": "e", "blockers": "b", "next_steps": "n"}
        source_revs = {conv["id"]: {"type": "conversation", "content_hash": "old_hash", "project_id": proj_id}}
        conn.execute(
            """
            INSERT INTO handoffs (id, project_id, source_agent, target_agent, status, card, source_ids, source_revisions, created_at)
            VALUES (?, ?, 'codex', 'omp', 'awaiting_confirmation', ?, ?, ?, ?)
            """,
            (h2_id, proj_id, encode(card), encode([conv["id"]]), encode(source_revs), now_iso()),
        )
    # Confirm web confirm route is removed (404)
    c_fail_web = client.post(f"/api/handoffs/{h2_id}/confirm", json={"confirmed": True}, headers={"X-CSRF-Token": csrf_token})
    assert c_fail_web.status_code == 404

    # Attempt to confirm handoff with deleted conversation via target MCP -> must reject 400
    tok = authenticated_agent.set("omp")
    try:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as rejected:
            confirm_handoff(handoff_id=h2_id, confirmed=True)
        assert rejected.value.status_code == 400
    finally:
        authenticated_agent.reset(tok)

def test_concrete_selected_task_mismatch_prioritized_in_next_steps(raw_env):
    """
    Review Finding 1: Explicitly selected task must take priority in next_steps
    over arbitrary active_tasks[0].
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    proj_dir = os.path.join(raw_env["tmpdir"], "task_priority_proj")
    os.makedirs(proj_dir, exist_ok=True)

    from app.services import handoff_service

    p = client.post("/api/projects", json={"name": "Task Priority Project", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token}).json()
    proj_id = p["id"]

    t1 = client.post("/api/content", json={"title": "Task 1: Setup DB", "category": "task", "kind": "memory", "agent": "codex", "project_id": proj_id}, headers={"X-CSRF-Token": csrf_token}).json()
    t2 = client.post("/api/content", json={"title": "Task 2: Build UI", "category": "task", "kind": "memory", "agent": "codex", "project_id": proj_id}, headers={"X-CSRF-Token": csrf_token}).json()

    # Explicitly select Task 2
    h = handoff_service.create_handoff(
        project_id=proj_id,
        target_agent="omp",
        task_id=t2["id"],
        source_agent="codex",
    )
    assert "Task 2: Build UI" in h["card"]["next_steps"]
    assert "Task 1: Setup DB" not in h["card"]["next_steps"]


def test_target_own_recent_turn_excluded_from_evidence(raw_env):
    """
    Review Finding 3: Target agent's own recent turns (e.g. '接着做') must be excluded
    from handoff card evidence, preserving true predecessor provenance.
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    proj_dir = os.path.join(raw_env["tmpdir"], "target_turn_proj")
    os.makedirs(proj_dir, exist_ok=True)

    from app.services import conversation_service, handoff_service

    p = client.post("/api/projects", json={"name": "Target Turn Project", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token}).json()
    proj_id = p["id"]

    # Predecessor agent conversation
    conversation_service.upsert_conversation(
        agent="codex",
        title="Predecessor Work",
        external_id="codex:turn-pred",
        project_id=proj_id,
        cwd=proj_dir,
        messages=[{"id": "m1", "role": "user", "content": "Implement auth engine"}],
    )

    # Target agent's own incoming turn
    conversation_service.upsert_conversation(
        agent="omp",
        title="OMP Turn",
        external_id="omp:turn-incoming",
        project_id=proj_id,
        cwd=proj_dir,
        messages=[{"id": "m2", "role": "user", "content": "接着做"}],
    )

    task = client.post("/api/content", json={"title": "Core Auth", "category": "task", "kind": "memory", "agent": "codex", "project_id": proj_id}, headers={"X-CSRF-Token": csrf_token}).json()

    # Target agent 'omp' creates handoff
    h = handoff_service.create_handoff(
        project_id=proj_id,
        target_agent="omp",
        task_id=task["id"],
        source_agent="codex",
    )
    evidence = h["card"]["evidence"]
    assert "[codex]" in evidence
    assert "[omp]" not in evidence


def test_same_agent_casing_cannot_manufacture_a_cross_agent_relay(raw_env):
    """
    Case-normalized same-Agent work is not a cross-Agent predecessor and cannot
    manufacture an executable card through explicit relay.
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    proj_dir = os.path.join(raw_env["tmpdir"], "casing_proj")
    os.makedirs(proj_dir, exist_ok=True)

    p = client.post("/api/projects", json={"name": "Casing Project", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token}).json()
    proj_id = p["id"]

    # Task created with Title-cased 'Codex'
    client.post(
        "/api/content",
        json={"title": "Codex Task Title Cased", "category": "task", "kind": "memory", "agent": "Codex", "project_id": proj_id},
        headers={"X-CSRF-Token": csrf_token},
    )

    response = client.post("/api/handoffs/relay", headers={"X-CSRF-Token": csrf_token}, json={
        "project_id": proj_id, "consumer": "codex", "sync_first": False, "max_tokens": 32000,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "selection_required"
    assert data["must_not_execute"] is True
    assert not data.get("handoff_id")


def test_other_agent_confirm_rejected_and_audit_correct(raw_env):
    """
    Review Finding 5: MCP confirm_handoff must require confirming agent to match
    handoff target_agent, and audit log must attribute confirmation to the confirming agent.
    """
    client = raw_env["client"]
    csrf_token = raw_env["csrf_token"]
    proj_dir = os.path.join(raw_env["tmpdir"], "confirm_actor_proj")
    os.makedirs(proj_dir, exist_ok=True)

    from app.services import handoff_service, conversation_service
    from app.security import authenticated_agent
    from app.mcp_server import confirm_handoff
    from app.database import db

    p = client.post("/api/projects", json={"name": "Confirm Actor Project", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token}).json()
    proj_id = p["id"]

    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Predecessor Task",
        external_id="omp:c-actor",
        project_id=proj_id,
        cwd=proj_dir,
        messages=[{"id": "m1", "role": "user", "content": "Build feature"}],
    )

    # Handoff created for target_agent 'codex'
    h = handoff_service.create_handoff(
        project_id=proj_id,
        target_agent="codex",
        conversation_id=conv["id"],
        source_agent="omp",
    )
    hid = h["id"]

    # 1. Other agent 'cursor' tries to confirm -> must be rejected
    tok1 = authenticated_agent.set("cursor")
    try:
        res_unauthorized = confirm_handoff(handoff_id=hid, confirmed=True)
        assert res_unauthorized["ok"] is False
        assert "does not match handoff target agent" in res_unauthorized["error"]
    finally:
        authenticated_agent.reset(tok1)

    # 2. Addressed target agent 'codex' confirms -> succeeds
    tok2 = authenticated_agent.set("codex")
    try:
        res_ok = confirm_handoff(handoff_id=hid, confirmed=True)
        assert res_ok["ok"] is True
        assert res_ok["status"] == "confirmed"
    finally:
        authenticated_agent.reset(tok2)

    # 3. Check audit log records actor='codex'
    with db() as conn:
        audit_row = conn.execute(
            "SELECT actor, action, target FROM audit_logs WHERE action = 'handoff.confirm' AND target = ? ORDER BY created_at DESC LIMIT 1",
            (hid,),
        ).fetchone()
        assert audit_row is not None
        assert audit_row["actor"] == "codex"
