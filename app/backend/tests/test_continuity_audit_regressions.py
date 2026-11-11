from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def continuity_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_continuity_audit.db")
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


def test_f2_web_confirm_route_removed_and_mcp_enforces_target_identity(continuity_env):
    """
    F2: POST /api/handoffs/{id}/confirm is removed (returns 404).
    Only target Agent can confirm via MCP confirm_handoff, verifying caller == target_agent.
    """
    client = continuity_env["client"]
    csrf_token = continuity_env["csrf_token"]
    from app.services import handoff_service, project_service
    from app.security import authenticated_agent
    from app import mcp_server

    proj = project_service.create_project(name="F2 Project")
    h = handoff_service.create_handoff(
        project_id=proj["id"],
        target_agent="omp",
        source_agent="codex",
    )
    hid = h["id"]

    # 1. Web route must return 404
    res_web = client.post(
        f"/api/handoffs/{hid}/confirm",
        json={"confirmed": True},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert res_web.status_code == 404

    # 2. Non-target agent via MCP must be rejected
    tok_cursor = authenticated_agent.set("cursor")
    try:
        denied = mcp_server.confirm_handoff(hid, confirmed=True)
        assert denied["ok"] is False
        assert "does not match handoff target agent" in denied["error"]
    finally:
        authenticated_agent.reset(tok_cursor)

    # 3. Target agent via MCP succeeds and records audit log
    tok_omp = authenticated_agent.set("omp")
    try:
        confirmed = mcp_server.confirm_handoff(hid, confirmed=True)
        assert confirmed["ok"] is True
        assert confirmed["status"] == "confirmed"
        assert confirmed["continuation"]["status"] == "ready"
        assert confirmed["continuation"]["must_not_execute"] is False
    finally:
        authenticated_agent.reset(tok_omp)

    from app.database import db
    with db() as conn:
        audit = conn.execute(
            "SELECT actor, action, target FROM audit_logs WHERE action='handoff.confirm' AND target=?",
            (hid,),
        ).fetchone()
        assert audit is not None
        assert audit["actor"] == "omp"


def test_f3_lookup_continuation_multiple_cards_fail_closed_as_candidates(continuity_env):
    """
    F3: lookup_continuation must not silently LIMIT 1 when multiple cards exist.
    It returns status='ambiguous_continuation', must_not_execute=True, and lists candidates.
    Explicit handoff_id or unique query match resolves to the single card.
    """
    from app.services import handoff_service, project_service, content_service
    from app.security import authenticated_agent
    from app import mcp_server

    p1 = project_service.create_project(name="Project One")
    p2 = project_service.create_project(name="Project Two")

    t1 = content_service.create_content(project_id=p1["id"], category="task", title="Task A in P1", content="Work on P1")
    t2 = content_service.create_content(project_id=p2["id"], category="task", title="Task B in P2", content="Work on P2")

    h1 = handoff_service.create_handoff(project_id=p1["id"], task_id=t1["id"], target_agent="codex", source_agent="omp")
    h2 = handoff_service.create_handoff(project_id=p2["id"], task_id=t2["id"], target_agent="codex", source_agent="omp")

    # 1. Bare continuation query for codex with no project_id: 2 cards match -> ambiguous_continuation
    res = handoff_service.lookup_continuation(consumer_id="codex", query="接着做")
    assert res is not None
    assert res["status"] == "ambiguous_continuation"
    assert res["must_not_execute"] is True
    assert len(res["candidates"]) == 2
    c_ids = {c["handoff_id"] for c in res["candidates"]}
    assert h1["id"] in c_ids and h2["id"] in c_ids

    # 2. Scoped by project_id: only 1 card in p1 -> returns single card
    res_p1 = handoff_service.lookup_continuation(consumer_id="codex", project_id=p1["id"], query="接着做")
    assert res_p1 is not None
    assert res_p1["status"] == "awaiting_confirmation"
    assert res_p1["handoff_id"] == h1["id"]

    # 3. Explicit handoff_id resolves directly
    res_h2 = handoff_service.lookup_continuation(consumer_id="codex", handoff_id=h2["id"])
    assert res_h2 is not None
    assert res_h2["handoff_id"] == h2["id"]

    # 4. MCP assemble_context reflects ambiguous_continuation without picking silently
    tok = authenticated_agent.set("codex")
    try:
        pack = mcp_server.assemble_context(task_query="接着做", max_tokens=32000)
        assert pack["continuation"]["status"] == "ambiguous_continuation"
        assert pack["continuation"]["must_not_execute"] is True
        assert len(pack["continuation"]["candidates"]) == 2
    finally:
        authenticated_agent.reset(tok)


def test_f4_sole_open_task_not_autobound_when_explicit_instruction_provided(continuity_env):
    """
    F4: When project has 1 open task but caller gave a specific instruction,
    do NOT auto-bind the open task. Keep task unselected, preserve explicit instruction in goal/next_steps.
    """
    from app.services import handoff_service, project_service, content_service

    p = project_service.create_project(name="Auth Project")
    open_task = content_service.create_content(
        project_id=p["id"],
        category="task",
        title="支付联调",
        content="完成微信支付服务端接入",
    )

    # Specific instruction provided: should NOT auto-bind open_task
    h = handoff_service.create_handoff(
        project_id=p["id"],
        target_agent="omp",
        source_agent="codex",
        query="修一下登录页 500 报错",
    )
    assert h["card"]["goal"] == "修一下登录页 500 报错"
    assert "修一下登录页 500 报错" in h["card"]["next_steps"]
    assert "支付联调" not in h["card"]["goal"]
    assert "并行任务未选定: 支付联调" in h["card"]["progress"]
    assert open_task["id"] not in h["source_ids"]

    # Bare continuation prompt: DOES auto-bind the sole open task
    h_cont = handoff_service.create_handoff(
        project_id=p["id"],
        target_agent="omp",
        source_agent="codex",
        query="接着做",
    )
    assert "支付联调" in h_cont["card"]["goal"]
    assert open_task["id"] in h_cont["source_ids"]


def test_f5_ambiguous_projects_and_predecessor_never_guessed_by_recency(continuity_env):
    """
    F5: Multiple active projects fail closed with 422 ambiguous_project.
    Predecessor agent is never guessed by recency from unrelated conversations.
    """
    from app.services import handoff_service, project_service, conversation_service
    from fastapi import HTTPException

    p1 = project_service.create_project(name="Alpha Project")
    p2 = project_service.create_project(name="Beta Project")

    # Multiple active projects without project_id -> 422
    with pytest.raises(HTTPException) as exc:
        handoff_service.create_handoff(target_agent="omp", query="do work")
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "ambiguous_project"

    # Predecessor agent not guessed by recency
    # Add conversation in p1 by 'cursor'
    conversation_service.upsert_conversation(
        agent="cursor",
        project_id=p1["id"],
        title="Cursor chat",
        external_id="cursor:sess1",
        messages=[{"role": "user", "content": "hi"}],
    )
    # Handoff created for omp in p1 without source_agent or predecessor conv
    h = handoff_service.create_handoff(
        project_id=p1["id"],
        target_agent="omp",
        query="new task",
        actor="user",
    )
    # Must NOT adopt cursor by recency
    assert h["source_agent"] != "cursor"


def test_f6_specific_instructions_containing_continue_not_discarded(continuity_env):
    """
    F6: Specific instructions containing words like 'continue' or 'resume' must not be
    discarded as bare continuation prompts.
    """
    from app.services.handoff_service import _is_continuation_prompt, create_handoff
    from app.services.project_service import create_project

    # Specific instructions
    p1 = "continue the CSV export work but do not touch the schema"
    p2 = "resume from where we left off on the auth module"
    p3 = "接着做支付联调任务"
    for p in (p1, p2, p3):
        assert _is_continuation_prompt(p) is False

    # Bare continuation prompts
    assert _is_continuation_prompt("continue") is True
    assert _is_continuation_prompt("continue.") is True
    assert _is_continuation_prompt("/continue") is True
    assert _is_continuation_prompt("接着做") is True
    assert _is_continuation_prompt("resume") is True
    assert _is_continuation_prompt("take over") is True

    # Card creation retains specific instruction
    proj = create_project(name="Export Project")
    h = create_handoff(
        project_id=proj["id"],
        target_agent="omp",
        source_agent="codex",
        query=p1,
    )
    assert h["card"]["goal"] == p1
    assert p1 in h["card"]["next_steps"]


def test_f9_ordinary_hooks_stay_silent_and_no_injection_accounting(continuity_env):
    """
    F9: Lifecycle hooks remain silent even when their payload claims to request
    context. Explicit context retrieval belongs to MCP, not notification input.
    No records are written to context_injections.
    """
    client = continuity_env["client"]
    csrf_token = continuity_env["csrf_token"]
    tokens = continuity_env["tokens"]
    from app.services.project_service import create_project
    from app.database import db

    proj_dir = os.path.join(continuity_env["tmpdir"], "f9_repo")
    os.makedirs(proj_dir, exist_ok=True)
    create_project(name="F9 Silent Project", directories=[proj_dir])

    # Ordinary UserPromptSubmit without context_request
    payload = {
        "event": "UserPromptSubmit",
        "session_id": "sess-silent",
        "cwd": proj_dir,
        "prompt": "write a python function to add two numbers",
    }
    headers = {"Authorization": f"Bearer {tokens['codex']}", "X-CSRF-Token": csrf_token}
    res = client.post("/api/connections/codex/hook", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["context_type"] == "none"
    assert "injection" not in data
    assert "card" not in data

    # Verify no injection recorded in context_injections
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM context_injections").fetchone()[0]
        assert count == 0

    # A payload flag is not explicit MCP retrieval or user confirmation.
    payload_explicit = {
        "event": "UserPromptSubmit",
        "session_id": "sess-explicit",
        "cwd": proj_dir,
        "prompt": "write code",
        "context_request": True,
    }
    res_exp = client.post("/api/connections/codex/hook", json=payload_explicit, headers=headers)
    assert res_exp.status_code == 200
    assert res_exp.json()["context_type"] == "none"
    assert not {"injection", "card", "handoff_id"}.intersection(res_exp.json())
    with db() as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM context_injections").fetchone()[0]
        assert count_after == 0


def test_f10_mcp_ack_context_enables_epoch_deduplication(continuity_env):
    """
    F10: mcp_server.ack_context(pack_id) records emission in context_injections,
    enabling subsequent assemble_context calls in the same epoch to deduplicate.
    """
    from app.security import authenticated_agent
    from app.services import project_service, content_service
    from app import mcp_server

    proj = project_service.create_project(name="F10 Project")
    mem = content_service.create_content(
        project_id=proj["id"],
        category="knowledge",
        title="DB Design",
        content="Use SQLite for lightweight storage",
    )

    tok = authenticated_agent.set("codex")
    try:
        # First assembly
        pack = mcp_server.assemble_context("DB Design", project_id=proj["id"], max_tokens=4000)
        pack_id = pack["id"]
        assert len(pack["blocks"]) == 1
        assert pack["blocks"][0]["id"] == mem["id"]

        # Acknowledge via MCP tool
        ack = mcp_server.ack_context(pack_id)
        assert ack["status"] == "emitted"
        assert ack["consumed"] == "unknown"

        # Repeated assembly in same epoch: deduplicated (blocks empty, omitted count >= 1)
        pack2 = mcp_server.assemble_context("DB Design", project_id=proj["id"], max_tokens=4000)
        assert len(pack2["blocks"]) == 0
        assert pack2["omitted_matching_items"] >= 1
    finally:
        authenticated_agent.reset(tok)

    # Ownership enforcement: different agent cannot ack pack prepared for codex
    tok_other = authenticated_agent.set("omp")
    try:
        denied = mcp_server.ack_context(pack_id)
        assert denied.get("ok") is False
        assert "Unauthorized" in str(denied.get("error", ""))
    finally:
        authenticated_agent.reset(tok_other)


def test_f11_relation_extraction_allows_explicit_retry_on_failure(continuity_env, monkeypatch):
    """
    F11: When relation extraction is in failed/unknown/cancelled state,
    retry=False returns duplicate=True without retry, but retry=True permits a fresh attempt
    while preserving previous attempt audit/usage provenance in the ledger.
    """
    import asyncio
    from fastapi import HTTPException
    from app.services import project_service, content_service, retrieval_service
    from app.database import db, now_iso, encode, set_app_setting
    from app import transport

    proj = project_service.create_project(name="F11 Project")
    c1 = content_service.create_content(project_id=proj["id"], category="knowledge", title="A", content="Text A")
    c2 = content_service.create_content(project_id=proj["id"], category="knowledge", title="B", content="Text B")

    # 1. Unset compute permission: correctly raises 403 (prod gate intact)
    with pytest.raises(HTTPException) as exc_403:
        asyncio.run(retrieval_service.extract_relations([c1["id"], c2["id"]], retry=False))
    assert exc_403.value.status_code == 403

    # 2. Explicitly authorize synthetic FakeOMP in database (never real model)
    with db() as conn:
        set_app_setting("compute_authorized", True, conn)
        set_app_setting("compute_host", "omp", conn)
        conn.execute("UPDATE connections SET enabled=1, paused=0 WHERE id='omp'")
    retrieval_service.update_settings({"relation_enabled": True})

    # Fake process runner only
    async def fake_distill(*args, **kwargs):
        return transport.DistillResult(
            status="success",
            memories=[],
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )
    monkeypatch.setattr(transport, "run_distill", fake_distill)

    # Seed an existing failed extraction row
    with db() as conn:
        snapshot = retrieval_service._relation_snapshot(conn, sorted([c1["id"], c2["id"]]))
        key = hashlib.sha256(encode({"kind": "relation-extraction-v1", "sources": snapshot}).encode()).hexdigest()
        now = now_iso()
        conn.execute(
            "INSERT INTO relation_extractions (id,request_key,source_revisions,policy_revision,governance_revision,state,invoked,lease_until,error_code,receipt,created_at,updated_at) VALUES (?,?,?,?,?,'failed',1,NULL,'malformed_output','[]',?,?)",
            ("rel_fail_1", key, encode(snapshot), 1, 1, now, now),
        )

    # 3. Without retry flag -> returns duplicate=True, automatic_retry=False
    res_no_retry = asyncio.run(retrieval_service.extract_relations([c1["id"], c2["id"]], retry=False))
    assert res_no_retry["status"] == "failed"
    assert res_no_retry["duplicate"] is True
    assert res_no_retry["automatic_retry"] is False

    # 4. With retry=True -> archives old attempt and executes new attempt
    res_retry = asyncio.run(retrieval_service.extract_relations([c1["id"], c2["id"]], retry=True))
    assert res_retry["status"] == "succeeded"
    assert res_retry.get("id") != "rel_fail_1"

    # 5. Verify old attempt row is PRESERVED in the database with audit provenance
    with db() as conn:
        archived = conn.execute("SELECT * FROM relation_extractions WHERE id='rel_fail_1'").fetchone()
        assert archived is not None
        assert ":attempt:rel_fail_1" in archived["request_key"]
        assert archived["error_code"] == "malformed_output"


def test_f12_cancelled_card_reports_cancelled_not_awaiting_confirmation(continuity_env):
    """
    F12: continuation_contract must report status='cancelled' and must_not_execute=True
    for cancelled cards, never 'awaiting_confirmation'.
    """
    from app.services import handoff_service, project_service

    proj = project_service.create_project(name="F12 Project")
    h = handoff_service.create_handoff(
        project_id=proj["id"],
        target_agent="omp",
        source_agent="codex",
    )
    assert h["status"] == "awaiting_confirmation"

    # Cancel the handoff
    cancelled = handoff_service.cancel_handoff(h["id"])
    assert cancelled["status"] == "cancelled"

    # Continuation contract must report cancelled
    contract = handoff_service.continuation_contract(cancelled, "omp")
    assert contract["status"] == "cancelled"
    assert contract["must_not_execute"] is True
    assert "cancelled" in contract["message"].lower()
