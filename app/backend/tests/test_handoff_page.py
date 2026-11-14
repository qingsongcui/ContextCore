"""Write-on-select five-field handoff pages for conversation relay."""
from __future__ import annotations

from app import mcp_server, transport
from app.database import create_schema, db, set_app_setting
from app.security import authenticated_agent
from app.services import conversation_service, handoff_page_service, project_service


def _enable_omp_compute():
    set_app_setting("compute_authorized", True)
    set_app_setting("compute_host", "omp")
    with db() as conn:
        now = __import__("app.database", fromlist=["now_iso"]).now_iso()
        conn.execute(
            """
            INSERT INTO connections (id,label,discovered,enabled,paused,paths,capabilities,created_at,updated_at)
            VALUES ('omp','OMP',1,1,0,'[]','{}',?,?)
            ON CONFLICT(id) DO UPDATE SET enabled=1, paused=0, updated_at=excluded.updated_at
            """,
            (now, now),
        )


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "handoff-page.db"))
    monkeypatch.setenv("CONTEXTCORE_DATA_DIR", str(tmp_path / "data"))
    create_schema()
    _enable_omp_compute()
    proj = project_service.create_project(name="handoff-page-demo", directories=[str(tmp_path / "repo")])
    conv = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="真机回归走查",
        external_id="cursor:handoff-page-1",
        cwd=str(tmp_path / "repo"),
        messages=[
            {"role": "user", "content": "把 test/relay-worktree-identity 的真机测试做完"},
            {"role": "assistant", "content": "已完成宿主库并发同步与接棒体积复测。"},
            {"role": "user", "content": "真机测试都测过了是吧？"},
            {"role": "assistant", "content": "核心链路测过了，可以准备提交。"},
        ],
    )
    return proj, conv


def _fake_page_result(**overrides):
    structured = {
        "goal": "把 test/relay-worktree-identity 的真机测试做完",
        "done": "宿主库并发同步与接棒体积复测",
        "do_not_redo": "已经测过的并发同步与体积复测",
        "unverified": "没有重打 Senrelay.app",
        "next_step": "整理提交，准备合并",
    }
    structured.update(overrides)
    return transport.DistillResult(status="success", structured=structured, summary=structured["next_step"])


def test_discovery_does_not_write_handoff_page(tmp_path, monkeypatch):
    proj, conv = _setup(tmp_path, monkeypatch)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("discovery must not call the model")

    monkeypatch.setattr(transport, "run_distill_sync", boom)
    tok = authenticated_agent.set("cursor")
    try:
        listed = mcp_server.discover_relay_conversations(cwd=str(tmp_path / "repo"), limit=10)
    finally:
        authenticated_agent.reset(tok)
    assert listed["ok"] is True
    assert calls["n"] == 0
    assert handoff_page_service.get_handoff_page(conv["id"]) is None
    briefs = [c.get("brief_summary") or "" for c in listed.get("candidates") or []]
    assert any("真机" in brief or "测" in brief for brief in briefs)


def test_selection_loads_full_transcript_compaction_without_calling_model(tmp_path, monkeypatch):
    proj, conv = _setup(tmp_path, monkeypatch)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("relay_conversation must not call background distillation model")

    monkeypatch.setattr(transport, "run_distill_sync", boom)
    tok = authenticated_agent.set("cursor")
    try:
        res = mcp_server.relay_conversation(conversation_id=conv["id"], cwd=str(tmp_path / "repo"))
    finally:
        authenticated_agent.reset(tok)

    assert res["ok"] is True
    assert calls["n"] == 0
    assert "handoff_page" not in res
    assert res["relay_mode"] == "full_transcript_compaction"
    assert res["transcript_paging"]["complete"] is True
    assert res["transcript_paging"]["total_messages"] == 4
    assert "交接纸" not in res["context_text"]
    assert any("真机测试都测过了是吧" in (m.get("content") or "") for m in res["selected_conversation"]["messages"])


def test_ensure_handoff_page_write_reuse_and_delta_unit(tmp_path, monkeypatch):
    proj, conv = _setup(tmp_path, monkeypatch)
    calls = {"n": 0, "payloads": []}

    def fake_run(host_id, payload, **kwargs):
        calls["n"] += 1
        calls["payloads"].append(payload)
        assert host_id == "omp"
        assert kwargs.get("parse_mode") == "handoff_page"
        return _fake_page_result()

    monkeypatch.setattr(transport, "run_distill_sync", fake_run)
    full_conv = conversation_service.get_conversation(conv["id"], include_messages=True)
    before_hash = full_conv["content_hash"]
    first = handoff_page_service.ensure_handoff_page(full_conv)
    second = handoff_page_service.ensure_handoff_page(full_conv)

    assert first["status"] == "ready"
    assert first["goal"].startswith("把 test/relay-worktree-identity")
    assert calls["n"] == 1
    assert second["reused"] is True
    assert calls["n"] == 1
    assert conversation_service.get_conversation(conv["id"], include_messages=True)["content_hash"] == before_hash

    updated = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="真机回归走查",
        external_id="cursor:handoff-page-1",
        cwd=str(tmp_path / "repo"),
        messages=[
            {"role": "user", "content": "把 test/relay-worktree-identity 的真机测试做完"},
            {"role": "assistant", "content": "已完成宿主库并发同步与接棒体积复测。"},
            {"role": "user", "content": "真机测试都测过了是吧？"},
            {"role": "assistant", "content": "核心链路测过了，可以准备提交。"},
            {"role": "user", "content": "再补一轮冒烟"},
            {"role": "assistant", "content": "好，开始冒烟。"},
        ],
    )

    def fake_delta(host_id, payload, **kwargs):
        calls["payloads"].append(payload)
        assert payload.get("previous_page") is not None
        assert min(m["index"] for m in payload["messages"]) >= 4
        return _fake_page_result(next_step="再跑一次冒烟")

    monkeypatch.setattr(transport, "run_distill_sync", fake_delta)
    inc = handoff_page_service.ensure_handoff_page(updated)
    assert inc["next_step"] == "再跑一次冒烟"
    assert inc["fresh"] is True


def test_compute_off_keeps_verbatim_without_invented_page(tmp_path, monkeypatch):
    proj, conv = _setup(tmp_path, monkeypatch)
    set_app_setting("compute_authorized", False)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("compute off must not call the model")

    monkeypatch.setattr(transport, "run_distill_sync", boom)
    tok = authenticated_agent.set("cursor")
    try:
        result = mcp_server.relay_conversation(conversation_id=conv["id"], cwd=str(tmp_path / "repo"))
    finally:
        authenticated_agent.reset(tok)
    assert calls["n"] == 0
    assert result["ok"] is True
    assert "handoff_page" not in result
    assert handoff_page_service.get_handoff_page(conv["id"]) is None
    assert "交接纸" not in result["context_text"]
    messages = result["selected_conversation"]["messages"]
    assert any("真机测试都测过了是吧" in (m.get("content") or "") for m in messages)


def test_oversized_field_rejects_without_truncating_ready_page(tmp_path, monkeypatch):
    proj, conv = _setup(tmp_path, monkeypatch)

    monkeypatch.setattr(transport, "run_distill_sync", lambda *a, **k: _fake_page_result())
    full_conv = conversation_service.get_conversation(conv["id"], include_messages=True)
    ready = handoff_page_service.ensure_handoff_page(full_conv)
    assert ready["status"] == "ready"

    updated = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="真机回归走查",
        external_id="cursor:handoff-page-1",
        cwd=str(tmp_path / "repo"),
        messages=[
            {"role": "user", "content": "把 test/relay-worktree-identity 的真机测试做完"},
            {"role": "assistant", "content": "已完成宿主库并发同步与接棒体积复测。"},
            {"role": "user", "content": "真机测试都测过了是吧？"},
            {"role": "assistant", "content": "核心链路测过了，可以准备提交。"},
            {"role": "user", "content": "补充说明"},
            {"role": "assistant", "content": "收到"},
        ],
    )

    monkeypatch.setattr(
        transport,
        "run_distill_sync",
        lambda *a, **k: _fake_page_result(done="x" * (handoff_page_service.FIELD_MAX_CHARS + 20)),
    )
    res = handoff_page_service.ensure_handoff_page(updated)
    assert res["status"] == "ready"
    assert res["done"] == ready["done"]
    assert res.get("write_error") and "exceeds" in res["write_error"]


def test_partial_or_clipped_relay_sets_transcript_paging_next_args(tmp_path, monkeypatch):
    proj, conv = _setup(tmp_path, monkeypatch)
    huge = "测过了。" + ("细节。" * 4000)
    updated = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="真机回归走查",
        external_id="cursor:handoff-page-1",
        cwd=str(tmp_path / "repo"),
        messages=[
            {"role": "user", "content": "真机测试都测过了是吧？"},
            {"role": "assistant", "content": huge},
        ],
    )
    tok = authenticated_agent.set("cursor")
    try:
        result = mcp_server.relay_conversation(
            conversation_id=updated["id"],
            cwd=str(tmp_path / "repo"),
            max_tokens=8000,
        )
    finally:
        authenticated_agent.reset(tok)
    assert result["ok"] is True
    assert "handoff_page" not in result
    assert result["transcript_paging"]["complete"] is False
    assert result["transcript_paging"]["next_args"] == {
        "conversation_id": updated["id"],
        "offset": 0,
        "limit": 30,
    }
    clipped = result["coverage"].get("clipped_message")
    assert clipped is not None and clipped.get("omitted_prefix_bytes", 0) > 0


def test_detail_endpoint_reads_page_without_writing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    proj, conv = _setup(tmp_path, monkeypatch)
    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        return _fake_page_result()

    monkeypatch.setattr(transport, "run_distill_sync", fake_run)
    page = handoff_page_service.ensure_handoff_page(
        conversation_service.get_conversation(conv["id"], include_messages=True)
    )
    assert page["status"] == "ready"
    writes = calls["n"]
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
    csrf = client.get("/api/session").json()["csrf_token"]
    res = client.get(
        f"/api/conversations/{conv['id']}?include_messages=false",
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["handoff_page"]["status"] == "ready"
    assert calls["n"] == writes


def test_fifty_tool_calls_trigger_transcript_paging_and_stay_within_budget(tmp_path, monkeypatch):
    import json

    proj, conv = _setup(tmp_path, monkeypatch)
    heavy_messages = []
    for idx in range(25):
        heavy_messages.append({"role": "user", "content": f"Step {idx}: inspect build & tests for module_{idx}"})
        heavy_messages.append(
            {
                "role": "assistant",
                "content": f"Running checks for module_{idx} and inspecting output " + ("x" * 120),
                "tool_calls": [
                    {"name": "run_command", "command": f"pytest tests/test_mod_{idx}_a.py -q"},
                    {"name": "read_file", "path": f"app/backend/app/mod_{idx}.py"},
                ],
            }
        )
    updated = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="真机回归走查",
        external_id="cursor:handoff-page-1",
        cwd=str(tmp_path / "repo"),
        messages=heavy_messages,
    )
    assert len(handoff_page_service.build_facts(updated)["commands"]) > 0

    tok = authenticated_agent.set("codex")
    try:
        result = mcp_server.relay_conversation(conversation_id=updated["id"], cwd=str(tmp_path / "repo"))
    finally:
        authenticated_agent.reset(tok)

    assert "handoff_page" not in result
    assert result["transcript_paging"]["complete"] is False
    assert result["transcript_paging"]["next_args"]["offset"] == 0
    encoded_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    assert encoded_bytes <= 12000


def test_timeout_error_preserves_ready_page_in_direct_service(tmp_path, monkeypatch):
    proj, _ = _setup(tmp_path, monkeypatch)

    def raise_timeout(*args, **kwargs):
        raise transport.DistillExecutionError("OMP CLI timed out", send_started=True, kind="timeout")

    conv2 = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="Second session",
        external_id="cursor:handoff-page-2",
        cwd=str(tmp_path / "repo"),
        messages=[
            {"role": "user", "content": "Initial task"},
            {"role": "assistant", "content": "Initial step done"},
        ],
    )
    monkeypatch.setattr(
        transport,
        "run_distill_sync",
        lambda *a, **kw: _fake_page_result(goal="Keep ready v1"),
    )
    ready_page = handoff_page_service.ensure_handoff_page(conv2)
    assert ready_page["status"] == "ready"

    conv2_updated = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="Second session",
        external_id="cursor:handoff-page-2",
        cwd=str(tmp_path / "repo"),
        messages=[
            {"role": "user", "content": "Initial task"},
            {"role": "assistant", "content": "Initial step done"},
            {"role": "user", "content": "Check timeout fallback"},
        ],
    )
    monkeypatch.setattr(transport, "run_distill_sync", raise_timeout)
    refreshed = handoff_page_service.ensure_handoff_page(conv2_updated)
    assert refreshed["status"] == "ready"
    assert refreshed["goal"] == "Keep ready v1"
    assert refreshed["stale"] is True
    assert "timed out" in (refreshed.get("write_error") or "")


def test_run_distill_handoff_page_fenced_json_usage_audit_and_paused_source_guard(tmp_path, monkeypatch):
    import asyncio
    import json

    proj, conv = _setup(tmp_path, monkeypatch)
    seen_prompts: list[str] = []
    fenced_fields = {
        "goal": "G1",
        "done": "D1",
        "do_not_redo": "R1",
        "unverified": "U1",
        "next_step": "N1",
    }
    omp_event = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "stopReason": "stop",
                "usage": {"input": 120, "output": 45, "totalTokens": 165},
                "content": [{"type": "text", "text": f"Here is the page:\n```json\n{json.dumps(fenced_fields)}\n```"}],
            },
        }
    ).encode("utf-8")

    class _FakeProc:
        returncode = 0
        pid = 999999

        async def communicate(self, input: bytes | None = None):
            seen_prompts.append((input or b"").decode("utf-8"))
            return omp_event + b"\n", b""

    async def _fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(transport, "build_distill_command", lambda host_id, overlay: (["/usr/bin/true"], True))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    res = asyncio.run(
        transport.run_distill(
            "omp",
            {
                "conversation_id": conv["id"],
                "messages": [{"role": "user", "content": "hi"}],
                "process_summary": "proc_tail",
            },
            allow_execution=True,
            parse_mode="handoff_page",
        )
    )
    assert res.status == "success"
    assert res.structured == fenced_fields
    assert res.usage and res.usage["tokens"] == 165
    assert "proc_tail" in seen_prompts[0]

    # Process-summary-only conversation also passes validation in handoff_page mode
    res_proc_only = asyncio.run(
        transport.run_distill(
            "omp",
            {"conversation_id": conv["id"], "messages": [], "process_summary": "proc_only_summary"},
            allow_execution=True,
            parse_mode="handoff_page",
        )
    )
    assert res_proc_only.status == "success"

    # Usage & audit logging on ensure_handoff_page
    res_with_usage = _fake_page_result()
    res_with_usage.usage = {"tokens": 165, "input_tokens": 120, "output_tokens": 45}
    monkeypatch.setattr(transport, "run_distill_sync", lambda *a, **kw: res_with_usage)
    tok = authenticated_agent.set("codex")
    try:
        written = handoff_page_service.ensure_handoff_page(conv)
    finally:
        authenticated_agent.reset(tok)
    assert written["status"] == "ready"
    with db() as conn:
        meter = conn.execute("SELECT * FROM usage_meters WHERE host_id = 'omp' ORDER BY created_at DESC LIMIT 1").fetchone()
        audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'handoff_page.write' AND target = ? ORDER BY created_at DESC LIMIT 1",
            (conv["id"],),
        ).fetchone()
    assert meter is not None and meter["tokens"] == 165
    assert audit is not None and audit["actor"] == "codex"

    # Paused source host guard blocks compute without calling model or writing error row
    with db() as conn:
        now = __import__("app.database", fromlist=["now_iso"]).now_iso()
        conn.execute(
            """
            INSERT INTO connections (id,label,discovered,enabled,paused,paths,capabilities,created_at,updated_at)
            VALUES ('cursor','Cursor',1,1,1,'[]','{}',?,?)
            ON CONFLICT(id) DO UPDATE SET enabled=1, paused=1, updated_at=excluded.updated_at
            """,
            (now, now),
        )
    conv_paused = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="Paused host chat",
        external_id="cursor:paused-1",
        cwd=str(tmp_path / "repo"),
        messages=[{"role": "user", "content": "should not compute"}],
    )
    monkeypatch.setattr(
        transport,
        "run_distill_sync",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("paused source must not invoke model")),
    )
    paused_page = handoff_page_service.ensure_handoff_page(conv_paused)
    assert paused_page["status"] == "unavailable"
    assert handoff_page_service.get_handoff_page(conv_paused["id"]) is None


def test_inplace_edit_delta_non_empty_and_oversized_input_capped(tmp_path, monkeypatch):
    import json

    proj, _ = _setup(tmp_path, monkeypatch)
    msgs_8 = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i}"} for i in range(8)]
    conv = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="In-place edit chat",
        external_id="cursor:inplace-8",
        cwd=str(tmp_path / "repo"),
        messages=msgs_8,
    )
    captured: list[dict] = []

    def capture_run(host_id, payload, **kwargs):
        captured.append(payload)
        return _fake_page_result(unverified="exact_model_unverified_value")

    monkeypatch.setattr(transport, "run_distill_sync", capture_run)
    p1 = handoff_page_service.ensure_handoff_page(conv)
    assert p1["status"] == "ready"
    assert p1["covered_message_count"] == 8

    # Edit turn 1 in place (message count stays 8 while content_hash changes)
    msgs_8_edited = list(msgs_8)
    msgs_8_edited[1] = {"role": "assistant", "content": "Turn 1 revised in place"}
    conv_edited = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="In-place edit chat",
        external_id="cursor:inplace-8",
        cwd=str(tmp_path / "repo"),
        messages=msgs_8_edited,
    )
    captured.clear()
    p2 = handoff_page_service.ensure_handoff_page(conv_edited)
    assert p2["status"] == "ready"
    assert captured[0]["previous_page"] is not None
    assert len(captured[0]["messages"]) > 0

    # Oversized 160k user turn + 40k tool turn capped under 125k without corrupting model unverified field
    captured.clear()
    conv_huge = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=proj["id"],
        title="Oversized transcript chat",
        external_id="cursor:oversized-1",
        cwd=str(tmp_path / "repo"),
        messages=[
            {"role": "user", "content": "U" * 160_000},
            {"role": "tool", "content": "T" * 40_000},
            {"role": "user", "content": "Final question stays readable"},
        ],
    )
    p_huge = handoff_page_service.ensure_handoff_page(conv_huge)
    assert p_huge["status"] == "ready"
    assert p_huge["unverified"] == "exact_model_unverified_value"
    assert len(json.dumps(captured[0]["messages"], ensure_ascii=False)) <= 125_000
    with db() as conn:
        row = conn.execute(
            "SELECT facts_json FROM conversation_handoff_pages WHERE conversation_id = ?",
            (conv_huge["id"],),
        ).fetchone()
    assert row is not None
    stored_facts = json.loads(row["facts_json"])
    assert stored_facts.get("coverage_note")


def test_mcp_read_conversation_messages_paging_and_external_id(tmp_path, monkeypatch):
    proj, _ = _setup(tmp_path, monkeypatch)
    msgs_35 = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"Message {i}"} for i in range(35)]
    conv = conversation_service.upsert_conversation(
        agent="antigravity",
        project_id=proj["id"],
        title="Paging 35 turns",
        external_id="uuid-native-page-35",
        cwd=str(tmp_path / "repo"),
        messages=msgs_35,
    )
    tok = authenticated_agent.set("codex")
    try:
        page1 = mcp_server.read_conversation_messages(conversation_id=conv["id"], offset=0, limit=15)
        page_last = mcp_server.read_conversation_messages(conversation_id="uuid-native-page-35", offset=30, limit=15)
    finally:
        authenticated_agent.reset(tok)

    assert page1["ok"] is True
    assert page1["total"] == 35
    assert len(page1["items"]) == 15
    assert page1["next_offset"] == 15
    assert page1["items"][0]["content"] == "Message 0"

    assert page_last["ok"] is True
    assert page_last["total"] == 35
    assert len(page_last["items"]) == 5
    assert page_last["next_offset"] is None
    assert page_last["has_more"] is False
    assert page_last["items"][-1]["content"] == "Message 34"


def test_mcp_read_conversation_messages_bounds_large_pages_and_advances_all_turns(tmp_path, monkeypatch):
    import json

    proj, _ = _setup(tmp_path, monkeypatch)
    large_msgs = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"TURN_{i:02d}_" + ("正文内容" * 1500),
            "tool_calls": [{"name": "run_command", "arguments": json.dumps({"cmd": "x" * 2000})}] if i % 2 == 1 else None,
        }
        for i in range(12)
    ]
    conv = conversation_service.upsert_conversation(
        agent="antigravity",
        project_id=proj["id"],
        title="Large 12 turns",
        external_id="uuid-large-12",
        cwd=str(tmp_path / "repo"),
        messages=large_msgs,
    )
    collected = []
    offset = 0
    tok = authenticated_agent.set("codex")
    try:
        while offset is not None:
            res = mcp_server.read_conversation_messages(conversation_id=conv["id"], offset=offset, limit=30)
            assert res["ok"] is True
            assert len(json.dumps(res, ensure_ascii=False).encode("utf-8")) <= mcp_server._MAX_MCP_TRANSCRIPT_PAGE_BYTES + 2048
            assert res["returned_count"] >= 1
            collected.extend(res["items"])
            offset = res["next_offset"]
    finally:
        authenticated_agent.reset(tok)

    assert len(collected) == 12
    assert all(f"TURN_{idx:02d}_" in collected[idx]["content"] for idx in range(12))
