from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.adapters import read_sessions
from app.database import create_schema, db, new_id, now_iso, seed_data
from app.services import (
    connection_service,
    content_service,
    conversation_service,
    host_registry,
    project_service,
    working_memory_service,
)


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "contextcore_recovery.sqlite"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_path))
    create_schema()
    seed_data()
    return tmp_path


def test_broad_and_ephemeral_directories_never_create_or_swallow_projects(isolated_env: Path) -> None:
    broad_and_ephemeral = [
        ("/", None),
        ("/Users", None),
        ("/Users/alice", {"name": "alice"}),
        ("/Users/alice/Desktop", {"name": "Desktop"}),
        ("/Users/alice/Documents", {"name": "Documents"}),
        ("/Users/alice/Downloads", None),
        ("/tmp", {"name": "tmp"}),
        ("/private/tmp/cc-probe", {"name": "cc-probe"}),
        ("/var/folders/62/rw1z50sd6sx4z3hz7w2phmqh0000gn/T", {"name": "T"}),
        (
            "/Users/alice/.aionui/conversations/users/u1/2026/08/24/omp-temp-ee815fb1",
            {"name": "omp-temp-ee815fb1"},
        ),
        (
            "/Users/alice/Library/Application Support/AionUi/aionui/conversations/omp-temp-1234",
            {"name": "omp-temp-1234"},
        ),
        ("/Users/alice/.gemini/antigravity/brain/00000000-0000-4000-8000-000000000001", None),
    ]
    for raw_cwd, meta in broad_and_ephemeral:
        assert project_service.is_broad_or_ephemeral_project_path(raw_cwd) is True, raw_cwd
        assert project_service.reconstruct_or_resolve_project(raw_cwd, metadata=meta) is None, raw_cwd

    assert project_service.list_projects() == []

    # Simulate a legacy database that already had a broad '/Users/alice' active project.
    legacy = project_service.create_project(name="alice", directories=["/Users/alice"])
    assert legacy["id"]

    # Resolving or reconstructing '/Users/alice/Desktop/改造' must NOT be swallowed by '/Users/alice'.
    resolved_before, ambiguous_before = project_service.resolve_project_by_path("/Users/alice/Desktop/改造")
    assert resolved_before is None
    assert ambiguous_before is False

    gaizao_id = project_service.reconstruct_or_resolve_project("/Users/alice/Desktop/改造")
    assert gaizao_id is not None
    assert gaizao_id != legacy["id"]
    gaizao = project_service.get_project(gaizao_id)
    assert gaizao["name"] == "改造"
    assert "/Users/alice/Desktop/改造" in gaizao["directories"]


def test_repair_misclassified_projects_splits_blackhole_and_assigns_orphans(isolated_env: Path) -> None:
    # Seed legacy blackhole project ('/Users/alice') and ephemeral project ('omp-temp-999')
    blackhole = project_service.create_project(name="alice", directories=["/Users/alice"])
    ephemeral = project_service.create_project(
        name="omp-temp-999",
        directories=["/Users/alice/.aionui/conversations/users/u1/omp-temp-999"],
    )

    conv_gaizao_1 = conversation_service.upsert_conversation(
        agent="codex",
        title="升级记忆系统",
        external_id="codex:gaizao-1",
        project_id=blackhole["id"],
        cwd="/Users/alice/Desktop/改造",
        messages=[{"id": "m1", "role": "user", "content": "开始改造记忆模块", "timestamp": "2026-11-12T04:00:00Z"}],
        started_at="2026-11-12T04:00:00Z",
        updated_at="2026-11-12T04:30:00Z",
    )
    conv_gaizao_nested = conversation_service.upsert_conversation(
        agent="claude-code",
        title="修复 ContextCore 子目录测试",
        external_id="claude:gaizao-nested",
        project_id=blackhole["id"],
        cwd="/Users/alice/Desktop/改造/ContextCore",
        messages=[{"id": "m1", "role": "user", "content": "跑一下后端单测", "timestamp": "2026-11-12T05:00:00Z"}],
        started_at="2026-11-12T05:00:00Z",
        updated_at="2026-11-12T05:10:00Z",
    )
    conv_juecezhe = conversation_service.upsert_conversation(
        agent="omp",
        title="决策者方案评审",
        external_id="omp:juecezhe-1",
        project_id=blackhole["id"],
        cwd="/Users/alice/Desktop/决策者",
        messages=[{"id": "m1", "role": "user", "content": "整理评审意见", "timestamp": "2026-11-11T09:00:00Z"}],
        started_at="2026-11-11T09:00:00Z",
        updated_at="2026-11-11T09:20:00Z",
    )
    conv_home_loose = conversation_service.upsert_conversation(
        agent="omp",
        title="随手问个命令行问题",
        external_id="omp:loose-home",
        project_id=blackhole["id"],
        cwd="/Users/alice",
        messages=[{"id": "m1", "role": "user", "content": "mac 怎么看端口占用", "timestamp": "2026-10-25T01:00:00Z"}],
        started_at="2026-10-25T01:00:00Z",
        updated_at="2026-10-25T01:02:00Z",
    )
    conv_ephemeral = conversation_service.upsert_conversation(
        agent="omp",
        title="临时草稿",
        external_id="omp:temp-999",
        project_id=ephemeral["id"],
        cwd="/Users/alice/.aionui/conversations/users/u1/omp-temp-999",
        messages=[{"id": "m1", "role": "user", "content": "测试连接", "timestamp": "2026-08-26T01:00:00Z"}],
        started_at="2026-08-26T01:00:00Z",
        updated_at="2026-08-26T01:01:00Z",
    )

    # Linked memory & working task trapped on blackhole project
    linked_mem = content_service.create_content(
        project_id=blackhole["id"],
        kind="memory",
        category="decision",
        title="测试分支隔离原则",
        content="保持正式库只读不改动，使用独立测试目录验证。",
        source_id=conv_gaizao_1["id"],
        source_kind="conversation",
    )
    linked_task = working_memory_service.upsert_working_task(
        project_id=blackhole["id"],
        title="验证本地 8420 测试实例",
        goal="确认项目列表与历史记录完整显示",
        cwd="/Users/alice/Desktop/改造/ContextCore",
        source_conversation_id=conv_gaizao_1["id"],
        agent="antigravity",
    )
    handoff_id = new_id("ho")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO handoffs (id, project_id, source_agent, target_agent, status, card, source_ids, source_revisions, created_at)
            VALUES (?, ?, 'omp', 'codex', 'awaiting_confirmation', ?, ?, '{}', ?)
            """,
            (
                handoff_id,
                blackhole["id"],
                json.dumps({"working_directory": "/Users/alice/Desktop/改造"}),
                json.dumps([conv_gaizao_1["id"], linked_mem["id"]]),
                now_iso(),
            ),
        )

    # Unassigned Antigravity session on disk whose cwd was previously ""
    ag_dir = isolated_env / "brain" / "sess-ag-gaizao" / ".system_generated" / "logs"
    ag_dir.mkdir(parents=True)
    ag_transcript = ag_dir / "transcript.jsonl"
    ag_steps = [
        {
            "step_index": 1,
            "type": "USER_INPUT",
            "created_at": "2026-11-12T05:20:00Z",
            "content": "<USER_REQUEST>检查改造项目为什么没显示</USER_REQUEST>",
            "metadata": {"cwd": "/Users/alice/Desktop/改造"},
        },
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-11-12T05:22:00Z",
            "content": "正在读取项目服务代码。",
            "tool_calls": [
                {
                    "name": "read_file",
                    "args": {"Path": "/Users/alice/Desktop/改造/ContextCore/app/backend/app/services/project_service.py"},
                }
            ],
        },
    ]
    ag_transcript.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in ag_steps) + "\n", encoding="utf-8")
    conv_ag_orphan = conversation_service.upsert_conversation(
        agent="antigravity",
        title="检查改造项目为什么没显示",
        external_id="antigravity:sess-ag-gaizao",
        project_id=None,
        cwd="",
        source_path=str(ag_transcript),
        messages=[{"id": "m1", "role": "user", "content": "检查改造项目为什么没显示", "timestamp": "2026-11-12T05:20:00Z"}],
        started_at="2026-11-12T05:20:00Z",
        updated_at="2026-11-12T05:22:00Z",
    )
    with db() as conn:
        conn.execute(
            "UPDATE connections SET enabled = 1, paused = 0, paths = ? WHERE id = 'antigravity'",
            (json.dumps([str(isolated_env)]),),
        )

    report = project_service.repair_misclassified_projects()
    assert report["repaired_conversations"] >= 4

    active_projects = project_service.list_projects(status="active")
    by_name = {p["name"]: p for p in active_projects}
    assert "改造" in by_name
    assert "决策者" in by_name
    assert "alice" not in by_name
    assert "omp-temp-999" not in by_name

    gaizao_proj = by_name["改造"]
    juecezhe_proj = by_name["决策者"]

    # 改造 holds conv_gaizao_1, conv_gaizao_nested, and backfilled conv_ag_orphan
    assert conversation_service.get_conversation(conv_gaizao_1["id"], include_messages=False)["project_id"] == gaizao_proj["id"]
    assert conversation_service.get_conversation(conv_gaizao_nested["id"], include_messages=False)["project_id"] == gaizao_proj["id"]
    repaired_ag = conversation_service.get_conversation(conv_ag_orphan["id"], include_messages=False)
    assert repaired_ag["project_id"] == gaizao_proj["id"]
    assert repaired_ag["cwd"] == "/Users/alice/Desktop/改造"
    assert conversation_service.get_conversation(conv_juecezhe["id"], include_messages=False)["project_id"] == juecezhe_proj["id"]

    # Linked memory, working task, and handoff moved to 改造
    assert content_service.get_content(linked_mem["id"])["project_id"] == gaizao_proj["id"]
    assert working_memory_service.get_working_task(linked_task["id"])["project_id"] == gaizao_proj["id"]
    with db() as conn:
        ho_row = conn.execute("SELECT project_id FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        assert ho_row["project_id"] == gaizao_proj["id"]

    # Loose home/ephemeral conversations remain intact and queryable as unassigned
    loose_after = conversation_service.get_conversation(conv_home_loose["id"], include_messages=False)
    ephem_after = conversation_service.get_conversation(conv_ephemeral["id"], include_messages=False)
    assert loose_after["project_id"] is None
    assert ephem_after["project_id"] is None
    unassigned_page = conversation_service.conversation_index(project_id="__unassigned__")
    unassigned_ids = {item["id"] for item in unassigned_page["items"]}
    assert conv_home_loose["id"] in unassigned_ids
    assert conv_ephemeral["id"] in unassigned_ids

    # Second run is a clean idempotent no-op
    report_second = project_service.repair_misclassified_projects()
    assert report_second["repaired_conversations"] == 0


def test_project_recency_ordering_tracks_conversations_memories_and_tasks_by_epoch_ms(isolated_env: Path) -> None:
    # Project A reconstructed first with a recent Nov 12 timestamp (+00:00 microseconds)
    p_recent_id = project_service.reconstruct_or_resolve_project(
        str(isolated_env / "proj-recent"),
        activity_at="2026-11-12T05:22:41.500000+00:00",
    )
    # Project B reconstructed second with a Z timestamp 0.5s EARLIER (tests non-lexicographic epoch-ms comparison!)
    p_z_id = project_service.reconstruct_or_resolve_project(
        str(isolated_env / "proj-z"),
        activity_at="2026-11-12T05:22:41Z",
    )
    # Project C reconstructed last during historical sync, but its actual session is from Aug 31!
    p_stale_id = project_service.reconstruct_or_resolve_project(
        str(isolated_env / "proj-stale"),
        activity_at="2026-08-31T10:00:00Z",
    )
    assert p_recent_id and p_z_id and p_stale_id

    conversation_service.upsert_conversation(
        agent="codex",
        title="Recent work",
        external_id="codex:r1",
        project_id=p_recent_id,
        cwd=str(isolated_env / "proj-recent"),
        messages=[{"id": "m1", "role": "user", "content": "Work", "timestamp": "2026-11-12T05:22:41.500000+00:00"}],
        started_at="2026-11-12T05:22:41.500000+00:00",
        updated_at="2026-11-12T05:22:41.500000+00:00",
    )
    conversation_service.upsert_conversation(
        agent="codex",
        title="Slightly earlier Z work",
        external_id="codex:z1",
        project_id=p_z_id,
        cwd=str(isolated_env / "proj-z"),
        messages=[{"id": "m1", "role": "user", "content": "Z", "timestamp": "2026-11-12T05:22:41Z"}],
        started_at="2026-11-12T05:22:41Z",
        updated_at="2026-11-12T05:22:41Z",
    )
    conversation_service.upsert_conversation(
        agent="omp",
        title="August archive",
        external_id="omp:s1",
        project_id=p_stale_id,
        cwd=str(isolated_env / "proj-stale"),
        messages=[{"id": "m1", "role": "user", "content": "Old", "timestamp": "2026-08-31T10:00:00Z"}],
        started_at="2026-08-31T10:00:00Z",
        updated_at="2026-08-31T10:00:00Z",
    )

    ordered = project_service.list_projects(status="active")
    assert [p["id"] for p in ordered] == [p_recent_id, p_z_id, p_stale_id]

    # Saving a new memory on p_stale_id immediately promotes it to the top
    content_service.create_content(
        project_id=p_stale_id,
        kind="memory",
        category="lesson",
        title="New finding today",
        content="Verified behavior on test branch.",
    )
    ordered_after_mem = project_service.list_projects(status="active")
    assert ordered_after_mem[0]["id"] == p_stale_id

    # Updating a working task on p_z_id promotes p_z_id to the top
    working_memory_service.upsert_working_task(
        project_id=p_z_id,
        title="Active sprint task",
        goal="Ship recency fix",
        agent="codex",
    )
    ordered_after_task = project_service.list_projects(status="active")
    assert ordered_after_task[0]["id"] == p_z_id


def test_codex_sqlite_state_and_thread_history_adapter_and_discovery(isolated_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = isolated_env / "fake_home"
    codex_dir = home / ".codex"
    sessions_dir = codex_dir / "sessions"
    sessions_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text("DO_NOT_READ_SECRET", encoding="utf-8")

    state_db = codex_dir / "state_5.sqlite"
    hist_db = codex_dir / "thread_history_1.sqlite"

    with sqlite3.connect(state_db) as sc:
        sc.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT,
                created_at INTEGER,
                updated_at INTEGER,
                source TEXT,
                model_provider TEXT,
                cwd TEXT,
                title TEXT,
                archived INTEGER DEFAULT 0,
                git_sha TEXT,
                git_branch TEXT,
                first_user_message TEXT,
                model TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                thread_source TEXT,
                preview TEXT,
                name TEXT,
                attached_thread_parent_id TEXT
            )
            """
        )
        sc.executemany(
            """
            INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                archived, git_sha, git_branch, first_user_message, model, created_at_ms, updated_at_ms,
                thread_source, preview, name, attached_thread_parent_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "019f0000-0000-7000-8000-000000000001",
                    "",
                    1794460000,
                    1794460600,
                    "vscode",
                    "openai",
                    "/Users/alice/Desktop/改造",
                    "重构跨 Agent 工作记忆",
                    0,
                    "abc1234",
                    "test/relay-worktree-identity",
                    "重构跨 Agent 工作记忆",
                    "gpt-5.4",
                    1794460000000,
                    1794460600000,
                    "user",
                    "已完成重构",
                    "",
                    None,
                ),
                (
                    "019f0000-0000-7000-8000-000000000002",
                    "",
                    1794450000,
                    1794450300,
                    "exec",
                    "openai",
                    "/Users/alice/Desktop/决策者",
                    "",
                    0,
                    None,
                    None,
                    "请检查决策者报告结构",
                    "gpt-5.4",
                    1794450000000,
                    1794450300000,
                    "user",
                    "报告结构如下",
                    "",
                    None,
                ),
                (
                    "019f0000-0000-7000-8000-000000000099",
                    "",
                    1794460100,
                    1794460200,
                    '{"subagent":{"thread_spawn":{"parent_thread_id":"019f0000-0000-7000-8000-000000000001"}}}',
                    "openai",
                    "/Users/alice/Desktop/改造",
                    "Subagent worker",
                    0,
                    None,
                    None,
                    "Internal worker prompt",
                    "gpt-5.4",
                    1794460100000,
                    1794460200000,
                    "subagent",
                    "Worker done",
                    "",
                    None,
                ),
            ],
        )

    with sqlite3.connect(hist_db) as hc:
        hc.execute(
            """
            CREATE TABLE thread_turns (
                thread_id TEXT,
                turn_id TEXT,
                rollout_ordinal INTEGER,
                status TEXT,
                started_at INTEGER,
                completed_at INTEGER
            )
            """
        )
        hc.execute(
            """
            CREATE TABLE thread_items (
                thread_id TEXT,
                turn_id TEXT,
                item_id TEXT,
                rollout_ordinal INTEGER,
                created_at_ms INTEGER,
                item_json TEXT,
                item_type TEXT,
                updated_at_ordinal INTEGER,
                started_at_ms INTEGER,
                completed_at_ms INTEGER
            )
            """
        )
        hc.executemany(
            """
            INSERT INTO thread_items (
                thread_id, turn_id, item_id, rollout_ordinal, created_at_ms, item_json, item_type,
                updated_at_ordinal, started_at_ms, completed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "019f0000-0000-7000-8000-000000000001",
                    "t1",
                    "u1",
                    1,
                    1794460001000,
                    json.dumps(
                        {
                            "id": "u1",
                            "type": "userMessage",
                            "content": [{"type": "input_text", "text": "帮我升级记忆系统", "text_elements": []}],
                        },
                        ensure_ascii=False,
                    ),
                    "userMessage",
                    1,
                    1794460001000,
                    1794460001000,
                ),
                (
                    "019f0000-0000-7000-8000-000000000001",
                    "t1",
                    "cmd1",
                    2,
                    1794460100000,
                    json.dumps(
                        {
                            "id": "cmd1",
                            "type": "commandExecution",
                            "command": "pytest -q",
                            "cwd": "/Users/alice/Desktop/改造/ContextCore",
                            "exitCode": 0,
                            "status": "completed",
                        }
                    ),
                    "commandExecution",
                    2,
                    1794460100000,
                    1794460105000,
                ),
                (
                    "019f0000-0000-7000-8000-000000000001",
                    "t1",
                    "a1",
                    3,
                    1794460600000,
                    json.dumps(
                        {
                            "id": "a1",
                            "type": "agentMessage",
                            "text": "跨 Agent 工作记忆已完成升级并通过测试。",
                            "phase": "final_answer",
                        },
                        ensure_ascii=False,
                    ),
                    "agentMessage",
                    3,
                    1794460590000,
                    1794460600000,
                ),
                (
                    "019f0000-0000-7000-8000-000000000002",
                    "t1",
                    "u2",
                    1,
                    1794450001000,
                    json.dumps(
                        {
                            "id": "u2",
                            "type": "userMessage",
                            "content": [{"type": "input_text", "text": "请检查决策者报告结构", "text_elements": []}],
                        },
                        ensure_ascii=False,
                    ),
                    "userMessage",
                    1,
                    1794450001000,
                    1794450001000,
                ),
                (
                    "019f0000-0000-7000-8000-000000000002",
                    "t1",
                    "a2",
                    2,
                    1794450300000,
                    json.dumps({"id": "a2", "type": "agentMessage", "text": "已核对决策者报告六段结构。"}, ensure_ascii=False),
                    "agentMessage",
                    2,
                    1794450290000,
                    1794450300000,
                ),
                (
                    "019f0000-0000-7000-8000-000000000099",
                    "t1",
                    "sub-u",
                    1,
                    1794460101000,
                    json.dumps({"id": "sub-u", "type": "userMessage", "content": [{"type": "text", "text": "Subagent task"}]}),
                    "userMessage",
                    1,
                    1794460101000,
                    1794460101000,
                ),
            ],
        )

    # Metadata discovery must find SQLite history paths without reading auth.json
    orig_open = Path.open

    def guard_secret(self: Path, *args, **kwargs):
        if self.name == "auth.json":
            raise AssertionError("auth.json must never be opened")
        return orig_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard_secret)
    discovered = {item["id"]: item for item in host_registry.discover_hosts(home=home, env={})}
    codex_paths = discovered["codex"]["paths"]
    assert str(state_db) in codex_paths
    assert str(hist_db) in codex_paths
    assert all("auth.json" not in p for p in codex_paths)

    # Adapter reads 2 user sessions and excludes the subagent thread
    sessions = read_sessions("codex", codex_paths)
    assert len(sessions) == 2
    by_ext = {s["external_id"]: s for s in sessions}
    assert "codex:019f0000-0000-7000-8000-000000000099" not in by_ext

    s1 = by_ext["codex:019f0000-0000-7000-8000-000000000001"]
    assert s1["title"] == "重构跨 Agent 工作记忆"
    assert s1["cwd"] == "/Users/alice/Desktop/改造"
    assert len(s1["messages"]) == 2
    assert s1["messages"][0]["role"] == "user"
    assert s1["messages"][0]["content"] == "帮我升级记忆系统"
    assert s1["messages"][1]["role"] == "assistant"
    assert "已完成升级" in s1["messages"][1]["content"]
    assert "commandExecution" in s1["process_summary"]

    # End-to-end sync_connection creates projects '改造' & '决策者' ordered by recency
    connection_service.patch_connection("codex", enabled=True, paused=False, paths=codex_paths, confirmed=True)
    connection_service.sync_connection("codex", force=True)

    synced_projects = project_service.list_projects(status="active")
    assert [p["name"] for p in synced_projects] == ["改造", "决策者"]
    assert synced_projects[0]["counts"]["conversations"] == 1


def test_antigravity_does_not_infer_workspace_from_subagent_or_tool_paths(isolated_env: Path) -> None:
    brain_root = isolated_env / "ag_brain"

    # Subagent workspace metadata embedded in content does not identify this session's cwd.
    s1_dir = brain_root / "ag-uri-session" / ".system_generated" / "logs"
    s1_dir.mkdir(parents=True)
    (s1_dir / "transcript.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 1,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-12T01:00:00Z",
                        "content": "<USER_REQUEST>制作中文版宣传图</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "created_at": "2026-11-12T01:01:00Z",
                        "content": json.dumps(
                            {
                                "conversationId": "sub-1",
                                "workspaceUris": ["file:///Users/alice/Desktop/%E6%94%B9%E9%80%A0"],
                            }
                        ),
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-12T01:02:00Z",
                        "content": "已完成中文版替换。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Session 2: PLANNER_RESPONSE tool_calls touching both ~/.gemini artifact and real Desktop project
    s2_dir = brain_root / "ag-tool-session" / ".system_generated" / "logs"
    s2_dir.mkdir(parents=True)
    (s2_dir / "transcript.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 1,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-12T02:00:00Z",
                        "content": "<USER_REQUEST>审阅搜索工作流报告</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "step_index": 2,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-12T02:01:00Z",
                        "content": "已读取调查台账。",
                        "tool_calls": [
                            {
                                "name": "write_file",
                                "args": {"Path": "/Users/alice/.gemini/antigravity/brain/ag-tool-session/task.md"},
                            },
                            {
                                "name": "read_file",
                                "args": {"Path": "/Users/alice/Desktop/搜索工作流/investigations/inv_01/report.md"},
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Session 3: Pure Q&A with zero workspace files
    s3_dir = brain_root / "ag-pure-qa" / ".system_generated" / "logs"
    s3_dir.mkdir(parents=True)
    (s3_dir / "transcript.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "step_index": 1,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-12T03:00:00Z",
                        "content": "<USER_REQUEST>解释一下 WAL 模式</USER_REQUEST>",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "step_index": 2,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-12T03:01:00Z",
                        "content": "WAL 是预写式日志。",
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    sessions = read_sessions("antigravity", [str(brain_root)])
    by_ext = {s["external_id"]: s for s in sessions}
    assert by_ext["antigravity:ag-uri-session"]["cwd"] == ""
    assert by_ext["antigravity:ag-tool-session"]["cwd"] == ""
    assert by_ext["antigravity:ag-pure-qa"]["cwd"] == ""


def test_codex_sqlite_budgeted_sync_and_companion_pairing(isolated_env: Path) -> None:
    import threading
    from app.services.sync_budget import SyncBudget

    codex_dir = isolated_env / "codex_budget"
    codex_dir.mkdir(parents=True)
    state_db = codex_dir / "state_5.sqlite"
    hist_db = codex_dir / "thread_history_1.sqlite"

    with sqlite3.connect(state_db) as sc:
        sc.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                created_at INTEGER,
                updated_at INTEGER,
                source TEXT,
                cwd TEXT,
                title TEXT,
                first_user_message TEXT,
                created_at_ms INTEGER,
                updated_at_ms INTEGER,
                thread_source TEXT,
                name TEXT,
                attached_thread_parent_id TEXT
            )
            """
        )
        sc.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "019f0000-0000-7000-8000-000000000101",
                1794460000,
                1794460100,
                "vscode",
                "/Users/alice/Desktop/改造",
                "第一轮任务",
                "第一轮任务",
                1794460000000,
                1794460100000,
                "user",
                "",
                None,
            ),
        )

    with sqlite3.connect(hist_db) as hc:
        hc.execute(
            """
            CREATE TABLE thread_items (
                thread_id TEXT,
                turn_id TEXT,
                item_id TEXT,
                rollout_ordinal INTEGER,
                created_at_ms INTEGER,
                completed_at_ms INTEGER,
                item_type TEXT,
                item_json TEXT
            )
            """
        )
        hc.execute(
            "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "019f0000-0000-7000-8000-000000000101",
                "t1",
                "u1",
                1,
                1794460001000,
                1794460001000,
                "userMessage",
                json.dumps({"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "第一轮对话"}]}),
            ),
        )

    connection_service.patch_connection(
        "codex",
        enabled=True,
        paused=False,
        paths=[str(state_db), str(hist_db)],
        confirmed=True,
    )
    b1 = SyncBudget(threading.Event(), duty_cycle=1)
    b1.max_sources = 4
    res1 = connection_service.sync_connection("codex", force=False, budget=b1)
    assert res1["pending_sources"] == 0
    convs1 = conversation_service.list_conversations(agent="codex")
    assert len(convs1) == 1
    assert len(convs1[0]["messages"]) == 1

    # Append a reply ONLY to thread_history_1.sqlite (state_5.sqlite unchanged on disk)
    with sqlite3.connect(hist_db) as hc:
        hc.execute(
            "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "019f0000-0000-7000-8000-000000000101",
                "t1",
                "a1",
                2,
                1794460050000,
                1794460050000,
                "agentMessage",
                json.dumps({"id": "a1", "type": "agentMessage", "text": "增量同步仍保留 state_5 的项目目录与标题"}),
            ),
        )
    b2 = SyncBudget(threading.Event(), duty_cycle=1)
    b2.max_sources = 4
    connection_service.sync_connection("codex", force=False, budget=b2)
    convs2 = conversation_service.list_conversations(agent="codex")
    assert len(convs2) == 1
    assert convs2[0]["title"] == "第一轮任务"
    assert convs2[0]["cwd"] == "/Users/alice/Desktop/改造"
    assert len(convs2[0]["messages"]) == 2


def test_manual_projects_survive_startup_and_repair_and_merge_migrates_working_tasks(isolated_env: Path) -> None:
    from fastapi.testclient import TestClient
    from app.main import app

    desktop_dir = isolated_env / "Desktop"
    desktop_dir.mkdir(parents=True, exist_ok=True)

    manual_described = project_service.create_project(
        name="Curated Desktop Workspace",
        description="Created intentionally by user",
        directories=[str(desktop_dir)],
    )
    manual_rest_like = project_service.create_project(
        name="Desktop",
        directories=[str(desktop_dir)],
        manual=True,
    )

    with TestClient(app):
        pass

    report = project_service.repair_misclassified_projects()
    assert report["archived_blackholes"] == 0
    after_described = project_service.get_project(manual_described["id"])
    after_rest_like = project_service.get_project(manual_rest_like["id"])
    assert after_described["status"] == "active"
    assert str(desktop_dir) in after_described["directories"]
    assert after_rest_like["status"] == "active"
    assert str(desktop_dir) in after_rest_like["directories"]

    src_proj = project_service.create_project(name="Source Feature")
    dst_proj = project_service.create_project(name="Target Feature")
    task = working_memory_service.upsert_working_task(
        project_id=src_proj["id"],
        title="Track active migration work",
        goal="Ensure merge moves working tasks",
        execution_mode="discussion_only",
    )
    merged = project_service.merge_projects([src_proj["id"]], dst_proj["id"])
    assert merged["counts"]["tasks"] == 1
    moved_task = working_memory_service.get_working_task(task["id"])
    assert moved_task["project_id"] == dst_proj["id"]
    assert working_memory_service.list_working_tasks(dst_proj["id"], active_only=True)[0]["id"] == task["id"]
