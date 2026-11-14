"""Regression tests for relay budget defaults, compressed deduplication, and provisional handoff cleanup."""
from __future__ import annotations

import json

from app import mcp_server
from app.database import create_schema, db
from app.security import authenticated_agent
from app.services import (
    content_service,
    conversation_service,
    handoff_service,
    project_service,
    working_memory_service,
)


def _setup_realistic_project(tmp_path, monkeypatch, db_name: str = "relay-budget.db") -> dict:
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / db_name))
    monkeypatch.setenv("CONTEXTCORE_DATA_DIR", str(tmp_path / "data"))
    create_schema()

    proj = project_service.create_project(
        name="senrelay-e2e-relay-demo",
        directories=[str(tmp_path / "repo")],
    )
    dec = content_service.create_content(
        title="价格模块返回值与金额单位规范",
        content="所有价格计算函数禁止返回字典，必须直接返回 float 元并保留两位小数。",
        category="decision",
        project_id=proj["id"],
        agent="codex",
        tags=["pinned"],
    )
    pref = content_service.create_content(
        title="中文沟通与最小改动偏好",
        content="默认使用简洁中文汇报关键结论；不重写未要求改动的函数。",
        category="preference",
        project_id=None,
        agent="codex",
    )
    task1 = working_memory_service.upsert_working_task(
        title="验证经验提炼与冲突门禁",
        goal="验证 Gate-0 经验提炼与冲突确认门禁",
        project_id=proj["id"],
        status="in_progress",
        agent="codex",
    )
    task2 = working_memory_service.upsert_working_task(
        title="第二项独立验证任务",
        goal="测试超预算失败时不会提前作废旧草稿",
        project_id=proj["id"],
        status="in_progress",
        agent="opencode",
    )

    long_prompt = (
        "请严格按照仓库规范检查价格模块与交接协议，保留所有约束并不要重写 calc_subtotal。"
        + " 补充详细验收要求：检查每个分支路径与编码细节。" * 35
    )
    long_summary = "工具调用轨迹与检查总结：" + "verified module step; " * 80
    convs = []
    for idx in range(4):
        c = conversation_service.upsert_conversation(
            agent="opencode" if idx % 2 == 0 else "codex",
            project_id=proj["id"],
            title=f"Cross-Agent Session #{idx + 1}",
            external_id=f"ext:session-{idx + 1}",
            cwd=str(tmp_path / "repo"),
            process_summary=long_summary,
            messages=[
                {"role": "user", "content": f"[Session {idx + 1}] {long_prompt}"},
                {"role": "assistant", "content": f"[Session {idx + 1}] 已完成初步核对：" + ("记录阶段成果。" * 25)},
            ],
        )
        convs.append(c)

    return {"proj": proj, "dec": dec, "pref": pref, "task1": task1, "task2": task2, "convs": convs}


def test_relay_project_insufficient_budget_cleans_orphan_and_preserves_prior_draft(tmp_path, monkeypatch):
    fx = _setup_realistic_project(tmp_path, monkeypatch, "orphan-project.db")
    proj_id = fx["proj"]["id"]

    tok = authenticated_agent.set("omp")
    try:
        # 1. Valid baseline relay succeeds and records one awaiting_confirmation draft.
        ok_relay = mcp_server.relay_project(
            project_id=proj_id,
            task_id=fx["task1"]["id"],
            context_mode="compressed",
            max_tokens=24000,
        )
        assert ok_relay["status"] == "relayed"
        prior_hid = ok_relay["handoff_id"]

        with db() as conn:
            before_handoffs = conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]
            before_audits = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action IN ('handoff.create', 'handoff.supersede_draft')"
            ).fetchone()[0]

        # 2. Tight budget triggers pack-stage insufficient_budget refusal on task2.
        refused_pack = mcp_server.relay_project(
            project_id=proj_id,
            task_id=fx["task2"]["id"],
            context_mode="compressed",
            max_tokens=4000,
        )
        assert refused_pack["status"] == "insufficient_budget"
        assert refused_pack["must_not_execute"] is True
        assert refused_pack.get("handoff_id") is None
        assert refused_pack.get("handoff") is None
        assert (refused_pack.get("continuation") or {}).get("handoff_id") is None

        # 3. Full-mode tight budget passes pack-stage (~18KB) but fails final bound_context_response.
        refused_final = mcp_server.relay_project(
            project_id=proj_id,
            task_id=fx["task2"]["id"],
            context_mode="full",
            max_tokens=18000,
        )
        assert refused_final["status"] == "insufficient_budget"
        assert refused_final["must_not_execute"] is True
        assert refused_final.get("handoff_id") is None
        assert refused_final.get("handoff") is None
        assert (refused_final.get("continuation") or {}).get("handoff_id") is None

        with db() as conn:
            after_handoffs = conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]
            after_audits = conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action IN ('handoff.create', 'handoff.supersede_draft')"
            ).fetchone()[0]
        assert after_handoffs == before_handoffs
        assert after_audits == before_audits
        assert handoff_service.get_handoff(prior_hid)["status"] == "awaiting_confirmation"
    finally:
        authenticated_agent.reset(tok)


def test_relay_conversation_insufficient_budget_does_not_leave_orphan_handoff(tmp_path, monkeypatch):
    fx = _setup_realistic_project(tmp_path, monkeypatch, "orphan-conv.db")
    # Disambiguate open task by linking convs[0] explicitly to task1
    working_memory_service.upsert_working_task(
        title=fx["task1"]["title"],
        task_id=fx["task1"]["id"],
        project_id=fx["proj"]["id"],
        source_conversation_id=fx["convs"][0]["id"],
        expected_version=fx["task1"]["version"],
    )
    with db() as conn:
        before = conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]

    refused = handoff_service.relay_conversation(
        conversation_id=fx["convs"][0]["id"],
        consumer="omp",
        query="继续实现接口并运行单测",
        max_tokens=200,
        sync_first=False,
    )
    assert refused["status"] == "insufficient_budget"
    assert refused["must_not_execute"] is True
    assert refused.get("handoff_id") is None
    assert refused.get("handoff") is None
    assert (refused.get("continuation") or {}).get("handoff_id") is None

    with db() as conn:
        after = conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0]
    assert after == before


def test_relay_project_default_budget_and_compressed_progressive_digest_fit_real_prompts(tmp_path, monkeypatch):
    fx = _setup_realistic_project(tmp_path, monkeypatch, "default-budget.db")
    proj_id = fx["proj"]["id"]

    tok = authenticated_agent.set("omp")
    try:
        # Calling with default max_tokens (no max_tokens argument) must succeed without overflowing
        # and leave room for at least one recent cross-agent conversation digest.
        relayed = mcp_server.relay_project(
            project_id=proj_id,
            task_id=fx["task1"]["id"],
            context_mode="compressed",
        )
        assert relayed["status"] == "relayed"
        assert relayed["budget_tokens"] == 32000
        encoded_bytes = len(json.dumps(relayed, ensure_ascii=False).encode("utf-8"))
        assert encoded_bytes <= 32000
        assert relayed["estimated_tokens"] == encoded_bytes
        assert len(relayed["context"]["recent_conversation_digest"]) >= 1

        # Structural deduplication contract: compact handoff, slim source_revisions & $ref pointers.
        assert set(relayed["handoff"].keys()) == {"id", "status"}
        assert relayed["context"]["project"] == {"$ref": "#/project"}
        assert relayed["context"]["six_elements"] == {"$ref": "#/card"}
        assert relayed["continuation"]["card"] == {"$ref": "#/card"}
        assert relayed["continuation"]["readback"] == {"$ref": "#/readback"}
        for rev in (relayed["continuation"].get("source_revisions") or {}).values():
            if isinstance(rev, dict):
                assert "proposed" not in rev
                assert "process_summary" not in rev
                assert "directories" not in rev
        for d in relayed["continuation"].get("active_decisions") or []:
            assert "id" in d
            assert "content" not in d
        for d in relayed["continuation"].get("excluded_historical_decisions") or []:
            assert "id" in d
            assert "content" not in d

        # Protected rules remain intact and verbatim in both structured context and markdown.
        assert fx["dec"]["content"] in relayed["context_text"]
        assert any(fx["dec"]["content"] == item["content"] for item in relayed["context"]["active_decisions_and_constraints"])

        # Progressive omission honesty in compressed mode stays mathematically consistent.
        shown = len(relayed["context"]["recent_conversation_digest"])
        assert relayed["token_breakdown"]["conversation_count"] == shown
        assert relayed["omitted_conversations"] == len(fx["convs"]) - shown
        if relayed["omitted_conversations"] > 0:
            assert f"另有 {relayed['omitted_conversations']} 段会话没有写入这段摘要" in relayed["context_text"]

        # Compact digest items drop unrendered duplicate raw turn blobs and goal/tool copies already in context_text.
        assert fx["convs"][0]["messages"][0]["content"] in relayed["context_text"]
        for item in relayed["context"]["recent_conversation_digest"]:
            assert "latest_user_message" not in item
            assert "latest_assistant_reply" not in item
            assert "process_summary" not in item
            assert "user_goal" not in item
            assert "tool_pairs" not in item
            assert "brief_summary" not in item
            assert "tool_pair_count" in item

        # Tight explicit max_tokens=24000 also fits cleanly via progressive digest trimming.
        relayed_24 = mcp_server.relay_project(
            project_id=proj_id,
            task_id=fx["task1"]["id"],
            context_mode="compressed",
            max_tokens=24000,
        )
        assert relayed_24["status"] == "relayed"
        assert len(json.dumps(relayed_24, ensure_ascii=False).encode("utf-8")) <= 24000
    finally:
        authenticated_agent.reset(tok)


def test_assemble_context_default_budget_succeeds_with_active_real_handoff(tmp_path, monkeypatch):
    fx = _setup_realistic_project(tmp_path, monkeypatch, "assemble-default.db")
    proj_id = fx["proj"]["id"]

    tok = authenticated_agent.set("omp")
    try:
        relayed = mcp_server.relay_project(
            project_id=proj_id,
            task_id=fx["task1"]["id"],
            context_mode="compressed",
        )
        assert relayed["status"] == "relayed"

        assembled = mcp_server.assemble_context(
            task_query="float 元 价格",
            project_id=proj_id,
        )
        assert assembled["status"] == "ready"
        assert assembled["budget_tokens"] == 24000
        assert assembled["continuation"]["handoff_id"] == relayed["handoff_id"]
        assert assembled["continuation"]["card"] == relayed["card"]
        assert len(json.dumps(assembled, ensure_ascii=False).encode("utf-8")) <= 24000
    finally:
        authenticated_agent.reset(tok)


def test_rest_relay_project_accepts_selection_alias_and_caps_completed_tasks(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    fx = _setup_realistic_project(tmp_path, monkeypatch, "rest-selection.db")
    proj_id = fx["proj"]["id"]
    for idx in range(12):
        working_memory_service.upsert_working_task(
            title=f"Completed item #{idx + 1}",
            goal=f"Historical completed work #{idx + 1}",
            project_id=proj_id,
            status="completed",
            agent="codex",
        )

    client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
    session = client.get("/api/session").json()
    resp = client.post(
        "/api/handoffs/relay",
        headers={"X-CSRF-Token": session["csrf_token"]},
        json={
            "selection": proj_id,
            "target_agent": "omp",
            "task_id": fx["task1"]["id"],
            "context_mode": "compressed",
            "sync_first": False,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "relayed"
    assert body["project"]["id"] == proj_id
    assert len(body["context"]["completed_tasks"]) == 10
    assert body["context"]["omitted_completed_tasks"] == 2

