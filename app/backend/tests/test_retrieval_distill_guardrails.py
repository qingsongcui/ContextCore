"""Comprehensive tests for Wave 2B Backend Retrieval, Distillation & Handoff Guardrails:
(a) Non-suppression of protected rules (`decision`, `preference`, `pinned`) across sessions
    when `context_epoch="default"` or empty after prior `record_injection` / `ack_context`.
(b) `bootstrap_working_memory` strict project isolation, bounded minimal layered output,
    multi-task `task_selection_required`, `discussion_only` -> `must_not_execute=True`,
    `provided` telemetry, zero `read_count`/`provided_count`/`cited_count` ranking bias,
    zero time-decay on protected rules, and `outdated` + tombstone exclusion.
(c) Hook (`POST /api/connections/{agent}/hook`) & distillation (`extract_implicit_memories_from_messages`,
    `auto_summarize_memory`, `apply_distilled_memories`) blocking noisy progress dumps,
    AI proposals, guesses, raw tool tracebacks, and tombstoned memories, routing contradictions
    to `conflicts`, and routing `"这次先别改界面"` to `working_tasks.temp_constraints`.
(d) Cross-agent handoff & relay (`OMP -> Codex` User Section 8 scenario) integrating `working_tasks`
    (deduplicated against `legacy_content_id`, `completed` in `do_not_redo`, `paused`/`cancelled`
    not triggering `ambiguous_task`) and preserving `execution_mode='discussion_only'`
    (`must_not_execute=True` + `"尚未授权改代码"`).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import create_schema, db, seed_data
from app.main import app as fastapi_app
from app.services import (
    conflict_service,
    content_service,
    conversation_service,
    distillation_service,
    handoff_service,
    project_service,
    retrieval_service,
    telemetry_service,
    working_memory_service,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    db_path = tmp_path / "wave2b_guardrails.sqlite"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_path))
    create_schema(db_path)
    seed_data()

    repo_a = tmp_path / "repo-alpha"
    repo_a.mkdir()
    _git(repo_a, "init", "-b", "feat/payment-v2")
    (repo_a / "README.md").write_text("# Alpha\n", encoding="utf-8")
    _git(repo_a, "add", "README.md")
    _git(repo_a, "commit", "-m", "init alpha")

    repo_b = tmp_path / "repo-beta"
    repo_b.mkdir()
    _git(repo_b, "init", "-b", "feat/beta-ui")
    (repo_b / "README.md").write_text("# Beta\n", encoding="utf-8")
    _git(repo_b, "add", "README.md")
    _git(repo_b, "commit", "-m", "init beta")

    proj_a = project_service.create_project(
        name="Alpha Payment Service",
        description="Core payment backend",
        directories=[str(repo_a)],
    )
    proj_b = project_service.create_project(
        name="Beta Dashboard",
        description="Sibling frontend app",
        directories=[str(repo_b)],
    )
    return {
        "db_path": db_path,
        "repo_a": repo_a,
        "repo_b": repo_b,
        "proj_a": proj_a,
        "proj_b": proj_b,
    }


# ============================================================================
# (a) Non-suppression of protected rules across sessions (`context_epoch`)
# ============================================================================


def test_protected_rules_never_permanently_suppressed_across_default_epoch_sessions(isolated_env):
    proj_id = isolated_env["proj_a"]["id"]

    dec = content_service.create_content(
        project_id=proj_id,
        category="decision",
        title="Idempotency key enforcement",
        content="All payment mutation endpoints MUST require X-Idempotency-Key.",
        origin_authority="user_confirmed",
    )
    pref = content_service.create_content(
        project_id=proj_id,
        category="preference",
        title="Package manager rule",
        content="Always use pnpm for workspace scripts; never run npm install.",
        origin_authority="user_confirmed",
    )
    pinned = content_service.create_content(
        project_id=proj_id,
        category="knowledge",
        title="Critical gateway invariant",
        content="Stripe webhook signature verification uses raw request body bytes.",
        tags=["pinned"],
        origin_authority="user_confirmed",
    )
    ordinary = content_service.create_content(
        project_id=proj_id,
        category="knowledge",
        title="Payment retry backoff note",
        content="Payment webhook retry uses exponential backoff jitter.",
        origin_authority="agent_observed",
    )

    # Session 1 calls assemble_context with default context_epoch and acknowledges injection
    pack_s1 = retrieval_service.assemble_context(
        query="payment webhook idempotency retry",
        project_id=proj_id,
        budget_tokens=3000,
        context_epoch="default",
    )
    s1_ids = {entry["id"] for entry in pack_s1["entries"]}
    assert {dec["id"], pref["id"], pinned["id"], ordinary["id"]}.issubset(s1_ids)

    ack_res = retrieval_service.record_injection(pack_s1["id"])
    assert ack_res["entries"] >= 4

    # Session 2 (new session, default or empty context_epoch):
    # Protected rules (`decision`, `preference`, `pinned`) MUST NOT be suppressed!
    for epoch_val in ("default", ""):
        pack_s2 = retrieval_service.assemble_context(
            query="payment webhook idempotency retry",
            project_id=proj_id,
            budget_tokens=3000,
            context_epoch=epoch_val,
        )
        s2_ids = {entry["id"] for entry in pack_s2["entries"]}
        assert dec["id"] in s2_ids
        assert pref["id"] in s2_ids
        assert pinned["id"] in s2_ids
        # Ordinary knowledge still deduplicates in default epoch
        assert ordinary["id"] not in s2_ids

    # Explicit per-session context_epoch deduplicates within THAT session only,
    # while a subsequent explicit session gets all protected rules fresh again.
    pack_explicit_1 = retrieval_service.assemble_context(
        query="payment webhook idempotency",
        project_id=proj_id,
        budget_tokens=3000,
        context_epoch="sess-2026-alpha",
    )
    assert {dec["id"], pref["id"], pinned["id"]}.issubset({e["id"] for e in pack_explicit_1["entries"]})
    retrieval_service.record_injection(pack_explicit_1["id"])

    pack_same_session = retrieval_service.assemble_context(
        query="payment webhook idempotency",
        project_id=proj_id,
        budget_tokens=3000,
        context_epoch="sess-2026-alpha",
    )
    same_sess_ids = {e["id"] for e in pack_same_session["entries"]}
    assert pinned["id"] not in same_sess_ids
    assert dec["id"] not in same_sess_ids
    assert pref["id"] not in same_sess_ids

    pack_next_session = retrieval_service.assemble_context(
        query="payment webhook idempotency",
        project_id=proj_id,
        budget_tokens=3000,
        context_epoch="sess-2026-beta",
    )
    next_sess_ids = {e["id"] for e in pack_next_session["entries"]}
    assert {dec["id"], pref["id"], pinned["id"]}.issubset(next_sess_ids)


# ============================================================================
# (b) `bootstrap_working_memory`, project isolation, telemetry, 0 count bias
# ============================================================================


def test_bootstrap_working_memory_isolation_layers_tasks_conflicts_and_telemetry(isolated_env):
    proj_a = isolated_env["proj_a"]
    proj_b = isolated_env["proj_b"]
    repo_a = isolated_env["repo_a"]

    # Global constraint + project A constraint + sibling project B constraint (must NOT leak)
    global_pref = content_service.create_content(
        project_id=None,
        category="preference",
        title="Global Chinese response preference",
        content="Use concise Chinese explanations in user-facing summaries.",
        origin_authority="user_confirmed",
    )
    proj_a_dec = content_service.create_content(
        project_id=proj_a["id"],
        category="decision",
        title="Decimal money representation",
        content="Store all payment amounts as integer minor units (cents), never floats.",
        applicability={"module": "billing", "version": ">=2.0"},
        origin_authority="user_confirmed",
    )
    proj_b_secret = content_service.create_content(
        project_id=proj_b["id"],
        category="decision",
        title="Beta secret theme token",
        content="LEAK_CANARY_BETA_ONLY CSS variables in dashboard.",
        origin_authority="user_confirmed",
    )

    # Project A verified lesson with applicability + Project B lesson
    proj_a_lesson = content_service.create_content(
        project_id=proj_a["id"],
        category="lesson",
        title="SQLite WAL busy_timeout pitfall",
        content="Set PRAGMA busy_timeout=5000 when running concurrent payment workers.",
        applicability={"env": "sqlite-wal", "version": "3.45+", "when": "local multi-worker dev"},
        origin_authority="verified_execution",
    )
    content_service.create_content(
        project_id=proj_b["id"],
        category="lesson",
        title="SQLite WAL beta lesson",
        content="LEAK_CANARY_BETA_LESSON never appear in Alpha.",
        applicability={"env": "beta"},
        origin_authority="verified_execution",
    )

    # Add a noisy raw conversation transcript in Project A -> must NEVER dump into bootstrap
    conversation_service.upsert_conversation(
        agent="omp",
        project_id=proj_a["id"],
        cwd=str(repo_a),
        title="Long noisy debugging chat",
        messages=[
            {"role": "user", "content": "RAW_TRANSCRIPT_SECRET_MARKER_9912 check sqlite wal"},
            {"role": "assistant", "content": "RAW_TRANSCRIPT_ASSISTANT_DUMP_8841 checking logs..."},
        ],
    )

    # Create pending conflict in Project A and another in Project B
    conflict_a = conflict_service.create_conflict(
        project_id=proj_a["id"],
        existing_id=proj_a_dec["id"],
        proposed={
            "title": "Decimal money representation",
            "content": "Use Decimal(18, 4) strings instead of integer cents.",
            "category": "decision",
        },
        reason="Conflicting currency precision proposal awaiting user decision",
    )
    conflict_service.create_conflict(
        project_id=proj_b["id"],
        existing_id=proj_b_secret["id"],
        proposed={"title": "Beta conflict", "content": "LEAK_CANARY_CONFLICT", "category": "decision"},
        reason="LEAK_CANARY_CONFLICT_REASON",
    )

    # Working tasks in Project A: paused, completed, cancelled, plus 1 discussion_only active task
    wt_done = working_memory_service.upsert_working_task(
        project_id=proj_a["id"],
        title="Legacy webhook migration",
        goal="Migrate old v1 endpoint",
        status="completed",
    )
    wt_paused = working_memory_service.upsert_working_task(
        project_id=proj_a["id"],
        title="Paused metrics export",
        goal="Prometheus exporter",
        status="paused",
    )
    wt_cancelled = working_memory_service.upsert_working_task(
        project_id=proj_a["id"],
        title="Cancelled CSV report",
        goal="Deprecated CSV report",
        status="cancelled",
    )
    wt_active_1 = working_memory_service.upsert_working_task(
        project_id=proj_a["id"],
        title="Payment refund state machine",
        goal="Design idempotent partial refund transitions",
        status="in_progress",
        execution_mode="discussion_only",
        cwd=str(repo_a),
        git_branch="feat/payment-v2",
        temp_constraints=["这次先别改数据库表结构"],
        agent="omp",
    )
    # Sibling project active task
    working_memory_service.upsert_working_task(
        project_id=proj_b["id"],
        title="LEAK_CANARY_BETA_TASK",
        status="in_progress",
    )

    subdir = repo_a / "src" / "payments"
    subdir.mkdir(parents=True)

    boot = retrieval_service.bootstrap_working_memory(
        cwd=str(subdir),
        query="SQLite WAL payment amounts",
        consumer_id="codex",
        max_tokens=4000,
    )
    assert boot["ok"] is True
    assert boot["project_id"] == proj_a["id"]
    assert boot["estimated_tokens"] <= 4000

    # 1. Constraints isolated to Project A + Global, with applicability surfaced
    constraint_ids = {c["id"] for c in boot["applicable_constraints"]}
    assert global_pref["id"] in constraint_ids
    assert proj_a_dec["id"] in constraint_ids
    assert proj_b_secret["id"] not in constraint_ids
    dec_hit = next(c for c in boot["applicable_constraints"] if c["id"] == proj_a_dec["id"])
    assert dec_hit["applicability"] == {"module": "billing", "version": ">=2.0"}
    assert "适用条件" in dec_hit["applicability_warning"]

    # 2. Active tasks exclude paused/completed/cancelled; sole active task auto-selected
    active_task_ids = {t["id"] for t in boot["active_tasks"]}
    assert active_task_ids == {wt_active_1["id"]}
    assert wt_done["id"] not in active_task_ids
    assert wt_paused["id"] not in active_task_ids
    assert wt_cancelled["id"] not in active_task_ids
    assert boot["task_selection_required"] is False
    assert boot["selected_task"]["id"] == wt_active_1["id"]

    # 3. discussion_only propagates must_not_execute=True
    assert boot["must_not_execute"] is True

    # 4. Relevant lessons include applicability + applicability_warning, zero Project B leak
    lesson_ids = {l["id"] for l in boot["relevant_lessons"]}
    assert proj_a_lesson["id"] in lesson_ids
    lesson_hit = next(l for l in boot["relevant_lessons"] if l["id"] == proj_a_lesson["id"])
    assert lesson_hit["applicability"]["env"] == "sqlite-wal"
    assert "sqlite-wal" in lesson_hit["applicability_warning"]

    # 5. Pending conflicts surfaced for Project A only
    conflict_ids = {c["id"] for c in boot["pending_conflicts"]}
    assert conflict_ids == {conflict_a["id"]}

    # 6. Concise feedback_summary & zero raw chat dumps
    assert "已加载" in boot["feedback_summary"]
    assert "Payment refund state machine" in boot["feedback_summary"]
    serialized = str(boot)
    assert "LEAK_CANARY_" not in serialized
    assert "RAW_TRANSCRIPT_SECRET_MARKER_9912" not in serialized
    assert "RAW_TRANSCRIPT_ASSISTANT_DUMP_8841" not in serialized

    # 7. Second active task triggers task_selection_required=True when unambiguous match absent
    wt_active_2 = working_memory_service.upsert_working_task(
        project_id=proj_a["id"],
        title="Webhook signature rotation",
        goal="Support dual signing secrets",
        status="active",
        execution_mode="approved",
        agent="claude-code",
    )
    boot_multi = retrieval_service.bootstrap_working_memory(
        project_id=proj_a["id"],
        query="general payment overview",
        consumer_id="codex",
    )
    assert {t["id"] for t in boot_multi["active_tasks"]} == {wt_active_1["id"], wt_active_2["id"]}
    assert boot_multi["task_selection_required"] is True
    assert boot_multi["selected_task"] is None

    # Passing explicit task_id resolves ambiguity cleanly
    boot_explicit = retrieval_service.bootstrap_working_memory(
        project_id=proj_a["id"],
        task_id=wt_active_2["id"],
        consumer_id="codex",
    )
    assert boot_explicit["task_selection_required"] is False
    assert boot_explicit["selected_task"]["id"] == wt_active_2["id"]
    assert boot_explicit["must_not_execute"] is False

    # 8. assemble_context also includes working_tasks, discussion_only must_not_execute, and telemetry
    working_memory_service.patch_working_task(wt_active_2["id"], status="paused")
    pack = retrieval_service.assemble_context(
        query="SQLite WAL busy_timeout",
        project_id=proj_a["id"],
        consumer_id="claude-code",
    )
    assert [t["id"] for t in pack["working_tasks"]] == [wt_active_1["id"]]
    assert pack["must_not_execute"] is True

    dec_usage = content_service.get_content(proj_a_dec["id"])["usage_summary"]
    lesson_usage = content_service.get_content(proj_a_lesson["id"])["usage_summary"]
    with db() as conn:
        surfaces = {
            r["surface"]
            for r in conn.execute(
                "SELECT DISTINCT surface FROM memory_usage_events WHERE content_id = ? AND event_type = 'provided'",
                (proj_a_dec["id"],),
            ).fetchall()
        }
    assert dec_usage["provided_count"] >= 2
    assert lesson_usage["provided_count"] >= 2
    assert {"bootstrap_working_memory", "assemble_context"}.issubset(surfaces)


def test_zero_popularity_bias_zero_constraint_decay_and_outdated_tombstone_hygiene(isolated_env):
    proj_id = isolated_env["proj_a"]["id"]

    exact_lesson = content_service.create_content(
        project_id=proj_id,
        category="lesson",
        title="Redis Lua script atomicity",
        content="Redis Lua script EVALSHA guarantees atomic check-and-decrement on inventory keys.",
        applicability={"component": "inventory-redis"},
        origin_authority="verified_execution",
    )
    popular_weak = content_service.create_content(
        project_id=proj_id,
        category="lesson",
        title="General cache note",
        content="Redis connection pool settings exist in config.",
        applicability={"component": "cache"},
        origin_authority="verified_execution",
    )
    old_decision = content_service.create_content(
        project_id=proj_id,
        category="decision",
        title="Strict UTC timestamps",
        content="All database timestamps MUST use ISO-8601 UTC format.",
        origin_authority="user_confirmed",
    )
    new_decision = content_service.create_content(
        project_id=proj_id,
        category="decision",
        title="Strict UTC timestamps",
        content="All database timestamps MUST use ISO-8601 UTC format in API payloads.",
        origin_authority="user_confirmed",
    )

    hits_before = retrieval_service.search(query="Redis Lua script atomic EVALSHA", project_id=proj_id)["items"]
    score_weak_before = next(h["score"] for h in hits_before if h["id"] == popular_weak["id"])
    score_dec_before = next(
        h["score"]
        for h in retrieval_service.search(
            query="ISO-8601 UTC database timestamps", project_id=proj_id
        )["items"]
        if h["id"] == old_decision["id"]
    )

    # Inflate provided_count and cited_count heavily and age old_decision to 2019
    for idx in range(25):
        telemetry_service.record_provided(
            [popular_weak["id"]],
            agent="codex",
            project_id=proj_id,
            surface="bootstrap_working_memory",
        )
        telemetry_service.record_cited(
            [popular_weak["id"]],
            agent="codex",
            project_id=proj_id,
            note=f"cited-{idx}",
        )
    weak_usage = content_service.get_content(popular_weak["id"])["usage_summary"]
    assert weak_usage["provided_count"] >= 25
    assert weak_usage["cited_count"] >= 25

    with db() as conn:
        conn.execute(
            "UPDATE contents SET created_at = '2019-01-01T00:00:00Z', updated_at = '2019-01-01T00:00:00Z' WHERE id = ?",
            (old_decision["id"],),
        )
    retrieval_service.refresh_index()

    hits_after = retrieval_service.search(query="Redis Lua script atomic EVALSHA", project_id=proj_id)["items"]
    score_weak_after = next(h["score"] for h in hits_after if h["id"] == popular_weak["id"])
    score_exact_after = next(h["score"] for h in hits_after if h["id"] == exact_lesson["id"])

    # Zero popularity bias: heavy provisions/citations add 0.0 score boost!
    assert score_weak_after == score_weak_before
    assert score_exact_after > score_weak_after
    assert hits_after[0]["id"] == exact_lesson["id"]

    # Zero time-decay on protected decisions: aging from 2026 to 2019 changes score by 0.0!
    score_dec_after = next(
        h["score"]
        for h in retrieval_service.search(
            query="ISO-8601 UTC database timestamps", project_id=proj_id
        )["items"]
        if h["id"] == old_decision["id"]
    )
    assert score_dec_after == score_dec_before

    # Mark exact_lesson outdated -> immediately excluded from search, assemble_context, bootstrap
    content_service.mark_outdated(
        exact_lesson["id"],
        reason="Replaced by Postgres advisory lock",
        actor="codex",
    )
    # Delete new_decision -> canonical tombstone recorded
    content_service.delete_content(new_decision["id"])

    search_ids = {
        h["id"]
        for h in retrieval_service.search(query="Redis Lua UTC timestamps", project_id=proj_id)["items"]
    }
    assert exact_lesson["id"] not in search_ids
    assert new_decision["id"] not in search_ids

    pack_ids = {
        e["id"]
        for e in retrieval_service.assemble_context(
            query="Redis Lua UTC timestamps", project_id=proj_id
        )["entries"]
    }
    assert exact_lesson["id"] not in pack_ids
    assert new_decision["id"] not in pack_ids

    boot = retrieval_service.bootstrap_working_memory(
        project_id=proj_id, query="Redis Lua UTC timestamps"
    )
    boot_ids = {c["id"] for c in boot["applicable_constraints"]} | {
        l["id"] for l in boot["relevant_lessons"]
    }
    assert exact_lesson["id"] not in boot_ids
    assert new_decision["id"] not in boot_ids


# ============================================================================
# (c) Hook & Distillation Guardrails (No Auto-Pollution, Temp Routing, Tombstones)
# ============================================================================


def test_hook_and_distillation_block_noise_route_temp_constraints_and_honor_tombstones(isolated_env):
    proj_id = isolated_env["proj_a"]["id"]
    repo_a = isolated_env["repo_a"]

    raw_token = "hook-test-secret-omp"
    with db() as conn:
        conn.execute(
            "UPDATE connections SET enabled = 1, paused = 0, token_hash = ? WHERE id = 'omp'",
            (hashlib.sha256(raw_token.encode()).hexdigest(),),
        )

    client = TestClient(fastapi_app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    auth_headers = {"Authorization": f"Bearer {raw_token}"}

    # 1. Hook event with noisy assistant progress ("正在推进... 下一步需要..."), AI suggestion,
    # guess, and raw traceback MUST NOT pollute long-term `contents`!
    noisy_resp = client.post(
        "/api/connections/omp/hook",
        headers=auth_headers,
        json={
            "event_type": "turn_end",
            "session_id": "omp-sess-noisy-1",
            "title": "重构支付网关会话",
            "cwd": str(repo_a),
            "messages": [
                {"role": "user", "content": "帮我看一下支付网关目前的重构情况"},
                {
                    "role": "assistant",
                    "content": (
                        "正在推进支付网关鉴权层改造，下一步需要补齐集成测试。"
                        "我建议顺便把所有缓存切换到 Redis Cluster。"
                        "刚才报错可能是因为连接池超时。"
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        "Traceback (most recent call last):\n"
                        "  File \"/app/pay.py\", line 42, in charge\n"
                        "    raise TimeoutError('pool exhausted')\n"
                        "TimeoutError: pool exhausted"
                    ),
                },
            ],
        },
    )
    assert noisy_resp.status_code == 200, noisy_resp.text
    assert content_service.list_content(project_id=proj_id, status="active") == []
    assert all(
        "任务进展与阶段背景" not in item["title"]
        for item in content_service.list_content(project_id=proj_id, status="all")
    )

    # 2. Hook event with temporary constraint ("这次先别改界面") routes ONLY to working_tasks.temp_constraints
    temp_resp = client.post(
        "/api/connections/omp/hook",
        headers=auth_headers,
        json={
            "event_type": "turn_end",
            "session_id": "omp-sess-temp-2",
            "title": "退款接口联调",
            "cwd": str(repo_a),
            "messages": [
                {"role": "user", "content": "这次先别改界面，只验证后端退款状态机接口。"},
                {"role": "assistant", "content": "明白，本次仅调整后端状态机校验。"},
            ],
        },
    )
    assert temp_resp.status_code == 200, temp_resp.text
    assert content_service.list_content(project_id=proj_id, status="active") == []

    tasks = working_memory_service.list_working_tasks(proj_id, active_only=True)
    assert len(tasks) == 1
    assert any("先别改界面" in c for c in tasks[0]["temp_constraints"])

    # 3. Direct distillation / auto_summarize_memory rejects AI proposals, guesses, raw logs,
    # routes temp constraints to working_tasks, enforces canonical tombstones, and opens conflicts!
    active_pref = content_service.create_content(
        project_id=proj_id,
        category="preference",
        title="包管理器规范",
        content="统一使用 pnpm 管理依赖，禁止使用 npm。",
        origin_authority="user_confirmed",
    )
    deleted_rule = content_service.create_content(
        project_id=proj_id,
        category="decision",
        title="废弃网关协议",
        content="内部结算服务全部走 XML-RPC 协议。",
        origin_authority="user_confirmed",
    )
    content_service.delete_content(deleted_rule["id"])

    conv = conversation_service.upsert_conversation(
        agent="codex",
        project_id=proj_id,
        cwd=str(repo_a),
        title="Distillation governance check",
        messages=[
            {"role": "user", "content": "本次不要改动账单导出脚本。另外包管理器规范改用 npm 管理依赖。"},
            {"role": "assistant", "content": "我建议把日志改成 JSON 格式；可能是因为磁盘慢导致的。"},
        ],
    )

    sum_res = distillation_service.auto_summarize_memory(
        project_id=proj_id,
        conversation_id=conv["id"],
        actor="codex",
        auto_extract_from_messages=False,
        memories=[
            # (i) Unconfirmed AI proposal -> rejected
            {
                "category": "knowledge",
                "title": "日志格式改造建议",
                "content": "我建议把所有服务日志改成 JSON 格式输出。",
            },
            # (ii) Unverified guess -> rejected
            {
                "category": "lesson",
                "title": "磁盘超时猜测",
                "content": "可能是因为容器磁盘 IO 抖动导致的偶发超时，猜测需要调大阈值。",
                "applicability": {"env": "prod"},
            },
            # (iii) Raw traceback -> rejected
            {
                "category": "lesson",
                "title": "Crash dump",
                "content": "Traceback (most recent call last):\n  File \"main.py\", line 9, in <module>\nKeyError: 'x'",
                "applicability": {"env": "dev"},
            },
            # (iv) Tombstoned memory (even across category with extra whitespace!) -> rejected
            {
                "category": "preference",
                "title": "废弃网关协议",
                "content": "内部结算服务全部走   XML-RPC   协议。",
                "origin_authority": "user_confirmed",
            },
            # (v) Temporary constraint -> routed to working_tasks.temp_constraints
            {
                "category": "preference",
                "title": "账单脚本限制",
                "content": "本次不要改动账单导出脚本",
                "quote": "本次不要改动账单导出脚本",
                "source_message_index": 0,
                "user_message_index": 0,
            },
            # (vi) Contradiction against active preference -> routed to conflicts, active rule preserved
            {
                "category": "preference",
                "title": "包管理器安装规范",
                "content": "改用 npm 管理依赖",
                "quote": "包管理器规范改用 npm 管理依赖",
                "source_message_index": 0,
                "user_message_index": 0,
            },
        ],
    )

    assert sum_res["saved_count"] == 0
    # Active preference stays intact, zero silent overwrite, zero tombstoned resurrection
    active_items = content_service.list_content(project_id=proj_id, status="active")
    assert [item["id"] for item in active_items] == [active_pref["id"]]
    assert "pnpm" in active_items[0]["content"]

    # Temporary constraint added to active working task
    refreshed_tasks = working_memory_service.list_working_tasks(proj_id, active_only=True)
    assert any("不要改动账单导出脚本" in c for c in refreshed_tasks[0]["temp_constraints"])

    # Contradiction recorded in pending conflicts
    pending_conflicts = conflict_service.list_conflicts(project_id=proj_id, status="pending")
    assert len(pending_conflicts) == 1
    assert pending_conflicts[0]["existing_id"] == active_pref["id"]


# ============================================================================
# (d) Cross-Agent Handoff & Relay (`OMP -> Codex` `discussion_only` Scenario)
# ============================================================================


def test_cross_agent_handoff_omp_to_codex_preserves_discussion_only_and_working_task_lifecycle(isolated_env):
    proj_id = isolated_env["proj_a"]["id"]
    repo_a = isolated_env["repo_a"]

    # 1. Legacy task synced with working_tasks must NOT double-count as 2 open tasks
    legacy_done = content_service.create_content(
        project_id=proj_id,
        category="task",
        title="Initialize payment repo",
        content="Repo initialized",
        tags=["status:completed"],
        agent="omp",
    )
    # Paused & cancelled working_tasks must exit active todo without causing ambiguous_task!
    working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="Paused settlement batch job",
        status="paused",
        agent="omp",
    )
    working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="Cancelled legacy SOAP adapter",
        status="cancelled",
        agent="omp",
    )
    wt_completed = working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="Webhook signature verifier",
        goal="Verify HMAC-SHA256 signatures",
        status="completed",
        agent="omp",
    )

    # User discusses architecture plan with OMP WITHOUT authorizing code edits (`discussion_only`)
    omp_conv = conversation_service.upsert_conversation(
        agent="omp",
        project_id=proj_id,
        cwd=str(repo_a),
        title="与 OMP 讨论退款幂等状态机方案",
        messages=[
            {
                "role": "user",
                "content": "我们先讨论一下退款幂等状态机的方案，这次先别改界面，也先不要改代码，先把状态流转边界理清楚。",
            },
            {
                "role": "assistant",
                "content": "好的，我们先只讨论方案不改代码：建议分 pending_refund -> processing -> refunded 三态，并评估并发锁策略。",
            },
        ],
    )
    wt_discuss = working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="退款幂等状态机方案设计",
        goal="理清并发部分退款的状态流转与幂等键边界",
        current_state="已在 OMP 梳理三态模型草案，待权衡悲观锁 vs 唯一索引",
        temp_constraints=["这次先别改界面"],
        blockers=["锁粒度与超时回滚策略尚未敲定"],
        next_steps=["对比 SELECT FOR UPDATE 与唯一幂等键表的吞吐影响"],
        status="in_progress",
        execution_mode="discussion_only",
        cwd=str(repo_a),
        git_branch="feat/payment-v2",
        source_conversation_id=omp_conv["id"],
        agent="omp",
    )

    with db() as conn:
        open_tasks, completed_tasks = handoff_service.list_project_tasks(conn, proj_id)
    assert [t["id"] for t in open_tasks] == [wt_discuss["id"]]
    completed_ids = {t["id"] for t in completed_tasks}
    assert {legacy_done["id"], wt_completed["id"]}.issubset(completed_ids)

    # 2. User switches to Codex and says "接着刚才和 OMP 讨论的方案继续" via relay_conversation
    conv_relay = handoff_service.relay_conversation(
        conversation_id=omp_conv["id"],
        consumer="codex",
        query="接着刚才和 OMP 讨论的方案继续",
        sync_first=False,
    )
    assert conv_relay["ok"] is True
    assert conv_relay["must_not_execute"] is True
    assert conv_relay["confirmed"] is False
    assert conv_relay["execution_mode"] == "discussion_only"
    assert conv_relay["continuation"]["must_not_execute"] is True
    assert conv_relay["continuation"]["execution_mode"] == "discussion_only"
    assert conv_relay["continuation"]["status"] == "discussion_only"
    assert conv_relay["continuation"]["resume_from"] is None
    assert conv_relay["continuation"]["selected_task_id"] == wt_discuss["id"]
    assert "尚未授权改代码" in conv_relay["card"]["progress"]
    assert "尚未授权改代码" in conv_relay["card"]["decisions"]
    assert "先别改界面" in conv_relay["card"]["decisions"]
    assert "尚未授权改代码" in conv_relay["card"]["blockers"]
    assert "尚未授权改代码" in conv_relay["card"]["next_steps"]
    assert "尚未授权改代码" in conv_relay["context_text"]
    assert "discussion_only" in conv_relay["context_text"]

    # 3. Even if confirm_handoff is called explicitly on a discussion_only handoff,
    # continuation_contract MUST keep must_not_execute=True until execution_mode is approved!
    explicit_handoff = handoff_service.create_handoff(
        project_id=proj_id,
        task_id=wt_discuss["id"],
        conversation_id=omp_conv["id"],
        target_agent="codex",
        source_agent="omp",
        actor="codex",
    )
    confirmed_handoff = handoff_service.confirm_handoff(
        explicit_handoff["id"], confirmed=True, actor="codex"
    )
    contract = handoff_service.continuation_contract(confirmed_handoff, "codex")
    assert contract["must_not_execute"] is True
    assert contract["execution_mode"] == "discussion_only"
    assert contract["status"] == "discussion_only"
    assert contract["resume_from"] is None
    assert {item["id"] for item in contract["do_not_redo"]} >= {legacy_done["id"], wt_completed["id"]}

    # 4. Project-level relay (`relay_project`) also preserves discussion_only & must_not_execute=True
    proj_relay = handoff_service.relay_project(
        project_id=proj_id,
        consumer="codex",
        query="接着刚才和 OMP 讨论的方案继续",
        max_tokens=32000,
        sync_first=False,
    )
    assert proj_relay["ok"] is True, proj_relay
    assert proj_relay["must_not_execute"] is True
    assert proj_relay["execution_mode"] == "discussion_only"
    assert proj_relay["continuation"]["must_not_execute"] is True
    assert "尚未授权改代码" in proj_relay["context_text"]

    # 5. Once user explicitly authorizes execution (`execution_mode="approved"`),
    # old discussion_only handoff becomes stale (409/stale), and a fresh confirmed handoff is ready!
    working_memory_service.patch_working_task(
        wt_discuss["id"],
        execution_mode="approved",
        agent="user",
    )
    stale_contract = handoff_service.continuation_contract(confirmed_handoff, "codex")
    assert stale_contract["status"] == "stale"
    assert stale_contract["must_not_execute"] is True

    approved_handoff = handoff_service.create_handoff(
        project_id=proj_id,
        task_id=wt_discuss["id"],
        conversation_id=omp_conv["id"],
        target_agent="codex",
        source_agent="omp",
        actor="codex",
    )
    approved_confirmed = handoff_service.confirm_handoff(
        approved_handoff["id"], confirmed=True, actor="codex"
    )
    ready_contract = handoff_service.continuation_contract(approved_confirmed, "codex")
    assert ready_contract["status"] == "ready"
    assert ready_contract["execution_mode"] == "approved"
    assert ready_contract["must_not_execute"] is False
    assert ready_contract["selected_task_id"] == wt_discuss["id"]
