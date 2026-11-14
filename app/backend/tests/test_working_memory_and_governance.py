"""Comprehensive unit & service tests for Wave 2A Backend Foundation:
- SQLite `contents` rebuild migration (`status='outdated'`, governance columns, FKs, indexes, triggers)
- Pydantic models (`Content*`, `WorkingTask*`, preserved `Conversation` worktree fields)
- 3-state telemetry (`saved` -> `provided` -> `cited`)
- Admission & reconciliation guardrails (`governance_guardrails.py`, `reconcile_long_term_memory`)
- Canonical tombstones across `delete_content`, `create_content`, `merge_content`, `reconcile_long_term_memory`
- Task-bound working memory (`working_memory_service.py`) & `project_service._get_project_counts`
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.database import create_schema, db, now_iso
from app.models import (
    Content,
    ContentCorrectRequest,
    ContentCreate,
    ContentOutdatedRequest,
    ContentUpdate,
    Conversation,
    WorkingTask,
    WorkingTaskPatch,
    WorkingTaskPromoteRequest,
    WorkingTaskUpsert,
)
from app.services import (
    conflict_service,
    content_service,
    conversation_service,
    derivation_service,
    governance_guardrails,
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
def env_project(tmp_path, monkeypatch):
    db_path = tmp_path / "wm_gov.sqlite"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_path))
    create_schema(db_path)
    repo_dir = tmp_path / "sample-repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-b", "feat/working-memory")
    (repo_dir / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-m", "initial")
    proj = project_service.create_project(
        name="Memory Governance Repo",
        description="Test project",
        directories=[str(repo_dir)],
    )
    return {"db_path": db_path, "project": proj, "repo_dir": repo_dir}


# ============================================================================
# 1. Legacy SQLite Schema Rebuild Migration & Models Contract
# ============================================================================


def test_legacy_contents_table_rebuild_preserves_rows_fks_indexes_and_triggers(tmp_path, monkeypatch):
    legacy_db = tmp_path / "legacy_pre_outdated.sqlite"
    monkeypatch.setenv("CONTEXTCORE_DB", str(legacy_db))

    raw_conn = sqlite3.connect(legacy_db)
    raw_conn.row_factory = sqlite3.Row
    try:
        raw_conn.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
                archived_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE contents (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id),
                kind TEXT NOT NULL CHECK(kind IN ('memory', 'document')),
                category TEXT NOT NULL CHECK(category IN ('preference', 'background', 'decision', 'task', 'lesson', 'knowledge')),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                agent TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'historical')),
                version INTEGER NOT NULL DEFAULT 1,
                related_ids TEXT NOT NULL DEFAULT '[]',
                content_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX idx_custom_legacy_contents_title ON contents(title);
            CREATE TABLE content_versions (
                id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL,
                UNIQUE(content_id, version)
            );
            CREATE TABLE content_sources (
                id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
                source_id TEXT,
                source_kind TEXT NOT NULL CHECK(source_kind IN ('conversation', 'document')),
                source_title TEXT NOT NULL DEFAULT '',
                source_agent TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                quote TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE conflicts (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id),
                existing_id TEXT REFERENCES contents(id) ON DELETE CASCADE,
                proposed TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'resolved')),
                resolution TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            CREATE TABLE conversations (
                id TEXT PRIMARY KEY,
                external_id TEXT,
                project_id TEXT REFERENCES projects(id),
                agent TEXT NOT NULL,
                title TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                messages TEXT NOT NULL DEFAULT '[]',
                process_summary TEXT NOT NULL DEFAULT '[]',
                processing_status TEXT NOT NULL DEFAULT 'pending',
                processing_error TEXT,
                source_path TEXT,
                content_hash TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        derivation_service.ensure_schema(raw_conn)
        retrieval_service.ensure_schema(raw_conn)

        ts = "2026-11-12T00:00:00+00:00"
        raw_conn.execute(
            "INSERT INTO projects (id, name, description, status, created_at, updated_at) VALUES (?, ?, '', 'active', ?, ?)",
            ("proj_leg", "LegacyProj", ts, ts),
        )
        raw_conn.execute(
            """
            INSERT INTO contents (
                id, project_id, kind, category, title, content, tags, agent,
                status, version, related_ids, content_hash, created_at, updated_at
            ) VALUES (?, ?, 'memory', 'decision', ?, ?, '["pinned"]', 'codex', 'active', 2, '[]', 'hash123', ?, ?)
            """,
            ("cnt_leg1", "proj_leg", "Use SQLite WAL", "Always enable WAL mode.", ts, ts),
        )
        raw_conn.execute(
            "INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at) VALUES (?, ?, 1, ?, ?, 'user', ?)",
            ("ver_leg1", "cnt_leg1", "Use SQLite WAL", "v1", ts),
        )
        raw_conn.execute(
            "INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at) VALUES (?, ?, 2, ?, ?, 'codex', ?)",
            ("ver_leg2", "cnt_leg1", "Use SQLite WAL", "Always enable WAL mode.", ts),
        )
        raw_conn.execute(
            "INSERT INTO conflicts (id, project_id, existing_id, proposed, reason, status, created_at) VALUES (?, ?, ?, '{}', 'test', 'pending', ?)",
            ("conf_leg1", "proj_leg", "cnt_leg1", ts),
        )
        raw_conn.commit()
    finally:
        raw_conn.close()

    # Run create_schema twice to prove idempotency
    create_schema(legacy_db)
    create_schema(legacy_db)

    with db(legacy_db) as conn:
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_errors == []

        migrated = conn.execute("SELECT * FROM contents WHERE id = 'cnt_leg1'").fetchone()
        assert migrated is not None
        assert migrated["title"] == "Use SQLite WAL"
        assert migrated["version"] == 2
        assert migrated["applicability"] == "{}"
        assert migrated["origin_authority"] == "manual"
        assert migrated["canonical_key"] and migrated["canonical_key"].startswith("cmk_")

        # Custom index & system triggers survived rebuild
        indexes = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='contents'")
        }
        assert "idx_custom_legacy_contents_title" in indexes

        triggers = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='contents'")
        }
        assert {
            "content_identity_insert",
            "content_identity_update",
            "retrieval_content_update",
            "retrieval_content_delete",
            "derivation_contents_update",
            "derivation_contents_delete",
        }.issubset(triggers)

        # Child rows survived without FK rewrite breakage
        assert conn.execute("SELECT COUNT(*) FROM content_versions WHERE content_id='cnt_leg1'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM conflicts WHERE existing_id='cnt_leg1'").fetchone()[0] == 1

        # Invalid status still rejected by expanded CHECK constraint
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE contents SET status = 'invalid_status' WHERE id = 'cnt_leg1'")

    # 'outdated' status works end-to-end on the migrated row
    outdated = content_service.mark_outdated("cnt_leg1", reason="Superseded by v3 config", expected_version=2)
    assert outdated["status"] == "outdated"
    assert outdated["outdated_reason"] == "Superseded by v3 config"
    assert outdated["outdated_at"] is not None
    assert outdated["version"] == 3


def test_pydantic_models_and_preserved_conversation_fields():
    conv = Conversation(
        id="conv_1",
        agent="codex",
        title="Worktree session",
        cwd="/tmp/repo",
        git_branch="feat/branch",
        git_head="a" * 40,
        started_at=now_iso(),
        updated_at=now_iso(),
    )
    assert conv.cwd == "/tmp/repo"
    assert conv.git_branch == "feat/branch"
    assert conv.git_head == "a" * 40

    c_model = Content(
        id="cnt_1",
        title="Rule",
        content="Body",
        status="outdated",
        applicability={"env": "macos"},
        origin_authority="user_explicit",
        outdated_reason="Old OS",
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    assert c_model.status == "outdated"
    assert c_model.usage_summary["state"] == "saved"
    assert ContentCreate(title="T", applicability={"when": "x"}, origin_authority="user_explicit").origin_authority == "user_explicit"
    assert ContentUpdate(status="outdated", expected_version=1).status == "outdated"
    assert ContentOutdatedRequest(reason="retired", expected_version=1).reason == "retired"
    assert ContentCorrectRequest(content="New body", applicability={"env": "linux"}).content == "New body"

    wt = WorkingTask(
        id="tsk_1",
        project_id="proj_1",
        title="Implement API",
        execution_mode="discussion_only",
        must_not_execute=True,
        created_at=now_iso(),
        updated_at=now_iso(),
    )
    assert wt.must_not_execute is True
    assert WorkingTaskUpsert(title="Implement API", execution_mode="approved").execution_mode == "approved"
    assert WorkingTaskPatch(status="paused", expected_version=2).status == "paused"
    assert (
        WorkingTaskPromoteRequest(
            title="Lesson",
            content="Fix",
            evidence_quote="Fix the bug",
            applicability={"when": "always"},
        ).category
        == "lesson"
    )


# ============================================================================
# 2. Honest 3-State Telemetry (`saved` -> `provided` -> `cited`)
# ============================================================================


def test_three_state_telemetry_saved_provided_cited(env_project):
    proj_id = env_project["project"]["id"]
    mem = content_service.create_content(
        title="Strict ESM Guard",
        content="Use pathToFileURL(path.resolve(process.argv[1])).href for CLI main guard.",
        project_id=proj_id,
        category="preference",
    )
    assert mem["usage_summary"]["state"] == "saved"
    assert mem["usage_summary"]["provided_count"] == 0
    assert mem["usage_summary"]["cited_count"] == 0

    # Record provided (with session dedupe)
    telemetry_service.record_provided(
        [mem["id"]],
        agent="claude-code",
        project_id=proj_id,
        session_key="sess-001",
        surface="bootstrap_working_memory",
        dedupe_session=True,
    )
    telemetry_service.record_provided(
        [mem["id"]],
        agent="claude-code",
        project_id=proj_id,
        session_key="sess-001",
        surface="bootstrap_working_memory",
        dedupe_session=True,
    )
    after_provided = content_service.get_content(mem["id"])
    assert after_provided["usage_summary"]["state"] == "provided"
    assert after_provided["usage_summary"]["provided_count"] == 1
    assert after_provided["usage_summary"]["last_provided_agent"] == "claude-code"
    assert after_provided["usage_summary"]["cited_count"] == 0

    # Record cited
    telemetry_service.record_cited(
        mem["id"],
        agent="codex",
        project_id=proj_id,
        surface="cite_memory",
        note="Applied canonical URL main-module check in cli.mjs",
    )
    after_cited = content_service.get_content(mem["id"])
    us = after_cited["usage_summary"]
    assert us["state"] == "cited"
    assert us["provided_count"] == 1
    assert us["cited_count"] == 1
    assert us["last_cited_agent"] == "codex"
    assert us["last_cited_note"] == "Applied canonical URL main-module check in cli.mjs"
    assert len(us["recent_events"]) == 2

    # Summary list_content & content_index also surface usage_summary and governance fields
    listed = content_service.list_content(project_id=proj_id, summary=True)
    assert listed[0]["usage_summary"]["state"] == "cited"
    idx_page = content_service.content_index(project_id=proj_id)
    assert idx_page["items"][0]["usage_summary"]["state"] == "cited"


# ============================================================================
# 3. Admission Guardrails, Reconciliation State Machine & Tombstone Shield
# ============================================================================


def test_admission_guardrails_block_proposals_guesses_raw_logs_and_route_temp_constraints(env_project):
    proj_id = env_project["project"]["id"]

    # 1. Temporary constraint -> routed to working_tasks.temp_constraints, NOT long-term contents
    routed = content_service.reconcile_long_term_memory(
        title="界面修改限制",
        content="这次先别改界面，只修后端数据库迁移逻辑",
        project_id=proj_id,
        category="preference",
        origin_authority="user_explicit",
        agent="cursor",
        is_agent_write=True,
    )
    assert routed["action"] == "ROUTE_TO_WORKING_TASK"
    assert routed["memory"] is None
    assert routed["working_task"] is not None
    assert any("这次先别改界面" in c for c in routed["working_task"]["temp_constraints"])
    assert content_service.list_content(project_id=proj_id) == []

    # 2. Unconfirmed AI proposal -> REJECTED
    proposal = content_service.reconcile_long_term_memory(
        title="缓存架构",
        content="我建议采用 Redis Cluster 替代本地缓存方案",
        project_id=proj_id,
        category="decision",
        agent="claude-code",
        is_agent_write=True,
    )
    assert proposal["ok"] is False
    assert proposal["action"] == "REJECTED"
    assert proposal["reason"] == "unconfirmed_ai_proposal"

    # 3. Unverified debugging guess -> REJECTED
    guess = content_service.reconcile_long_term_memory(
        title="超时排查",
        content="接口偶发 504 可能是因为连接池没有及时释放",
        project_id=proj_id,
        category="lesson",
        agent="codex",
        is_agent_write=True,
    )
    assert guess["ok"] is False
    assert guess["action"] == "REJECTED"
    assert guess["reason"] == "unverified_guess"

    # 4. Raw stacktrace / tool dump -> REJECTED
    raw_log = content_service.reconcile_long_term_memory(
        title="报错记录",
        content='Traceback (most recent call last):\n  File "/app/main.py", line 42, in run\n    raise RuntimeError("boom")',
        project_id=proj_id,
        category="lesson",
        agent="codex",
        is_agent_write=True,
    )
    assert raw_log["ok"] is False
    assert raw_log["action"] == "REJECTED"
    assert raw_log["reason"] == "raw_tool_output"

    # 5. Session filler title -> REJECTED
    filler = content_service.reconcile_long_term_memory(
        title="任务进展与阶段背景：调试会话",
        content="完成了初步代码阅读。",
        project_id=proj_id,
        category="background",
        agent="codex",
        is_agent_write=True,
    )
    assert filler["ok"] is False
    assert filler["action"] == "REJECTED"
    assert filler["reason"] == "session_filler_summary"

    assert content_service.list_content(project_id=proj_id) == []


def test_reconcile_add_noop_open_conflict_supersede_correct_and_tombstone(env_project):
    proj_id = env_project["project"]["id"]
    conv = conversation_service.upsert_conversation(
        agent="claude-code",
        project_id=proj_id,
        external_id="conv-gov-1",
        title="Package manager decision",
        messages=[{"role": "user", "content": "Always use pnpm in this repo."}],
    )

    # ADD
    added = content_service.reconcile_long_term_memory(
        title="包管理器规范",
        content="统一使用 pnpm 安装前端依赖",
        project_id=proj_id,
        category="preference",
        origin_authority="user_explicit",
        agent="claude-code",
        is_agent_write=True,
    )
    assert added["action"] == "ADD"
    mem_id = added["id"]

    # NOOP with provenance append
    noop = content_service.reconcile_long_term_memory(
        title="用户偏好：包管理器规范",
        content="请始终使用 pnpm 安装前端依赖。",
        project_id=proj_id,
        category="preference",
        origin_authority="user_explicit",
        agent="codex",
        source_id=conv["id"],
        source_kind="conversation",
        quote="Always use pnpm in this repo.",
        is_agent_write=True,
    )
    assert noop["action"] == "NOOP"
    assert noop["id"] == mem_id
    assert len(noop["memory"]["sources"]) == 1
    assert noop["memory"]["sources"][0]["quote"] == "Always use pnpm in this repo."
    idx_with_sources = content_service.content_index(project_id=proj_id)
    assert idx_with_sources["items"][0]["source_count"] == 1
    assert idx_with_sources["items"][0]["sources"][0]["quote"] == "Always use pnpm in this repo."

    # OPEN_CONFLICT when unconfirmed agent write contradicts active preference
    conf_res = content_service.reconcile_long_term_memory(
        title="包管理器规范",
        content="统一使用 npm 安装前端依赖",
        project_id=proj_id,
        category="preference",
        origin_authority="user_confirmed",
        agent="cursor",
        is_agent_write=True,
    )
    assert conf_res["action"] == "OPEN_CONFLICT"
    assert conf_res["existing_id"] == mem_id
    pending = conflict_service.list_conflicts(project_id=proj_id, status="pending")
    assert len(pending) == 1
    assert pending[0]["id"] == conf_res["conflict_id"]
    # Active memory was NOT silently overwritten
    assert "pnpm" in content_service.get_content(mem_id)["content"]

    # Explicit user correction (`correct_content`) -> retires old to historical + superseded_by_id
    corrected = content_service.correct_content(
        mem_id,
        content="统一改用 bun 安装与运行前端脚本",
        applicability={"paths": ["app/frontend/"]},
        actor="user",
        expected_version=1,
    )
    assert corrected["status"] == "active"
    assert corrected["origin_authority"] == "user_explicit"
    assert corrected["applicability"] == {"paths": ["app/frontend/"]}
    old_mem = content_service.get_content(mem_id)
    assert old_mem["status"] == "historical"
    assert old_mem["superseded_by_id"] == corrected["id"]

    # Mark outdated + status filtering
    bg = content_service.create_content(
        title="Node 18 兼容说明",
        content="CI 暂时运行在 Node 18。",
        project_id=proj_id,
        category="background",
    )
    content_service.mark_outdated(bg["id"], reason="CI upgraded to Node 22")
    assert retrieval_service._valid_content(sqlite3.connect(env_project["db_path"]), bg["id"]) is None
    active_list = content_service.list_content(project_id=proj_id, status="active")
    outdated_list = content_service.list_content(project_id=proj_id, status="outdated")
    all_list = content_service.list_content(project_id=proj_id, status="all")
    assert {it["id"] for it in active_list} == {corrected["id"]}
    assert {it["id"] for it in outdated_list} == {bg["id"]}
    assert {it["id"] for it in all_list} == {mem_id, corrected["id"], bg["id"]}

    # Delete & canonical tombstone blocks paraphrased / recategorized resurrection
    content_service.delete_content(corrected["id"], expected_version=1)
    resurrect = content_service.reconcile_long_term_memory(
        title="技术与架构决策：包管理器规范",
        content="请统一改用 bun 安装与运行前端脚本。",
        project_id=proj_id,
        category="decision",
        origin_authority="user_explicit",
        agent="codex",
        is_agent_write=True,
    )
    assert resurrect["action"] == "DROP_TOMBSTONED"
    with pytest.raises(HTTPException) as exc_info:
        content_service.create_content(
            title="用户偏好：包管理器规范",
            content="改用 bun 安装与运行前端脚本",
            project_id=proj_id,
            category="preference",
        )
    assert exc_info.value.status_code == 410


def test_exclude_category_and_consolidation_ignores_legacy_tasks(env_project):
    proj_id = env_project["project"]["id"]
    t1 = content_service.create_content(
        title="Legacy task A",
        content="Do something",
        project_id=proj_id,
        category="task",
        tags=["status:in_progress"],
    )
    t2 = content_service.create_content(
        title="Legacy task A",
        content="Do something more",
        project_id=proj_id,
        category="task",
        tags=["status:in_progress"],
    )
    pref = content_service.create_content(
        title="Code language",
        content="Respond in Chinese",
        project_id=proj_id,
        category="preference",
    )

    non_tasks = content_service.list_content(project_id=proj_id, exclude_category="task")
    assert [it["id"] for it in non_tasks] == [pref["id"]]

    cons = content_service.consolidate_memories(project_id=proj_id, scope="project")
    assert cons["merged_groups"] == 0
    assert content_service.get_content(t1["id"])["status"] == "active"
    assert content_service.get_content(t2["id"])["status"] == "active"


# ============================================================================
# 4. Short-Term Working Tasks, Cross-Agent Convergence & Lesson Promotion
# ============================================================================


def test_working_tasks_trivial_qa_guard_convergence_cas_citations_and_lifecycle(env_project):
    proj_id = env_project["project"]["id"]
    repo_dir = env_project["repo_dir"]

    # Trivial Q&A rejected
    assert working_memory_service.is_trivial_qa("现在有几个分支？") is True
    assert working_memory_service.is_trivial_qa("what branch am I on?") is True
    assert working_memory_service.is_trivial_qa("实现工作记忆与长期记忆分层") is False

    with pytest.raises(HTTPException) as qa_exc:
        working_memory_service.upsert_working_task(
            project_id=proj_id,
            title="现在有几个分支？",
        )
    assert qa_exc.value.status_code == 422

    skipped = working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="what branch am I on?",
        allow_skip_trivial=True,
    )
    assert skipped["action"] == "skipped_trivial_qa"

    # Seed a rule to cite during task execution
    rule = content_service.create_content(
        title="SQLite Migration Rule",
        content="Disable foreign_keys outside transaction before rebuilding tables.",
        project_id=proj_id,
        category="decision",
    )

    # Agent 1 (claude-code) starts a discussion-only task in repo_dir -> auto-detects git_branch
    t_v1 = working_memory_service.upsert_working_task(
        cwd=str(repo_dir),
        title="重构记忆治理状态机",
        execution_mode="discussion_only",
        goal="将短期任务与长期记忆拆表并补齐状态机",
        completed_steps=[{"step": "完成现有表结构审计", "evidence": "database.py:162"}],
        current_state="正在讨论方案，尚未授权改代码",
        temp_constraints=["这次先别改前端样式"],
        next_steps=["确认契约字段", "编写迁移脚本"],
        agent="claude-code",
    )
    assert t_v1["project_id"] == proj_id
    assert t_v1["git_branch"] == "feat/working-memory"
    assert t_v1["execution_mode"] == "discussion_only"
    assert t_v1["must_not_execute"] is True
    assert t_v1["version"] == 1
    assert project_service.get_project(proj_id)["counts"]["tasks"] == 1

    # Agent 2 (codex) converges onto the SAME task by normalized title without task_id
    t_v2 = working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="Task: 重构记忆治理状态机",
        add_completed_step={"step": "对齐契约模型字段", "evidence": "models.py"},
        add_failed_attempt={"attempt": "ALTER TABLE 修改 CHECK", "reason": "SQLite 不支持直接修改 CHECK"},
        add_blocker="等待前端联调字段确认",
        cited_memory_ids=[rule["id"]],
        citation_note="遵循 SQLite 表重建外键规范",
        agent="codex",
    )
    assert t_v2["id"] == t_v1["id"]
    assert t_v2["version"] == 2
    assert t_v2["last_agent"] == "codex"
    # Discussion-only firewall preserved across convergence!
    assert t_v2["execution_mode"] == "discussion_only"
    assert t_v2["must_not_execute"] is True
    assert len(t_v2["completed_steps"]) == 2
    assert t_v2["temp_constraints"] == ["这次先别改前端样式"]
    assert len(t_v2["failed_attempts"]) == 1
    assert t_v2["blockers"] == ["等待前端联调字段确认"]
    assert len(t_v2["events"]) == 2

    # Citation receipt reflected on long-term memory!
    rule_after = content_service.get_content(rule["id"])
    assert rule_after["usage_summary"]["state"] == "cited"
    assert rule_after["usage_summary"]["cited_count"] == 1
    assert rule_after["usage_summary"]["last_cited_agent"] == "codex"

    # CAS expected_version mismatch -> 409
    with pytest.raises(HTTPException) as cas_exc:
        working_memory_service.patch_working_task(
            t_v2["id"],
            current_state="stale write",
            expected_version=1,
        )
    assert cas_exc.value.status_code == 409

    # Pause -> immediately exits active_only list & active task count
    paused = working_memory_service.transition_working_task(
        t_v2["id"],
        "paused",
        actor="user",
        expected_version=2,
    )
    assert paused["status"] == "paused"
    assert working_memory_service.list_working_tasks(proj_id, active_only=True) == []
    assert len(working_memory_service.list_working_tasks(proj_id, active_only=False)) == 1
    assert project_service.get_project(proj_id)["counts"]["tasks"] == 0


def test_legacy_task_sync_deduplication_and_promote_task_conclusion(env_project):
    proj_id = env_project["project"]["id"]
    conv = conversation_service.upsert_conversation(
        agent="codex",
        project_id=proj_id,
        external_id="conv-promote-1",
        title="SQLite CHECK migration debugging",
        messages=[{"role": "user", "content": "Fix the status CHECK constraint safely."}],
    )

    # Create a legacy contents(category='task') item and verify count + sync deduplication
    legacy = content_service.create_content(
        title="修复 SQLite CHECK 迁移",
        content="需要在不破坏外键的前提下扩展 status 枚举",
        project_id=proj_id,
        category="task",
        tags=["status:in_progress"],
        agent="claude-code",
    )
    assert project_service.get_project(proj_id)["counts"]["tasks"] == 1

    tasks = working_memory_service.list_working_tasks(proj_id, active_only=True)
    assert len(tasks) == 1
    synced_task = tasks[0]
    assert synced_task["legacy_content_id"] == legacy["id"]
    assert synced_task["status"] == "in_progress"
    # Count must remain 1 (no double counting between working_tasks and legacy contents)
    assert project_service.get_project(proj_id)["counts"]["tasks"] == 1

    # Attach source_conversation_id to task
    patched = working_memory_service.patch_working_task(
        synced_task["id"],
        source_conversation_id=conv["id"],
        expected_version=synced_task["version"],
    )

    # Promoting without applicability MUST fail with 422
    with pytest.raises(HTTPException) as missing_app:
        working_memory_service.promote_task_conclusion(
            patched["id"],
            title="SQLite 扩展 CHECK 约束方法",
            content="新建临时表复制数据后 DROP 原表再 RENAME，避免 ALTER RENAME 污染子表外键。",
            applicability={},
        )
    assert missing_app.value.status_code == 422

    # Promoting temporary constraint MUST fail with 422
    with pytest.raises(HTTPException) as temp_leak:
        working_memory_service.promote_task_conclusion(
            patched["id"],
            title="临时限制",
            content="这次先别改界面",
            applicability={"when": "current sprint"},
        )
    assert temp_leak.value.status_code == 422

    # Valid promotion creates a source-backed conditional lesson + conversation source (never 'task')
    promoted = working_memory_service.promote_task_conclusion(
        patched["id"],
        title="SQLite 扩展 CHECK 约束方法",
        content="关闭 foreign_keys 后创建新表复制数据、DROP 旧表再 RENAME 新表，防止 SQLite 3.26+ 改写子表外键目标。",
        category="lesson",
        applicability={
            "when": "需要在 SQLite 中修改已有表的 CHECK 约束且存在子表外键引用时",
            "env": "SQLite >= 3.26",
            "paths": ["app/backend/app/database.py"],
        },
        evidence_quote="Fix the status CHECK constraint safely.",
        actor="codex",
        complete_task=True,
    )
    assert promoted["task"]["status"] == "completed"
    assert promoted["task"]["completed_at"] is not None
    mem = promoted["memory"]
    assert mem["origin_authority"] == "source_backed"
    assert mem["promoted_from_task_id"] == patched["id"]
    assert mem["applicability"]["env"] == "SQLite >= 3.26"
    assert len(mem["sources"]) == 1
    assert mem["sources"][0]["kind"] == "conversation"
    assert mem["sources"][0]["id"] == conv["id"]
    assert project_service.get_project(proj_id)["counts"]["tasks"] == 0

    # Deleting a synced task prevents legacy resurrection
    working_memory_service.delete_working_task(patched["id"])
    assert working_memory_service.list_working_tasks(proj_id, active_only=False) == []


# ============================================================================
# 5. Pre-commit final-audit regressions (ambiguity gate, legacy retirement,
#    promotion admission, versioned retirement, bounded bootstrap, history)
# ============================================================================


def test_ambiguous_active_tasks_fail_closed_in_bootstrap_and_assemble(env_project):
    proj_id = env_project["project"]["id"]
    t1 = working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="重构限流核心",
        goal="对齐限流状态机",
        execution_mode="approved",
    )
    working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="补齐审计导出",
        goal="流式导出 CSV",
        execution_mode="approved",
    )

    boot = retrieval_service.bootstrap_working_memory(project_id=proj_id, query="限流与导出", consumer_id="codex")
    assert boot["task_selection_required"] is True
    assert boot["selected_task"] is None
    assert boot["must_not_execute"] is True

    pack = retrieval_service.assemble_context(query="项目整体状态检查", project_id=proj_id, consumer_id="codex")
    assert pack["task_selection_required"] is True
    assert pack["must_not_execute"] is True

    resolved = retrieval_service.bootstrap_working_memory(
        project_id=proj_id, task_id=t1["id"], consumer_id="codex"
    )
    assert resolved["task_selection_required"] is False
    assert resolved["must_not_execute"] is False


def test_legacy_outdated_pauses_instead_of_completing_and_keeps_discussion_gate(env_project):
    proj_id = env_project["project"]["id"]

    legacy = content_service.create_content(
        title="修复旧任务镜像",
        content="旧任务正文",
        project_id=proj_id,
        category="task",
        tags=["status:in_progress"],
        agent="claude-code",
    )
    synced = next(
        t for t in working_memory_service.list_working_tasks(proj_id, active_only=True)
        if t["legacy_content_id"] == legacy["id"]
    )
    assert synced["status"] == "in_progress"

    content_service.mark_outdated(legacy["id"], reason="旧任务笔记已过期")
    retired = next(
        t for t in working_memory_service.list_working_tasks(proj_id, active_only=False)
        if t["id"] == synced["id"]
    )
    assert retired["status"] == "paused"
    assert retired["completed_at"] is None

    legacy_gate = content_service.create_content(
        title="讨论中的限流方案",
        content="先讨论方案，不落地",
        project_id=proj_id,
        category="task",
        tags=["status:pending"],
        agent="claude-code",
    )
    gate_task = next(
        t for t in working_memory_service.list_working_tasks(proj_id, active_only=True)
        if t["legacy_content_id"] == legacy_gate["id"]
    )
    patched = working_memory_service.patch_working_task(
        gate_task["id"],
        execution_mode="discussion_only",
        expected_version=gate_task["version"],
    )
    assert patched["must_not_execute"] is True

    content_service.mark_outdated(legacy_gate["id"], reason="旧讨论笔记过期")
    gate_after = next(
        t for t in working_memory_service.list_working_tasks(proj_id, active_only=True)
        if t["id"] == gate_task["id"]
    )
    assert gate_after["status"] == "active"
    assert gate_after["execution_mode"] == "discussion_only"
    assert gate_after["must_not_execute"] is True


def test_promote_task_conclusion_rejects_agent_writes_and_unverified_quotes(env_project):
    proj_id = env_project["project"]["id"]
    conv = conversation_service.upsert_conversation(
        agent="codex",
        project_id=proj_id,
        external_id="conv-promote-guard-1",
        title="Rate limit window alignment",
        messages=[{"role": "user", "content": "请确认限流窗口改成 60 秒。"}],
    )
    task = working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="对齐限流窗口",
        goal="把窗口改为 60 秒",
        execution_mode="approved",
        source_conversation_id=conv["id"],
        agent="codex",
    )
    applicability = {"when": "限流窗口配置"}

    discussion = working_memory_service.upsert_working_task(
        project_id=proj_id,
        title="讨论限流窗口方案",
        goal="仅讨论，不改代码",
        execution_mode="discussion_only",
        agent="codex",
    )
    with pytest.raises(HTTPException) as disc_exc:
        working_memory_service.promote_task_conclusion(
            discussion["id"],
            title="限流窗口经验",
            content="限流窗口固定为 60 秒。",
            applicability=applicability,
            actor="codex",
        )
    assert disc_exc.value.status_code == 422
    assert "Discussion-only" in disc_exc.value.detail

    with pytest.raises(HTTPException) as proposal_exc:
        working_memory_service.promote_task_conclusion(
            task["id"],
            title="限流窗口方案",
            content="我建议先把窗口改成 30 秒，待用户确认后再落地。",
            applicability=applicability,
            actor="codex",
        )
    assert proposal_exc.value.status_code == 422
    assert "unconfirmed_ai_proposal" in proposal_exc.value.detail

    with pytest.raises(HTTPException) as quote_exc:
        working_memory_service.promote_task_conclusion(
            task["id"],
            title="限流窗口经验",
            content="限流窗口固定为 60 秒。",
            applicability=applicability,
            evidence_quote="用户要求把窗口改成 5 秒",
            actor="codex",
        )
    assert quote_exc.value.status_code == 422
    assert "evidence_quote" in quote_exc.value.detail

    promoted = working_memory_service.promote_task_conclusion(
        task["id"],
        title="限流窗口经验",
        content="限流窗口固定为 60 秒。",
        applicability=applicability,
        evidence_quote="请确认限流窗口改成 60 秒。",
        actor="codex",
    )
    assert promoted["memory"]["origin_authority"] == "source_backed"
    assert promoted["promoted"] is True
    assert promoted["memory"]["promoted_from_task_id"] == task["id"]


def test_historical_supersede_bumps_version_and_blocks_reactivation(env_project):
    proj_id = env_project["project"]["id"]
    original = content_service.create_content(
        title="限流窗口策略",
        content="窗口固定 60 秒。",
        project_id=proj_id,
        category="decision",
    )
    assert original["version"] == 1

    corrected = content_service.correct_content(
        original["id"],
        content="窗口固定 30 秒。",
        actor="user",
        expected_version=1,
    )
    retired = content_service.get_content(original["id"])
    assert retired["status"] == "historical"
    assert retired["superseded_by_id"] == corrected["id"]
    assert retired["version"] == 2

    with pytest.raises(HTTPException) as reactivate:
        content_service.update_content(original["id"], status="active", expected_version=2)
    assert reactivate.value.status_code == 409

    content_service.create_content(
        title="限流窗口策略",
        content="窗口固定 20 秒。",
        project_id=proj_id,
        category="decision",
        supersedes_id=corrected["id"],
    )
    retired_again = content_service.get_content(corrected["id"])
    assert retired_again["status"] == "historical"
    assert retired_again["version"] == 2


def test_list_working_tasks_history_status_offset_and_summary(env_project):
    proj_id = env_project["project"]["id"]
    done = working_memory_service.upsert_working_task(project_id=proj_id, title="重构限流阶段一", goal="对齐状态机")
    paused = working_memory_service.upsert_working_task(project_id=proj_id, title="修复审计阶段二", goal="补齐审计字段")
    active = working_memory_service.upsert_working_task(project_id=proj_id, title="补齐导出阶段三", goal="流式导出")

    working_memory_service.transition_working_task(
        done["id"], "completed", actor="user", expected_version=done["version"]
    )
    working_memory_service.transition_working_task(
        paused["id"], "paused", actor="user", expected_version=paused["version"]
    )

    summary = working_memory_service.summarize_working_tasks(proj_id)
    assert summary == {"active": 1, "history": 2, "all": 3}

    history = working_memory_service.list_working_tasks(proj_id, status="history", include_events=False)
    assert {t["id"] for t in history} == {done["id"], paused["id"]}
    assert {t["id"] for t in working_memory_service.list_working_tasks(proj_id, status="active")} == {active["id"]}

    first_page = working_memory_service.list_working_tasks(
        proj_id, status="all", include_events=False, limit=2, offset=0
    )
    second_page = working_memory_service.list_working_tasks(
        proj_id, status="all", include_events=False, limit=2, offset=2
    )
    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {t["id"] for t in [*first_page, *second_page]} == {done["id"], paused["id"], active["id"]}

    with pytest.raises(HTTPException) as invalid:
        working_memory_service.list_working_tasks(proj_id, status="not_a_status")
    assert invalid.value.status_code == 422


def test_bootstrap_working_memory_bounds_serialized_payload_for_verbose_tasks(env_project):
    proj_id = env_project["project"]["id"]
    verbose = "限流与审计链路的完整实现细节与待确认事项" * 22
    for index in range(6):
        working_memory_service.upsert_working_task(
            project_id=proj_id,
            title=f"重构限流链路阶段 {index}",
            goal=verbose,
            current_state=verbose,
            status="in_progress" if index % 2 else "active",
            execution_mode="discussion_only" if index == 0 else "approved",
            temp_constraints=[f"这次先别改动模块 {index}"],
            blockers=[verbose[:180]],
            next_steps=[verbose[:180]],
        )

    boot = retrieval_service.bootstrap_working_memory(project_id=proj_id, max_tokens=3200)
    assert boot["estimated_tokens"] <= 3200
    assert len(json.dumps(boot, ensure_ascii=False).encode("utf-8")) == boot["estimated_tokens"]
    assert boot["tasks_truncated"] is True
    assert boot["task_selection_required"] is True
    assert boot["must_not_execute"] is True
    assert boot["active_tasks"]
    for task in boot["active_tasks"]:
        assert {"id", "title", "status", "execution_mode", "temp_constraints"} <= set(task)
