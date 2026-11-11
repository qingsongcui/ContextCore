"""Comprehensive regression tests for backend audit fixes across Groups 1–6."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import cli, mcp_server, runtime
from app.adapters.antigravity import (
    _conversation_external_id,
    _parse_transcript,
    _resolve_hook_transcript_file,
    normalize_antigravity_hook,
)
from app.adapters.cursor import _decode_store_blob_message, _parse_cursor_store_db
from app.config import settings
from app.database import backup_database, create_schema, db
from app.integrations import cc_hook
from app.integrations.config_transactions import ConfigurationError
from app.integrations.secret_store import SecretStore
from app.main import app
from app.models import Conflict
from app.security import ALLOWED_ORIGINS, SecurityMiddleware
from app.services import (
    conflict_service,
    connection_service,
    content_service,
    conversation_service,
    distillation_service,
    handoff_service,
    management_experience,
    management_service,
    migration_service,
    project_service,
    provider_checks,
    retrieval_service,
    routing_service,
)
from app.services.host_registry import HOST_DEFINITIONS
from app.services.management_service import ManagementService, set_secret_store
from app.services.oauth_service import OAuthService
from app.transport import CodexAppServerClient


class _InMemoryVault(SecretStore):
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def status(self) -> dict[str, object]:
        return {"available": True, "backend": "memory"}

    def put(self, secret: str) -> str:
        ref = f"keychain:senrelay:{len(self.items) + 1:032x}"
        self.items[ref] = secret
        return ref

    def get(self, reference: str) -> str:
        return self.items[reference]

    def delete(self, reference: str) -> None:
        self.items.pop(reference, None)


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    v = int(value)
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def _pb_varint_field(field_no: int, value: int) -> bytes:
    return _encode_varint((field_no << 3) | 0) + _encode_varint(value)


def _pb_bytes_field(field_no: int, payload: bytes) -> bytes:
    return _encode_varint((field_no << 3) | 2) + _encode_varint(len(payload)) + payload


# ---------------------------------------------------------------------------
# Group 1: Continuation triggers & selection-only relay queries
# ---------------------------------------------------------------------------


def test_group1_continuation_triggers_and_selection_only_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "group1.db"))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    for trigger in ("接力", "继续接力", "项目接力", "继续", "接着做", "接棒"):
        assert handoff_service._is_continuation_prompt(trigger) is True

    selection_only_queries = [
        None,
        "",
        "   ",
        "继续",
        "接着做",
        "接棒",
        "接力",
        "/contextcore",
        "/handoff",
        "/continue",
        "continue",
        "1",
        "2",
        "10",
        "#1",
        "[1]",
        "(2)",
        "选 2",
        "选择第 1 个",
        "第 3 条",
        "继续 #2",
        "接力 1",
        "/contextcore 1",
        "select option 2",
        "item #3",
        "conv_abc123def456",
        "antigravity:6acc896e-9e05-4abe",
        "codex:rollout-2026-11-10",
        "claude-code:session-42",
        "cursor:composer-99",
        "omp:sess-001",
        "opencode:ses_987",
    ]
    for q in selection_only_queries:
        assert handoff_service._relay_has_task_instruction(q) is False, f"Expected False for {q!r}"

    task_queries = [
        "1 补上可见性测试断言",
        "选 2，把登录超时改成 30 秒",
        "补上可见性测试断言",
        "continue and add regression test for timeout",
        "antigravity:6acc896e 修复空指针异常",
    ]
    for q in task_queries:
        assert handoff_service._relay_has_task_instruction(q) is True, f"Expected True for {q!r}"

    # Verify same-agent / unlinked relay_conversation respects _relay_has_task_instruction
    conv = conversation_service.upsert_conversation(
        agent="codex",
        title="Fix visibility assertions",
        external_id="codex:unlinked-1",
        cwd=str(tmp_path / "unlinked_repo"),
        messages=[
            {"role": "user", "content": "请检查可见性断言逻辑"},
            {"role": "assistant", "content": "已定位可见性断言缺失位置"},
        ],
    )

    res_no_task = handoff_service.relay_conversation(
        conversation_id=conv["id"],
        consumer="codex",
        sync_first=False,
        query="1",
    )
    assert res_no_task["status"] == "loaded"
    assert res_no_task["must_not_execute"] is True
    assert res_no_task["continuation"]["status"] == "awaiting_confirmation"

    res_with_task = handoff_service.relay_conversation(
        conversation_id=conv["id"],
        consumer="codex",
        sync_first=False,
        query="1 补上可见性测试断言",
    )
    assert res_with_task["status"] in ("confirmed", "ready", "loaded")
    assert res_with_task["must_not_execute"] is False
    assert res_with_task["continuation"]["status"] in ("confirmed", "ready")


# ---------------------------------------------------------------------------
# Group 2: read_handoff readback, >32KB evidence bound, relay_project fallback
# ---------------------------------------------------------------------------


def test_group2_read_handoff_readback_budget_and_relay_project_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "group2.db"))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    ws = tmp_path / "proj_ws"
    ws.mkdir()
    proj = project_service.create_project("Group2Proj", "desc", [str(ws)])

    huge_turn = "A" * 15000
    conv = conversation_service.upsert_conversation(
        agent="claude-code",
        project_id=proj["id"],
        external_id="claude-code:huge-1",
        cwd=str(ws),
        title="Large turn handoff test",
        messages=[
            {"role": "user", "content": f"User spec: {huge_turn}"},
            {"role": "assistant", "content": f"Assistant analysis: {huge_turn}"},
            {"role": "user", "content": f"Followup spec: {huge_turn}"},
            {"role": "assistant", "content": f"Followup analysis: {huge_turn}"},
        ],
    )

    handoff = handoff_service.create_handoff(
        project_id=proj["id"],
        source_agent="claude-code",
        target_agent="codex",
        conversation_id=conv["id"],
    )
    assert handoff["status"] == "awaiting_confirmation"
    card_bytes = len(json.dumps(handoff["card"], ensure_ascii=False).encode("utf-8"))
    assert card_bytes < 32000, f"Card JSON exceeded 32KB budget: {card_bytes}"
    for ev in handoff["card"].get("evidence", []):
        if isinstance(ev, str) and ev.startswith("[turn="):
            assert len(ev) <= 4200

    # MCP read_handoff on awaiting_confirmation returns readback-only payload without deadlocking
    monkeypatch.setattr(mcp_server, "_actor", lambda: "codex")
    readback_res = mcp_server.read_handoff(handoff_id=handoff["id"])
    assert readback_res["status"] == "awaiting_confirmation"
    assert readback_res["must_not_execute"] is True
    assert readback_res["card"]["goal"]
    assert "Read back all 6 fields" in readback_res["instructions"]

    # Confirming allows assemble_context under 32KB ceiling without insufficient_budget
    handoff_service.confirm_handoff(handoff["id"], confirmed=True, actor="codex")
    ctx = retrieval_service.assemble_context(
        query="继续推进",
        project_id=proj["id"],
        budget_tokens=32000,
        consumer_id="codex",
        handoff_id=handoff["id"],
    )
    assert ctx.get("status") != "insufficient_budget"
    assert len(json.dumps(ctx, ensure_ascii=False).encode("utf-8")) <= 32000
    tok_ctx = mcp_server.authenticated_agent.set("codex")
    try:
        mcp_ctx = mcp_server.assemble_context(
            task_query="继续推进",
            project_id=proj["id"],
            max_tokens=32000,
            handoff_id=handoff["id"],
        )
    finally:
        mcp_server.authenticated_agent.reset(tok_ctx)
    assert mcp_ctx.get("status") != "insufficient_budget"
    assert len(json.dumps(mcp_ctx, ensure_ascii=False).encode("utf-8")) <= 32000

    # Cancelled handoffs must still be rejected by read_handoff
    cancelled_h = handoff_service.create_handoff(
        project_id=proj["id"],
        source_agent="claude-code",
        target_agent="codex",
        conversation_id=conv["id"],
    )
    handoff_service.cancel_handoff(cancelled_h["id"])
    with pytest.raises(ValueError, match="cancelled|confirmed"):
        mcp_server.read_handoff(handoff_id=cancelled_h["id"])

    # Web UI task (source_agent="user") fallback + orphan relay_project draft supersession
    ws2 = tmp_path / "proj_relay_ws"
    ws2.mkdir()
    proj_relay = project_service.create_project("Group2RelayProj", "desc", [str(ws2)])
    conversation_service.upsert_conversation(
        agent="claude-code",
        project_id=proj_relay["id"],
        external_id="claude-code:relay-src-1",
        cwd=str(ws2),
        title="Audit feature planning",
        messages=[{"role": "user", "content": "Plan audit endpoint implementation"}],
    )
    web_task_1 = content_service.create_content(
        project_id=proj_relay["id"],
        category="task",
        title="Web UI Task 1",
        content="Implement audit endpoint",
        agent="user",
    )
    web_task_2 = content_service.create_content(
        project_id=proj_relay["id"],
        category="task",
        title="Web UI Task 2",
        content="Add audit regression tests",
        agent="user",
    )
    r1 = handoff_service.relay_project(
        project_id=proj_relay["id"],
        consumer="codex",
        task_id=web_task_1["id"],
        max_tokens=24000,
        sync_first=False,
        query="项目接力",
    )
    assert r1["status"] == "relayed"
    assert r1["continuation"]["status"] == "awaiting_confirmation"
    first_hid = r1["handoff"]["id"]

    r2 = handoff_service.relay_project(
        project_id=proj_relay["id"],
        consumer="codex",
        task_id=web_task_2["id"],
        max_tokens=24000,
        sync_first=False,
        query="项目接力",
    )
    assert r2["status"] == "relayed"
    assert r2["continuation"]["status"] == "awaiting_confirmation"
    second_hid = r2["handoff"]["id"]
    assert second_hid != first_hid
    assert handoff_service.get_handoff(first_hid)["status"] == "cancelled"

    lookup = handoff_service.lookup_continuation(
        "codex",
        project_id=proj_relay["id"],
        query="项目接力",
    )
    assert lookup is not None
    assert lookup["status"] != "ambiguous_continuation"
    assert lookup["handoff_id"] == second_hid


# ---------------------------------------------------------------------------
# Group 3: Cross-port same-site browser protection & middleware order
# ---------------------------------------------------------------------------


def test_group3_cross_port_same_site_and_sec_fetch_dest_protection(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "group3.db"))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    # SecurityMiddleware must be outermost (index 0 in app.user_middleware)
    assert app.user_middleware, "Expected middleware stack"
    assert app.user_middleware[0].cls is SecurityMiddleware

    client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 51234))

    # Legitimate local CLI / same-origin requests succeed
    assert client.get("/api/session").status_code == 200
    assert (
        client.get(
            "/api/session",
            headers={"Origin": next(iter(ALLOWED_ORIGINS)), "Sec-Fetch-Site": "same-origin"},
        ).status_code
        == 200
    )

    # Cross-port localhost browser request (Sec-Fetch-Site: same-site + disallowed/missing Origin) -> 403
    cross_port = client.get(
        "/api/session",
        headers={"Origin": "http://localhost:3000", "Sec-Fetch-Site": "same-site"},
    )
    assert cross_port.status_code == 403

    cross_port_no_origin = client.get(
        "/api/handoffs/relay/conversation",
        headers={"Sec-Fetch-Site": "same-site"},
    )
    assert cross_port_no_origin.status_code == 403
    assert "same-site" in cross_port_no_origin.json()["detail"].lower()

    # Passive subresource / embed destinations on /api/* -> 403
    for dest in ("script", "image", "iframe", "style", "object", "embed", "audio", "video"):
        resp = client.get("/api/session", headers={"Sec-Fetch-Dest": dest})
        assert resp.status_code == 403, f"Expected 403 for Sec-Fetch-Dest={dest}"


# ---------------------------------------------------------------------------
# Group 4: Quote validation, negation window, null conflict existing_id,
#          save_memory decision conflict, and consolidate_memories safety
# ---------------------------------------------------------------------------


def test_group4_distillation_quote_negation_conflicts_and_consolidation(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "group4.db"))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    # 1. Tiny 4-char quote cannot auto-confirm a long fabricated claim
    assert (
        distillation_service._claim_supported_by_quote(
            "我们决定在所有生产集群永久开启全量明文调试日志并禁用鉴权",
            "调试日志",
        )
        is False
    )
    assert (
        distillation_service._claim_supported_by_quote(
            "使用 SQLite WAL 模式",
            "我们确定使用 SQLite WAL 模式作为本地存储引擎",
        )
        is True
    )

    # 2. Negation detection catches sliced quote preceded by negation in message text
    assert (
        distillation_service._unconfirmed_decision_evidence(
            text="在生产环境开启 debug",
            ev_msg_text="注意：千万不要在生产环境开启 debug，否则会泄露密钥！",
        )
        is True
    )
    assert (
        distillation_service._unconfirmed_decision_evidence(
            text="enable verbose auth logging in production",
            ev_msg_text="Warning: please do not enable verbose auth logging in production.",
        )
        is True
    )

    # 3. Null existing_id Conflict model & resolve_conflict resilience
    proj = project_service.create_project("Group4Proj")
    conversation_service.upsert_conversation(
        agent="claude-code",
        project_id=proj["id"],
        external_id="claude-code:quote-src",
        title="Cache architecture chat",
        messages=[{"role": "user", "content": "我们确定使用本地 SQLite 缓存表"}],
    )
    model_inst = Conflict(
        id="conf_null_test",
        project_id=proj["id"],
        existing_id=None,
        proposed={"title": "Logging policy", "content": "Disable debug in prod", "category": "decision", "tags": ["sec"], "quote": "严禁开启 debug"},
        reason="legacy null existing_id",
        status="pending",
        created_at="2026-11-10T00:00:00Z",
    )
    assert model_inst.existing_id is None

    with db() as conn:
        conn.execute(
            """INSERT INTO conflicts (id, project_id, existing_id, proposed, reason, status, created_at)
               VALUES (?, ?, NULL, ?, ?, 'pending', '2026-11-10T00:00:00Z')""",
            (
                "conf_null_replace",
                proj["id"],
                json.dumps(
                    {
                        "title": "Cache backend",
                        "content": "Use local SQLite cache table",
                        "category": "decision",
                        "tags": ["provenance:user"],
                        "quote": "确定使用本地 SQLite 缓存表",
                    },
                    ensure_ascii=False,
                ),
                "dangling conflict test",
            ),
        )
        conn.execute(
            """INSERT INTO conflicts (id, project_id, existing_id, proposed, reason, status, created_at)
               VALUES (?, ?, NULL, ?, ?, 'pending', '2026-11-10T00:00:00Z')""",
            (
                "conf_null_keep",
                proj["id"],
                json.dumps({"title": "Keep test", "content": "No-op", "category": "decision"}),
                "dangling keep test",
            ),
        )

    listed = conflict_service.list_conflicts(project_id=proj["id"])
    assert {c["id"] for c in listed} >= {"conf_null_replace", "conf_null_keep"}

    resolved_keep = conflict_service.resolve_conflict("conf_null_keep", "keep")
    assert resolved_keep["status"] == "resolved"

    resolved_replace = conflict_service.resolve_conflict("conf_null_replace", "replace")
    assert resolved_replace["status"] == "resolved"
    created_decisions = [
        m for m in content_service.list_content(project_id=proj["id"], category="decision")
        if m["title"] == "Cache backend"
    ]
    assert len(created_decisions) == 1
    assert "provenance:user" in created_decisions[0]["tags"]
    assert created_decisions[0]["content"] == "Use local SQLite cache table"
    with db() as conn:
        src_row = conn.execute(
            "SELECT quote FROM content_sources WHERE content_id = ?",
            (created_decisions[0]["id"],),
        ).fetchone()
    assert src_row is not None and src_row["quote"] == "确定使用本地 SQLite 缓存表"

    # 4. mcp_server.save_memory routes implicit same-title decision collision to pending conflict
    monkeypatch.setattr(mcp_server, "_actor", lambda: "codex")
    save_conflict = mcp_server.save_memory(
        project_id=proj["id"],
        title="Cache backend",
        content="Switch cache backend to Redis cluster",
        category="decision",
    )
    assert save_conflict["status"] == "conflict_pending"
    assert save_conflict["existing_id"] == created_decisions[0]["id"]
    assert content_service.get_content(created_decisions[0]["id"])["status"] == "active"

    # 5. consolidate_memories skips decisions/tasks, honors tombstones without re-merging duplicates[0], and invalidates handoffs
    deleted_pref = content_service.create_content(
        project_id=proj["id"],
        category="preference",
        title="Style rule",
        content="prefer   explicit   nullish   checks\nin   TypeScript\navoid   any",
    )
    content_service.delete_content(deleted_pref["id"])

    # Create 3 preferences where none matches the tombstone alone, but merging h1+h2+h3 produces normalized text matching the deleted preference
    h1 = content_service.create_content(
        project_id=proj["id"],
        category="preference",
        title="Style rule",
        content="prefer explicit nullish checks\nin TypeScript",
    )
    h2 = content_service.create_content(
        project_id=proj["id"],
        category="preference",
        title="Style rule",
        content="prefer explicit nullish checks\navoid any",
    )
    h3 = content_service.create_content(
        project_id=proj["id"],
        category="preference",
        title="Style rule",
        content="prefer explicit nullish checks\nin TypeScript",
    )
    d1 = content_service.create_content(
        project_id=proj["id"],
        category="decision",
        title="Auth token TTL",
        content="Token TTL is 15 minutes",
    )
    d2 = content_service.create_content(
        project_id=proj["id"],
        category="decision",
        title="Auth token TTL",
        content="Token TTL is 60 minutes",
    )

    cons = content_service.consolidate_memories(proj["id"])
    assert cons["merged_groups"] == 0
    # Neither h1/h2/h3 (blocked by tombstone without re-merging h3) nor d1/d2 (decisions skipped) were merged
    for item_id in (h1["id"], h2["id"], h3["id"], d1["id"], d2["id"]):
        assert content_service.get_content(item_id)["status"] == "active"


# ---------------------------------------------------------------------------
# Group 5: Antigravity surfaces/hooks/transcripts, Cursor DAG/protobuf, Codex transport
# ---------------------------------------------------------------------------


def test_group5_antigravity_cursor_and_codex_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "group5.db"))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    # 1. Antigravity surfaces include "ide"
    assert HOST_DEFINITIONS["antigravity"].surfaces == ("desktop", "cli", "ide")

    # 2. cc_hook multi-event queue preserves order, client cwd, surface, and never clobbers
    import hashlib

    fake_home = tmp_path / "agy_home"
    fake_home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("ANTIGRAVITY_SURFACE", "ide")
    cred_dir = fake_home / ".contextcore" / "credentials"
    cred_dir.mkdir(parents=True, mode=0o700)
    (fake_home / ".contextcore").chmod(0o700)
    cred_dir.chmod(0o700)
    tok_file = cred_dir / "antigravity.token"
    tok_file.write_text("test-agy-token-123", encoding="utf-8")
    tok_file.chmod(0o600)

    client_ws = tmp_path / "client_workspace"
    client_ws.mkdir()
    monkeypatch.chdir(client_ws)

    assert cc_hook.enqueue_notification("antigravity", "Stop", {"sessionId": "s-1", "seq": 1}) is True
    assert cc_hook.enqueue_notification("antigravity", "Stop", {"sessionId": "s-1", "seq": 2}) is True
    q_files = sorted((fake_home / ".contextcore" / "notifications").glob("antigravity-*.json"))
    assert len(q_files) == 2

    delivered: list[tuple[str, dict]] = []
    daemon_dir = tmp_path / "daemon_cwd"
    daemon_dir.mkdir()
    monkeypatch.chdir(daemon_dir)

    # Also enqueue an event without cwd to verify drain_notifications does not overwrite missing cwd with daemon_cwd
    import time as _time

    now_sec = _time.time()
    raw_no_cwd = fake_home / ".contextcore" / "notifications" / "antigravity-99999999999999999999-1-0001.json"
    raw_no_cwd.write_text(
        json.dumps(
            {
                "agent_id": "antigravity",
                "event": "Stop",
                "payload": {"sessionId": "s-nocwd"},
                "base_url": "http://127.0.0.1:8420",
                "created_at": now_sec,
                "created_at_ns": int(now_sec * 1_000_000_000),
                "credential_hash": hashlib.sha256(b"test-agy-token-123").hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    raw_no_cwd.chmod(0o600)

    def _fake_post(
        agent_id,
        event_arg,
        raw_payload,
        base_url="http://127.0.0.1:8420",
        timeout=3.0,
        read_response=True,
        *,
        preserve_missing_cwd=False,
    ):
        payload = dict(raw_payload)
        if not preserve_missing_cwd and "cwd" not in payload:
            payload["cwd"] = os.getcwd()
        delivered.append((agent_id, payload))
        return {"ok": True}

    monkeypatch.setattr(cc_hook, "post_to_contextcore", _fake_post)
    cc_hook.drain_notifications()
    assert len(delivered) == 3
    assert [d[1].get("seq") for d in delivered[:2]] == [1, 2]
    assert delivered[0][1]["cwd"] == str(client_ws)
    assert delivered[0][1]["surface"] == "ide"
    assert "cwd" not in delivered[2][1]
    assert list((fake_home / ".contextcore" / "notifications").glob("*.json")) == []

    # 3. Antigravity hook path validation, transcript_full.jsonl preference, surface inference, tool_calls + token usage
    brain_conv_dir = fake_home / ".gemini" / "antigravity" / "brain" / "conv-xyz" / ".system_generated" / "logs"
    brain_conv_dir.mkdir(parents=True)
    compact_path = brain_conv_dir / "transcript.jsonl"
    full_path = brain_conv_dir / "transcript_full.jsonl"
    compact_path.write_text(
        json.dumps({"type": "USER_INPUT", "content": "short", "created_at": "2026-11-10T10:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    full_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "USER_INPUT", "content": "Run full check", "created_at": "2026-11-10T10:00:00Z"}),
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "content": "",
                        "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest -q"}}],
                        "input_tokens": 1200,
                        "output_tokens": 85,
                        "cache_read_tokens": 400,
                        "created_at": "2026-11-10T10:00:02Z",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    outside_file = tmp_path / "arbitrary_secret.jsonl"
    outside_file.write_text('{"type":"USER_INPUT","content":"secret"}\n', encoding="utf-8")
    assert _resolve_hook_transcript_file(str(outside_file), None) is None

    resolved_tf = _resolve_hook_transcript_file(str(compact_path), None)
    assert resolved_tf == full_path.resolve()

    # Path-first surface inference ensures ~/.gemini/antigravity/brain/<id> maps to antigravity:<id>
    assert _conversation_external_id(full_path, "conv-xyz", surface="cli") == "antigravity:conv-xyz"
    assert (
        _conversation_external_id(
            fake_home / ".gemini" / "antigravity-ide" / "brain" / "conv-ide" / "transcript.jsonl",
            "conv-ide",
            surface="desktop",
        )
        == "antigravity:ide:conv-ide"
    )

    parsed_hook = normalize_antigravity_hook(
        {"sessionId": "conv-xyz", "transcriptPath": str(compact_path), "surface": "cli", "cwd": str(client_ws)}
    )
    assert parsed_hook is not None
    assert parsed_hook["external_id"] == "antigravity:conv-xyz"
    assert "tool_call: run_command" in (parsed_hook.get("process_summary") or "")
    assert parsed_hook["token_usage"]["input_tokens"] == 1200
    assert parsed_hook["token_usage"]["output_tokens"] == 85
    assert parsed_hook["token_usage"]["cache_read_tokens"] == 400

    # 4. Cursor store.db multi-level cyclic DAG traversal & strict protobuf wire decoding
    store_dir = tmp_path / "cursor_chats" / "wshash" / "chat-dag-1"
    store_dir.mkdir(parents=True)
    store_db = store_dir / "store.db"

    leaf1_bytes = _pb_varint_field(1, 99) + _pb_bytes_field(2, b"user") + _pb_bytes_field(4, b"system")
    nested_part = _pb_bytes_field(1, b"Nested assistant reply with details")
    leaf2_bytes = _pb_bytes_field(1, b"assistant") + _pb_bytes_field(3, nested_part)

    leaf1_id = "11" * 32
    leaf2_id = "22" * 32
    mid_id = "aa" * 32
    root_id = "ff" * 32

    # Cycle: mid references root_id, leaf1_id, leaf2_id
    mid_bytes = (
        _pb_bytes_field(1, bytes.fromhex(root_id))
        + _pb_bytes_field(1, bytes.fromhex(leaf1_id))
        + _pb_bytes_field(1, bytes.fromhex(leaf2_id))
    )
    root_bytes = _pb_bytes_field(1, bytes.fromhex(mid_id))

    conn = sqlite3.connect(store_db)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    meta_hex = json.dumps({"agentId": "chat-dag-1", "latestRootBlobId": root_id, "name": "DAG Test"}).encode().hex()
    conn.execute("INSERT INTO meta VALUES ('0', ?)", (meta_hex,))
    for bid, bdata in ((root_id, root_bytes), (mid_id, mid_bytes), (leaf1_id, leaf1_bytes), (leaf2_id, leaf2_bytes)):
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (bid, bdata))
    conn.commit()
    conn.close()

    conn_read = sqlite3.connect(store_db)
    try:
        cursor_sessions = _parse_cursor_store_db(conn_read.cursor(), store_db.resolve())
    finally:
        conn_read.close()
    assert len(cursor_sessions) == 1
    parsed_cursor = cursor_sessions[0]
    assert len(parsed_cursor["messages"]) == 2
    # Role string "user" in field 2 took precedence over varint 99 in field 1, and body text "system" in field 4 did NOT hijack role!
    assert parsed_cursor["messages"][0]["role"] == "user"
    assert parsed_cursor["messages"][0]["content"] == "system"
    assert parsed_cursor["messages"][1]["role"] == "assistant"
    assert "Nested assistant reply with details" in parsed_cursor["messages"][1]["content"]

    # Invalid wire type (7) or truncated payload must return None
    assert _decode_store_blob_message(b"\x0fhello") is None
    assert _decode_store_blob_message(b"\x0a\x20short") is None

    # 5. Codex transport notification-skipping & 8 MiB limit
    src_init = inspect.getsource(CodexAppServerClient.initialize)
    assert "8 * 1024 * 1024" in src_init
    assert "DEVNULL" in src_init

    async def _check_notification_skip():
        client_rpc = CodexAppServerClient()
        reader = asyncio.StreamReader(limit=8 * 1024 * 1024)
        reader.feed_data((json.dumps({"method": "thread/updated", "params": {"id": "t1"}}) + "\n").encode())
        reader.feed_data((json.dumps({"id": 1, "result": {"ok": True, "big": "x" * 70000}}) + "\n").encode())
        reader.feed_eof()

        class _DummyStdin:
            def write(self, _b):
                pass

            async def drain(self):
                pass

        client_rpc._proc = SimpleNamespace(stdin=_DummyStdin(), stdout=reader, returncode=None)
        resp = await client_rpc._send_request("thread/list", {})
        assert resp is not None and resp["ok"] is True and len(resp["big"]) == 70000

    asyncio.run(_check_notification_skip())


# ---------------------------------------------------------------------------
# Group 6: SSRF DNS/IP blocking, backup_database guards, runtime/CLI stop,
#          sync_local_environment safety, sync_to_hosts conflicts, CC Switch cleanup
# ---------------------------------------------------------------------------


def test_group6_ssrf_backup_stop_and_management_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "group6.db"))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    # 1. SSRF literal + IPv4-mapped IPv6 + DNS-resolved private IP blocking
    for blocked_host in ("10.0.0.1", "169.254.169.254", "192.168.1.10", "::ffff:127.0.0.1", "::ffff:169.254.169.254"):
        assert provider_checks._blocked_special_ip(blocked_host) is True

    assert provider_checks._blocked_special_ip("127.0.0.1", allow_loopback=True) is False
    assert provider_checks._blocked_special_ip("127.0.0.1", allow_loopback=False) is True

    # Simulate DNS rebinding resolving an external hostname to 169.254.169.254
    orig_getaddrinfo = socket.getaddrinfo

    def _fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "rebind.attacker.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port or 443))]
        if host == "offline.unit.example":
            raise socket.gaierror(8, "nodename nor servname provided")
        return orig_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    assert provider_checks._blocked_special_ip("rebind.attacker.example") is True
    assert provider_checks._blocked_special_ip("offline.unit.example") is False

    for bad_url in (
        "https://10.0.0.1/v1",
        "https://169.254.169.254/v1",
        "https://rebind.attacker.example/v1",
        "http://[::ffff:127.0.0.1]:8080/v1",
    ):
        with pytest.raises(ConfigurationError):
            ManagementService._endpoint(bad_url)
        with pytest.raises(ConfigurationError):
            OAuthService._https_endpoint(bad_url)
        with pytest.raises(HTTPException):
            provider_checks._base({"base_url": bad_url, "protocol": "openai", "enabled": True})

    # 2. backup_database rejects live DB self-overwrite and existing -wal/-shm sidecars
    live_db = tmp_path / "group6.db"
    with pytest.raises(OSError, match="live database"):
        backup_database(destination=live_db, db_path=live_db)

    sidecar_target = tmp_path / "existing_target.db"
    sidecar_target.write_bytes(b"placeholder")
    Path(f"{sidecar_target}-wal").write_bytes(b"active-wal")
    with pytest.raises(OSError, match="sidecar"):
        backup_database(destination=sidecar_target, db_path=live_db)

    valid_backup = backup_database(destination=tmp_path / "clean_backup.db", db_path=live_db)
    assert valid_backup["ok"] is True
    assert Path(valid_backup["backup_path"]).is_file()

    # 3. runtime.stop_service(stop_port_owner=True) and CLI --stop-port-owner / --require-port-free
    empty_data_dir = tmp_path / "other_data_dir"
    empty_data_dir.mkdir()
    killed_signals: list[tuple[int, int]] = []
    alive = {43210: True}

    monkeypatch.setattr(runtime, "is_launchd_registered", lambda *_a, **_k: False)
    monkeypatch.setattr(runtime, "find_current_user_port_owners", lambda port=8420: [43210] if alive[43210] else [])
    monkeypatch.setattr(runtime, "get_process_uid", lambda pid: os.getuid())
    monkeypatch.setattr(runtime, "get_process_cmdline", lambda pid: "/usr/local/bin/contextcore serve --port 18420")
    monkeypatch.setattr(runtime, "is_port_bound_by_pid", lambda pid, port: alive.get(pid, False))
    monkeypatch.setattr(runtime, "_pid_definitively_absent", lambda pid: not alive.get(pid, False))

    def _fake_kill(pid: int, sig: int):
        killed_signals.append((pid, sig))
        if sig == signal.SIGTERM:
            alive[pid] = False

    monkeypatch.setattr(os, "kill", _fake_kill)
    stop_code = cli.main(["stop", "--data-dir", str(empty_data_dir), "--port", "18420", "--stop-port-owner"])
    assert stop_code == 0
    assert (43210, signal.SIGTERM) in killed_signals

    # --require-port-free returns non-zero when port is still occupied
    monkeypatch.setattr(runtime, "is_port_occupied", lambda port=8420, host="127.0.0.1": True)
    occupied_code = cli.main(["stop", "--data-dir", str(empty_data_dir), "--port", "18420", "--require-port-free"])
    assert occupied_code == 1

    # 4. sync_local_environment: symlinked MCP resilience, existing entity preservation,
    #    multi-file skill sync, cc_app_to_host mapping, export sanitization, sync_to_hosts conflicts
    home_dir = tmp_path / "mgr_home"
    home_dir.mkdir()
    vault = _InMemoryVault()
    set_secret_store(vault)
    try:
        mgr = ManagementService(tmp_path / "mgr.db", home_dir, vault)

        # Pre-create user-customized provider & MCP asset with cwd and {{credential:...}}
        cred_ref = vault.put("secret-mcp-token")
        existing_prov = mgr.save(
            "provider",
            {
                "name": "DeepSeek Custom",
                "protocol": "openai",
                "base_url": "https://api.deepseek.com/v1",
                "model_map": {"default": "user-picked-model"},
                "supported_endpoints": ["chat/completions"],
                "agent_ids": ["codex"],
            },
        )
        existing_mcp = mgr.save(
            "asset",
            {
                "kind": "mcp",
                "name": "custom-mcp",
                "agent_ids": ["codex"],
                "config": {
                    "transport": "stdio",
                    "command": "npx",
                    "args": [],
                    "cwd": "/opt/custom-mcp",
                    "env": {"API_TOKEN": f"{{{{credential:{cred_ref}}}}}"},
                },
            },
        )

        # Create ~/.cc-switch/cc-switch.db with agy, grokbuild, openclaw + DeepSeek Custom
        cc_dir = home_dir / ".cc-switch"
        cc_dir.mkdir()
        with sqlite3.connect(cc_dir / "cc-switch.db") as cc_conn:
            cc_conn.executescript(
                """
                CREATE TABLE providers (
                    id TEXT, app_type TEXT, name TEXT, settings_config TEXT, is_current INTEGER, sort_index INTEGER, meta TEXT
                );
                CREATE TABLE prompts (
                    id TEXT, app_type TEXT, name TEXT, content TEXT, enabled INTEGER
                );
                """
            )
            cc_conn.execute(
                "INSERT INTO providers VALUES (?, ?, ?, ?, 1, 1, '{}')",
                (
                    "p1",
                    "claude",
                    "DeepSeek Custom",
                    json.dumps({"env": {"OPENAI_BASE_URL": "https://api.deepseek.com/v1", "OPENAI_MODEL": "overwritten-model"}}),
                ),
            )
            cc_conn.execute(
                "INSERT INTO prompts VALUES ('pr-agy', 'agy', 'AGY Prompt', 'Use strict types', 1)"
            )
            cc_conn.execute(
                "INSERT INTO prompts VALUES ('pr-grok', 'grokbuild', 'Grok Prompt', 'Check edge cases', 1)"
            )
            cc_conn.execute(
                "INSERT INTO prompts VALUES ('pr-claw', 'openclaw', 'OpenClaw Prompt', 'Verify configs', 1)"
            )

        # Symlinked host MCP file (~/.claude.json -> outside target) must NOT abort sync-local with 409/422
        outside_mcp = tmp_path / "outside_claude.json"
        outside_mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
        (home_dir / ".claude.json").symlink_to(outside_mcp)

        # Valid Cursor MCP config supplying args for custom-mcp
        (home_dir / ".cursor").mkdir()
        (home_dir / ".cursor" / "mcp.json").write_text(
            json.dumps({"mcpServers": {"custom-mcp": {"command": "npx", "args": ["-y", "@custom/mcp"]}}}),
            encoding="utf-8",
        )

        # Multi-file local skill with scripts/ and references/
        s_dir = home_dir / ".codex" / "skills" / "multi-file-skill"
        (s_dir / "scripts").mkdir(parents=True)
        (s_dir / "references").mkdir(parents=True)
        (s_dir / "SKILL.md").write_text("# Multi File Skill\n", encoding="utf-8")
        (s_dir / "scripts" / "run_check.py").write_text("print('ok')\n", encoding="utf-8")
        (s_dir / "references" / "guide.md").write_text("Authorization: Bearer sk-1234567890abcdef\n", encoding="utf-8")

        sync_out = mgr.sync_local_environment()
        # Symlinked ~/.claude.json recorded as non-fatal host-mcp failure rather than raising ConfigurationError
        assert sync_out["ok"] is False
        assert any(f["stage"] == "host-mcp" and f["name"] == "claude-code" for f in sync_out["failures"])

        # Pre-existing provider's model_map, supported_endpoints, agent_ids were preserved!
        after_prov = mgr.get("provider", existing_prov["id"])
        assert after_prov["model_map"] == {"default": "user-picked-model"}
        assert after_prov["supported_endpoints"] == ["chat/completions"]
        assert after_prov["agent_ids"] == ["codex"]

        # Pre-existing MCP asset preserved agent_ids, cwd, AND {{credential:...}} env while filling missing args!
        after_mcp = mgr.get("asset", existing_mcp["id"])
        assert after_mcp["agent_ids"] == ["codex"]
        assert after_mcp["config"]["cwd"] == "/opt/custom-mcp"
        assert after_mcp["config"]["args"] == ["-y", "@custom/mcp"]
        assert after_mcp["config"]["env"]["API_TOKEN"] == f"{{{{credential:{cred_ref}}}}}"

        # cc_app_to_host mappings verified
        prompts_by_name = {a["name"]: a for a in mgr.list("asset") if a["kind"] == "prompt"}
        assert prompts_by_name["AGY Prompt"]["agent_ids"] == ["antigravity-cli"]
        assert prompts_by_name["Grok Prompt"]["agent_ids"] == ["grok-build"]
        assert prompts_by_name["OpenClaw Prompt"]["agent_ids"] == ["openclaw"]

        # Multi-file skill collected sibling files with secret redaction
        skills_by_name = {a["name"]: a for a in mgr.list("asset") if a["kind"] == "skill"}
        mf_files = skills_by_name["multi-file-skill"]["files"]
        assert set(mf_files.keys()) == {"SKILL.md", "scripts/run_check.py", "references/guide.md"}
        assert "sk-1234567890abcdef" not in mf_files["references/guide.md"]
        assert "[REDACTED]" in mf_files["references/guide.md"]

        # export() strips cc_switch_current_hosts and normalizes source.type == "cc-switch" -> "local-sync"
        exported = mgr.export()
        exported_json = json.dumps(exported)
        assert "cc_switch_current_hosts" not in exported_json
        assert '"cc-switch"' not in exported_json
        exported_agy = next(o["data"] for o in exported["objects"] if o["data"].get("name") == "AGY Prompt")
        assert exported_agy["source"] == {"type": "local-sync"}

        # import_objects accepts "local-sync" and "compatible" in addition to "cc-switch"
        mgr2 = ManagementService(tmp_path / "mgr2.db", tmp_path / "mgr2_home", vault)
        imp_res = mgr2.import_objects(
            {
                "format": "local-sync",
                "data": {"prompts": [{"name": "Synced Prompt", "content": "Hello"}]},
            }
        )
        assert len(imp_res["imported"]) == 1
        assert imp_res["imported"][0]["source"] == {"type": "local-sync"}

        # sync_to_hosts reports conflict_preview separately instead of claiming status == "applied"
        conflict_mcp = mgr2.save(
            "asset",
            {
                "kind": "mcp",
                "name": "collide-mcp",
                "agent_ids": ["cursor"],
                "config": {"transport": "stdio", "command": "npx", "args": ["-y", "pkg"]},
            },
        )
        # Pre-populate unmanaged collide-mcp in mgr2's ~/.cursor/mcp.json so plan/apply conflicts, or mutate file between plan & apply
        orig_apply = mgr2.apply

        def _conflict_apply(*_args, **_kwargs):
            raise ConfigurationError("External modification detected", 409)

        monkeypatch.setattr(mgr2, "apply", _conflict_apply)
        sync_conflict = mgr2.sync_to_hosts("asset", conflict_mcp["id"], ["cursor"], operation="install", auto_apply=True)
        assert sync_conflict["ok"] is False
        assert sync_conflict["status"] == "conflict_preview"
        assert sync_conflict["conflicts"] == ["cursor"]
        assert sync_conflict["planned"] == []
        monkeypatch.setattr(mgr2, "apply", orig_apply)
    finally:
        set_secret_store(None)

    # 5. Docstrings in migration_service, management_service, management_experience have no "CC Switch"
    for mod_or_fn in (migration_service, management_service.ManagementService.sync_local_environment, management_experience.check_env_conflicts):
        doc = inspect.getdoc(mod_or_fn) or ""
        assert "CC Switch" not in doc
