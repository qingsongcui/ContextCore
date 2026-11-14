"""Comprehensive 1:1 regression suite for the 12 audit findings (and identical twin defects)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import mcp_server
from app.adapters import antigravity, claude_code, opencode
from app.adapters.base import AgentAdapterError
from app.database import create_schema, db
from app.main import app
from app.security import authenticated_agent
from app.services import (
    conflict_service,
    connection_service,
    content_service,
    conversation_service,
    distillation_service,
    governance_guardrails,
    handoff_service,
    history_watcher,
    project_service,
    working_memory_service,
)


def _snapshot_project_tables(conn: sqlite3.Connection) -> dict[str, list[tuple]]:
    return {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
        for table in ("projects", "project_directories", "conversations", "contents")
    }


def _client_with_csrf(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
    csrf = client.get("/api/session").json()["csrf_token"]
    return client, {"X-CSRF-Token": csrf}


def _write_transcript(path: Path, cwd: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "timestamp": "2026-06-01T10:00:00Z",
        "cwd": str(cwd),
        "message": {"role": "user", "content": f"Working inside {cwd}"},
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Bug #1: Host path authorization, validate_path, and validate_file_safety host
# ---------------------------------------------------------------------------


def test_bug1_host_path_boundary_and_adapter_host_type(tmp_path: Path):
    create_schema()

    auth_root = tmp_path / "authorized-antigravity"
    outside_root = tmp_path / "unauthorized-secret"
    proj_a_dir = tmp_path / "repo-alpha"
    proj_b_dir = tmp_path / "repo-beta"
    for d in (auth_root, outside_root, proj_a_dir, proj_b_dir):
        d.mkdir(parents=True, exist_ok=True)

    proj_a = project_service.create_project("Alpha", directories=[str(proj_a_dir)])
    proj_b = project_service.create_project("Beta", directories=[str(proj_b_dir)])

    auth_transcript = auth_root / "conv-auth.jsonl"
    outside_transcript = outside_root / "conv-secret.jsonl"
    _write_transcript(auth_transcript, proj_b_dir)
    _write_transcript(outside_transcript, proj_b_dir)

    with db() as conn:
        # Initially both antigravity conversations lack cwd/project binding while transcripts point to proj_b_dir.
        for cid, spath in (("ag-auth", auth_transcript), ("ag-out", outside_transcript)):
            conn.execute(
                """
                INSERT INTO conversations (
                    id, external_id, project_id, agent, title, cwd, source_path,
                    started_at, updated_at, messages, created_at
                ) VALUES (?, ?, NULL, 'antigravity', ?, '', ?, ?, ?, '[]', ?)
                """,
                (
                    cid,
                    cid,
                    cid,
                    str(spath),
                    "2026-06-01T10:00:00Z",
                    "2026-06-01T10:00:00Z",
                    "2026-06-01T10:00:00Z",
                ),
            )
        # Disabled antigravity connection must not repair either conversation.
        conn.execute(
            """
            INSERT OR REPLACE INTO connections (id, label, discovered, enabled, paused, paths, capabilities, created_at, updated_at)
            VALUES ('antigravity', 'Antigravity', 1, 0, 0, ?, '{}', ?, ?)
            """,
            (json.dumps([str(auth_root)]), "2026-06-01T10:00:00Z", "2026-06-01T10:00:00Z"),
        )

    res_disabled = project_service.repair_misclassified_projects()
    assert res_disabled["backfilled_cwd_count"] == 0
    assert res_disabled["repaired_conversations"] == 0

    with db() as conn:
        conn.execute("UPDATE connections SET enabled = 1, paused = 1 WHERE id = 'antigravity'")
    res_paused = project_service.repair_misclassified_projects()
    assert res_paused["backfilled_cwd_count"] == 0
    assert res_paused["repaired_conversations"] == 0

    # Enable & unpause: authorized transcript is backfilled & repaired to proj_b, outside transcript stays untouched.
    with db() as conn:
        conn.execute("UPDATE connections SET enabled = 1, paused = 0 WHERE id = 'antigravity'")
    res_enabled = project_service.repair_misclassified_projects()
    assert res_enabled["backfilled_cwd_count"] == 1
    assert res_enabled["repaired_conversations"] == 1

    with db() as conn:
        rows = {
            r["id"]: r["project_id"]
            for r in conn.execute("SELECT id, project_id FROM conversations WHERE id IN ('ag-auth', 'ag-out')").fetchall()
        }
    assert rows["ag-auth"] == proj_b["id"]
    assert rows["ag-out"] is None

    # Direct infer_transcript_cwd with roots also blocks outside paths.
    assert antigravity.infer_transcript_cwd(outside_transcript, roots=[auth_root]) == ""
    assert Path(antigravity.infer_transcript_cwd(auth_transcript, roots=[auth_root])).resolve() == proj_b_dir.resolve()

    # OpenCodeAdapter & ClaudeCodeAdapter twin checks: outside db/jsonl raises AgentAdapterError with str host!
    outside_db = outside_root / "opencode.db"
    sqlite3.connect(outside_db).close()
    with pytest.raises(AgentAdapterError) as oc_exc:
        list(opencode._iter_database_sessions(outside_db, [auth_root]))
    assert isinstance(oc_exc.value.host, str)
    assert oc_exc.value.host == "opencode"
    assert oc_exc.value.code in {"root_not_granted", "symlink_escape"}

    outside_cc = outside_root / "claude.jsonl"
    _write_transcript(outside_cc, proj_a_dir)
    with pytest.raises(AgentAdapterError) as cc_exc:
        claude_code._parse_file(outside_cc, [auth_root])
    assert isinstance(cc_exc.value.host, str)
    assert cc_exc.value.host == "claude-code"
    assert cc_exc.value.code in {"root_not_granted", "symlink_escape"}


# ---------------------------------------------------------------------------
# Bug #2: MCP resolve_conflict confirmation gate & metadata preservation
# ---------------------------------------------------------------------------


def test_bug2_mcp_resolve_conflict_gate_actor_and_metadata_preservation():
    create_schema()
    proj = project_service.create_project("Conflict Meta")
    existing = content_service.create_content(
        title="Deploy strategy",
        content="Deploy via canary first",
        project_id=proj["id"],
        category="lesson",
    )

    c_replace = conflict_service.create_conflict(
        proj["id"],
        existing["id"],
        {
            "title": "Deploy strategy",
            "content": "Run migration check before canary",
            "category": "lesson",
            "tags": ["deploy"],
            "applicability": {"env": "staging", "service": "api"},
            "origin_authority": "task_lesson_promoted",
            "pinned": True,
            "promoted_from_task_id": "task-staging-1",
        },
        "Conflicting staging deployment lesson",
    )
    c_both = conflict_service.create_conflict(
        proj["id"],
        existing["id"],
        {
            "title": "Rollback rule",
            "content": "Drain traffic for 60s before rollback",
            "category": "lesson",
            "tags": ["rollback"],
            "applicability": {"env": "prod"},
            "origin_authority": "task_lesson_promoted",
            "pinned": True,
            "promoted_from_task_id": "task-prod-2",
        },
        "Additional prod rollback lesson",
    )

    token = authenticated_agent.set("codex")
    try:
        # Must refuse without confirmed=True, leaving conflict pending & zero audit writes.
        blocked = mcp_server.resolve_conflict(
            conflict_id=c_replace["id"],
            resolution="replace",
            expected_version=1,
            confirmed=False,
        )
        assert blocked["ok"] is False
        assert conflict_service.get_conflict(c_replace["id"])["status"] == "pending"
        with db() as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE target = ? AND action LIKE 'conflict.resolve%'",
                    (c_replace["id"],),
                ).fetchone()[0]
                == 0
            )

        # Confirm replace via MCP -> records caller actor ('codex') and preserves all metadata fields.
        ok_res = mcp_server.resolve_conflict(
            conflict_id=c_replace["id"],
            resolution="replace",
            expected_version=1,
            confirmed=True,
        )
        assert ok_res["ok"] is True

        updated = content_service.get_content(existing["id"])
        assert updated["applicability"] == {"env": "staging", "service": "api"}
        assert updated["origin_authority"] == "task_lesson_promoted"
        assert "pinned" in updated["tags"]
        assert updated["pinned"] is True
        assert updated["promoted_from_task_id"] == "task-staging-1"

        with db() as conn:
            audit = conn.execute(
                "SELECT actor, action FROM audit_logs WHERE target = ? ORDER BY created_at DESC LIMIT 1",
                (c_replace["id"],),
            ).fetchone()
            assert audit is not None
            assert audit["actor"] == "codex"

        # Confirm 'both' resolution also preserves proposed metadata on new item.
        both_res = mcp_server.resolve_conflict(
            conflict_id=c_both["id"],
            resolution="both",
            confirmed=True,
        )
        assert both_res["ok"] is True
    finally:
        authenticated_agent.reset(token)

    all_items = content_service.list_content(project_id=proj["id"], status="active")
    created_both = [it for it in all_items if it["title"] == "Rollback rule"]
    assert len(created_both) == 1
    assert created_both[0]["applicability"] == {"env": "prod"}
    assert created_both[0]["origin_authority"] == "task_lesson_promoted"
    assert created_both[0]["promoted_from_task_id"] == "task-prod-2"
    assert "pinned" in created_both[0]["tags"]


# ---------------------------------------------------------------------------
# Bug #3: _move_content updates canonical_key & respects memory tombstones; no [:1024] slice
# ---------------------------------------------------------------------------


def test_bug3_move_content_canonical_key_tombstone_and_full_text_key():
    create_schema()
    proj_a = project_service.create_project("Move Source")
    proj_b = project_service.create_project("Move Target")

    long_body = ("Shared prefix " * 90) + "TAIL-MARKER"
    m1 = content_service.create_content(
        title="Long policy 1",
        content=long_body,
        project_id=proj_a["id"],
        category="knowledge",
    )
    content_service.batch_content([m1["id"]], action="recategorize", category="decision")
    expected_source_key = governance_guardrails.canonical_memory_key(
        category="decision",
        title="Long policy 1",
        content=long_body,
        project_id=proj_a["id"],
    )
    assert content_service.get_content(m1["id"])["canonical_key"] == expected_source_key

    # Merging proj_a into proj_b moves m1 and must rewrite canonical_key to proj_b's scope!
    project_service.merge_projects([proj_a["id"]], proj_b["id"])
    moved = content_service.get_content(m1["id"])
    assert moved["project_id"] == proj_b["id"]
    assert moved["canonical_key"] == governance_guardrails.canonical_memory_key(
        category="decision",
        title="Long policy 1",
        content=long_body,
        project_id=proj_b["id"],
    )

    # Now create a deleted-memory tombstone in proj_b, and a matching memory in a new proj_c.
    doomed_title = "Forbidden secret rule"
    doomed_body = "Never log raw JWTs in plain stdout"
    in_b = content_service.create_content(
        title=doomed_title,
        content=doomed_body,
        project_id=proj_b["id"],
        category="decision",
    )
    content_service.delete_content(in_b["id"], expected_version=1)

    proj_c = project_service.create_project("Resurrection Source")
    in_c = content_service.create_content(
        title=doomed_title,
        content=doomed_body,
        project_id=proj_c["id"],
        category="decision",
    )

    # Moving in_c into proj_b via merge_projects must be blocked with 410 (no tombstone resurrection).
    with pytest.raises(HTTPException) as exc:
        project_service.merge_projects([proj_c["id"]], proj_b["id"])
    assert exc.value.status_code == 410
    # Rollback ensures proj_c & in_c stay intact.
    assert content_service.get_content(in_c["id"])["project_id"] == proj_c["id"]


# ---------------------------------------------------------------------------
# Bug #4: split_project rejects foreign or arbitrary directories cleanly
# ---------------------------------------------------------------------------


def test_bug4_split_project_rejects_foreign_and_arbitrary_directories(tmp_path: Path):
    create_schema()
    dir_src = tmp_path / "src-dir"
    dir_other = tmp_path / "other-dir"
    dir_outside = tmp_path / "outside-dir"
    for d in (dir_src, dir_other, dir_outside):
        d.mkdir()

    proj_src = project_service.create_project("SplitSource", directories=[str(dir_src)])
    proj_other = project_service.create_project("OtherProj", directories=[str(dir_other)])

    with db() as conn:
        before = _snapshot_project_tables(conn)

    for bad_dirs, expected_codes in (
        ([str(dir_other)], {404, 422}),
        ([str(dir_outside)], {404, 422}),
        (["   "], {422}),
    ):
        with pytest.raises(HTTPException) as exc:
            project_service.split_project(proj_src["id"], "BadSplit", directories=bad_dirs)
        assert exc.value.status_code in expected_codes
        with db() as conn:
            assert _snapshot_project_tables(conn) == before

    # Valid directory owned by proj_src succeeds and transfers ownership.
    split_res = project_service.split_project(proj_src["id"], "ValidSplit", directories=[str(dir_src)])
    new_pid = split_res["id"]
    assert str(dir_src.resolve()) in [str(Path(d).resolve()) for d in split_res["directories"]]
    assert project_service.get_project(proj_src["id"])["directories"] == []
    assert proj_other["id"] != new_pid


# ---------------------------------------------------------------------------
# Bug #5: Codex SQLite WAL & SHM files included in _source_fingerprint
# ---------------------------------------------------------------------------


def test_bug5_codex_wal_shm_fingerprint_detects_incremental_writes(tmp_path: Path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    main_db = codex_dir / "state_5.sqlite"
    wal_db = codex_dir / "state_5.sqlite-wal"
    shm_db = codex_dir / "state_5.sqlite-shm"
    th_wal = codex_dir / "thread_history_1.sqlite-wal"

    main_db.write_bytes(b"SQLite format 3\x00")
    wal_db.write_bytes(b"wal-frame-1")
    shm_db.write_bytes(b"shm-1")

    fp1 = connection_service._source_fingerprint("codex", [str(codex_dir)])

    # Append to WAL only; main sqlite stays byte-identical -> fingerprint must change!
    wal_db.write_bytes(b"wal-frame-1-plus-new-session-data")
    fp2 = connection_service._source_fingerprint("codex", [str(codex_dir)])
    assert fp2 != fp1

    th_wal.write_bytes(b"thread-history-wal-1")
    fp3 = connection_service._source_fingerprint("codex", [str(codex_dir)])
    assert fp3 != fp2


# ---------------------------------------------------------------------------
# Bug #6: history_watcher filters noisy logs/cache while preserving valid sources
# ---------------------------------------------------------------------------


def test_bug6_history_watcher_filters_noise_files(tmp_path: Path):
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    codex_db = codex_dir / "state_5.sqlite"
    codex_wal = codex_dir / "state_5.sqlite-wal"
    other_sqlite = codex_dir / "unrelated.sqlite"
    codex_log = codex_dir / "log" / "codex-tui.log"
    ag_dir = tmp_path / ".antigravity"
    ag_transcript = ag_dir / ".system_generated" / "logs" / "transcript.jsonl"
    ag_cache = ag_dir / "CachedData" / "bundle.js"
    ag_debug_log = ag_dir / "logs" / "renderer.log"

    for p in (codex_db, codex_wal, other_sqlite, codex_log, ag_transcript, ag_cache, ag_debug_log):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    file_targets = {str(codex_db.resolve())}
    dir_roots = (str(ag_dir.resolve()),)
    watch_filter = history_watcher._build_watch_filter(file_targets, dir_roots)

    assert watch_filter(1, str(codex_db)) is True
    assert watch_filter(1, str(codex_wal)) is True
    assert watch_filter(1, str(other_sqlite)) is False
    assert watch_filter(1, str(codex_log)) is False
    assert watch_filter(1, str(ag_transcript)) is True
    assert watch_filter(1, str(ag_cache)) is False
    assert watch_filter(1, str(ag_debug_log)) is False


# ---------------------------------------------------------------------------
# Bug #7: MCP get_project_brief task precedence & tombstone/status filtering
# ---------------------------------------------------------------------------


def test_bug7_mcp_get_project_brief_task_filtering():
    create_schema()
    proj = project_service.create_project("Brief Tasks")

    # L1: active mirrored working_task -> should show working_task (in_progress), NOT duplicate legacy row.
    l1 = content_service.create_content(title="Mirrored Active", content="Step 1", project_id=proj["id"], category="task")
    # L2: mirrored working_task that is completed -> stale active legacy row L2 must NOT show up!
    l2 = content_service.create_content(title="Mirrored Done", content="Done", project_id=proj["id"], category="task")
    # L3: unmirrored active legacy task tagged status:completed -> must NOT show up!
    l3 = content_service.create_content(
        title="Tagged Done Legacy",
        content="Finished earlier",
        project_id=proj["id"],
        category="task",
        tags=["status:completed"],
    )
    # L4: unmirrored active legacy task with an entity_type='memory' tombstone -> must NOT show up!
    l4 = content_service.create_content(title="Tombstoned Legacy", content="Removed", project_id=proj["id"], category="task")
    # L5: genuine unmirrored active legacy task -> MUST show up!
    l5 = content_service.create_content(title="Truly Active Legacy", content="Do it", project_id=proj["id"], category="task")

    # Note: legacy l3/l4/l5 are excluded or tested separately; prevent ensure_legacy_tasks_synced
    # from auto-mirroring l5 by inserting l5 AFTER initial sync or verifying working_task mirroring directly:
    working_memory_service.ensure_legacy_tasks_synced(proj["id"])
    with db() as conn:
        wt1_id = conn.execute(
            "SELECT id FROM working_tasks WHERE legacy_content_id = ?",
            (l1["id"],),
        ).fetchone()["id"]
        wt2_id = conn.execute(
            "SELECT id FROM working_tasks WHERE legacy_content_id = ?",
            (l2["id"],),
        ).fetchone()["id"]
        conn.execute(
            "UPDATE working_tasks SET title = 'Mirrored Active Updated', status = 'in_progress' WHERE id = ?",
            (wt1_id,),
        )
        conn.execute(
            "UPDATE working_tasks SET title = 'Mirrored Done Updated', status = 'completed' WHERE id = ?",
            (wt2_id,),
        )
        # Keep l5 unmirrored by deleting its mirrored working_task if auto-synced, and simulate a true unmirrored legacy task:
        conn.execute("DELETE FROM working_tasks WHERE legacy_content_id IN (?, ?, ?)", (l3["id"], l4["id"], l5["id"]))
        conn.execute(
            """
            INSERT INTO tombstones (id, entity_type, dedupe_key, scope_project_id, deleted_at, deleted_by, reason)
            VALUES ('tmb-l4', 'memory', ?, ?, '2026-06-01T10:00:00Z', 'user', 'deleted')
            """,
            (f"legacy_task:{l4['id']}", proj["id"]),
        )

    # Note: list_working_tasks inside get_project_brief calls ensure_legacy_tasks_synced;
    # here keep ensure_legacy_tasks_synced from remirroring unmirrored legacy rows l3/l4/l5
    # by patching updated_at? Better yet, test unmirrored legacy filtering cleanly when sync doesn't recreate l3..l5,
    # OR let ensure_legacy_tasks_synced respect tombstones/tags too! Wait: let's patch ensure_legacy_tasks_synced no-op for the unmirrored test slice.
    orig_sync = working_memory_service.ensure_legacy_tasks_synced
    working_memory_service.ensure_legacy_tasks_synced = lambda *args, **kwargs: None
    try:
        brief = mcp_server.get_project_brief(proj["id"])
    finally:
        working_memory_service.ensure_legacy_tasks_synced = orig_sync

    returned_ids = {t["id"] for t in brief["tasks"]}
    assert wt1_id in returned_ids
    assert l5["id"] in returned_ids
    for forbidden in (l1["id"], l2["id"], wt2_id, l3["id"], l4["id"]):
        assert forbidden not in returned_ids


# ---------------------------------------------------------------------------
# Bug #8: consolidate_memories sources + all retirement paths write content_versions
# ---------------------------------------------------------------------------


def test_bug8_consolidate_sources_and_version_snapshots_across_retirements():
    create_schema()
    proj = project_service.create_project("Versioning")

    # 1. Consolidate near-identical memories with distinct content_sources.
    m1 = content_service.create_content(
        title="Pytest tmp_path rule",
        content="Always isolate tests with tmp_path in pytest test suites.",
        project_id=proj["id"],
        category="knowledge",
        source_id="conv-src-1",
        source_kind="conversation",
        quote="Quote from conversation 1",
    )
    m2 = content_service.create_content(
        title="Pytest tmp_path rule",
        content="Always isolate tests with tmp_path in pytest test suites!",
        project_id=proj["id"],
        category="knowledge",
        source_id="conv-src-2",
        source_kind="conversation",
        quote="Quote from conversation 2",
    )
    # Give m2 newer updated_at so m2 wins primary and m1 becomes historical duplicate.
    with db() as conn:
        conn.execute("UPDATE contents SET updated_at = '2026-06-02T10:00:00Z' WHERE id = ?", (m2["id"],))
        conn.execute("UPDATE contents SET updated_at = '2026-06-01T10:00:00Z' WHERE id = ?", (m1["id"],))

    con_res = content_service.consolidate_memories(project_id=proj["id"], scope="project")
    assert con_res["merged_groups"] >= 1

    with db() as conn:
        src_rows = conn.execute(
            "SELECT source_id, quote FROM content_sources WHERE content_id = ?",
            (m2["id"],),
        ).fetchall()
        m1_row = conn.execute("SELECT status, version FROM contents WHERE id = ?", (m1["id"],)).fetchone()
    assert {r["source_id"] for r in src_rows} == {"conv-src-1", "conv-src-2"}
    assert m1_row["status"] == "historical"
    snap_con = content_service.get_content_version(m1["id"], m1_row["version"])
    assert snap_con["version"] == m1_row["version"]

    # 2. supersede in create_content writes content_versions for the retired item's new version.
    sup_old = content_service.create_content(
        title="Old Auth",
        content="Use session cookies",
        project_id=proj["id"],
        category="decision",
    )
    content_service.create_content(
        title="New Auth",
        content="Use bearer tokens",
        project_id=proj["id"],
        category="decision",
        supersedes_id=sup_old["id"],
    )
    with db() as conn:
        sup_row = conn.execute("SELECT status, version FROM contents WHERE id = ?", (sup_old["id"],)).fetchone()
    assert sup_row["status"] == "historical"
    assert content_service.get_content_version(sup_old["id"], sup_row["version"])["version"] == sup_row["version"]

    # 3. correct_content increments & stores retired version snapshot.
    corr_orig = content_service.create_content(
        title="Timeout setting",
        content="Timeout is 10s",
        project_id=proj["id"],
        category="knowledge",
    )
    corr_res = content_service.correct_content(
        corr_orig["id"],
        content="Timeout is 30s",
        expected_version=1,
    )
    with db() as conn:
        old_corr_row = conn.execute("SELECT status, version FROM contents WHERE id = ?", (corr_orig["id"],)).fetchone()
    assert old_corr_row["status"] == "historical"
    assert content_service.get_content_version(corr_orig["id"], old_corr_row["version"])["version"] == old_corr_row["version"]
    assert corr_res["version"] == 1

    # 4. merge_content stores retired source version snapshots.
    mg_a = content_service.create_content(title="Rule A", content="Use uv", project_id=proj["id"], category="preference")
    mg_b = content_service.create_content(title="Rule B", content="Pin deps", project_id=proj["id"], category="preference")
    content_service.merge_content(
        ids=[mg_a["id"], mg_b["id"]],
        title="Combined dep rule",
        content="Use uv and pin deps",
        project_id=proj["id"],
        expected_versions={mg_a["id"]: 1, mg_b["id"]: 1},
    )
    with db() as conn:
        for sid in (mg_a["id"], mg_b["id"]):
            r = conn.execute("SELECT status, version FROM contents WHERE id = ?", (sid,)).fetchone()
            assert r["status"] == "historical"
            assert content_service.get_content_version(sid, r["version"])["version"] == r["version"]

    # 5. Twin: distillation_service._retire_and_replace stores retired version snapshot.
    dist_old = content_service.create_content(
        title="Old Build Cmd",
        content="npm run legacy-build",
        project_id=proj["id"],
        category="knowledge",
    )
    with db() as conn:
        distillation_service._retire_and_replace(
            conn=conn,
            project_id=proj["id"],
            old_id=dist_old["id"],
            category="knowledge",
            title="New Build Cmd",
            content="npm run build",
            tags=["build"],
            actor="distiller",
            quote="Use npm run build now",
            conversation_id=None,
            conversation_title="Build talk",
            source_agent="codex",
            now="2026-06-03T10:00:00Z",
        )
        dist_row = conn.execute("SELECT status, version FROM contents WHERE id = ?", (dist_old["id"],)).fetchone()
    assert dist_row["status"] == "historical"
    assert content_service.get_content_version(dist_old["id"], dist_row["version"])["version"] == dist_row["version"]


# ---------------------------------------------------------------------------
# Bug #9: Epoch-ms comparison prevents timezone/format inversion
# ---------------------------------------------------------------------------


def test_bug9_epoch_ms_iso_timestamp_comparison():
    create_schema()

    # Working task sync: legacy updated at 12:00Z is NEWER than working updated at 13:00+02:00 (=11:00Z),
    # even though "2026-06-01T13:00:00+02:00" > "2026-06-01T12:00:00Z" in string order!
    proj = project_service.create_project("TZ Sync")
    leg = content_service.create_content(
        title="Legacy Fresh",
        content="Updated in Z timezone",
        project_id=proj["id"],
        category="task",
    )
    working_memory_service.ensure_legacy_tasks_synced(proj["id"])
    wt = working_memory_service.list_working_tasks(project_id=proj["id"], active_only=False)[0]

    with db() as conn:
        conn.execute(
            "UPDATE contents SET title = 'Legacy Newer Title', updated_at = '2026-06-01T12:00:00Z' WHERE id = ?",
            (leg["id"],),
        )
        conn.execute(
            "UPDATE working_tasks SET updated_at = '2026-06-01T13:00:00+02:00' WHERE id = ?",
            (wt["id"],),
        )
    working_memory_service.ensure_legacy_tasks_synced(proj["id"])
    wt_after = working_memory_service.get_working_task(wt["id"])
    assert wt_after["title"] == "Legacy Newer Title"

    # Discovery sort: project with 12:30:00.500000Z must rank BEFORE project with 13:00:00+02:00 (=11:00:00Z).
    proj_older_offset = project_service.create_project("OffsetOlder", activity_at="2026-06-01T13:00:00+02:00")
    proj_newer_utc = project_service.create_project("UtcNewer", activity_at="2026-06-01T12:30:00.500000Z")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, project_id, agent, title, started_at, updated_at, messages, created_at)
            VALUES
              ('conv-old', ?, 'codex', 'Offset conv', '2026-06-01T13:00:00+02:00', '2026-06-01T13:00:00+02:00', '[]', '2026-06-01T13:00:00+02:00'),
              ('conv-new', ?, 'codex', 'UTC conv', '2026-06-01T12:30:00.500000Z', '2026-06-01T12:30:00.500000Z', '[]', '2026-06-01T12:30:00.500000Z')
            """,
            (proj_older_offset["id"], proj_newer_utc["id"]),
        )
        conn.execute("UPDATE projects SET updated_at = '2026-06-01T13:00:00+02:00' WHERE id = ?", (proj_older_offset["id"],))
        conn.execute("UPDATE projects SET updated_at = '2026-06-01T12:30:00.500000Z' WHERE id = ?", (proj_newer_utc["id"],))

    disc = handoff_service.discover_relay_projects(consumer="codex", sync_first=False)
    ids_in_order = [c["project_id"] for c in disc["candidates"] if c["project_id"] in {proj_older_offset["id"], proj_newer_utc["id"]}]
    assert ids_in_order == [proj_newer_utc["id"], proj_older_offset["id"]]


# ---------------------------------------------------------------------------
# Bug #10: Memory counts & relay exclude legacy category='task'
# ---------------------------------------------------------------------------


def test_bug10_memory_counts_and_relay_exclude_legacy_tasks(monkeypatch: pytest.MonkeyPatch):
    create_schema()
    proj = project_service.create_project("Count Scope")
    real_mem = content_service.create_content(
        title="Real Memory",
        content="Use PostgreSQL 16",
        project_id=proj["id"],
        category="decision",
    )
    legacy_task = content_service.create_content(
        title="Legacy Task Row",
        content="Old task record",
        project_id=proj["id"],
        category="task",
    )

    conversation_service.upsert_conversation(
        agent="claude-code",
        project_id=proj["id"],
        title="Init DB",
        external_id="cc:cnt-1",
        messages=[
            {"role": "user", "content": "Setup DB"},
            {"role": "assistant", "content": "Using PG 16"},
        ],
    )

    assert project_service.get_project(proj["id"])["counts"]["memories"] == 1

    client, _ = _client_with_csrf(monkeypatch)
    status_res = client.get("/api/status").json()
    assert status_res["counts"]["memories"] == 1

    disc = handoff_service.discover_relay_projects(consumer="codex", sync_first=False)
    cand = next(c for c in disc["candidates"] if c["project_id"] == proj["id"])
    assert cand["memory_count"] == 1

    relay_pkg = handoff_service.relay_project(
        proj["id"],
        consumer="codex",
        context_mode="full",
        max_tokens=32000,
        sync_first=False,
    )
    assert relay_pkg["status"] == "relayed"
    relay_mem_ids = {m["id"] for m in relay_pkg["context"]["memories"]}
    assert real_mem["id"] in relay_mem_ids
    assert legacy_task["id"] not in relay_mem_ids


# ---------------------------------------------------------------------------
# Bug #11: Global memory merge keeps project_id=None and shared/selected category
# ---------------------------------------------------------------------------


def test_bug11_merge_content_preserves_global_scope_and_category(monkeypatch: pytest.MonkeyPatch):
    create_schema()
    p1 = content_service.create_content(
        title="Concise style",
        content="Prefer concise responses",
        project_id=None,
        category="preference",
        tags=["pinned", "style"],
    )
    p2 = content_service.create_content(
        title="Chinese replies",
        content="Respond in Chinese by default",
        project_id=None,
        category="preference",
        tags=["lang"],
    )

    client, headers = _client_with_csrf(monkeypatch)
    resp = client.post(
        "/api/content/merge",
        headers=headers,
        json={
            "ids": [p1["id"], p2["id"]],
            "title": "Communication Preferences",
            "content": "Respond concisely in Chinese",
            "project_id": None,
            "expected_versions": {p1["id"]: 1, p2["id"]: 1},
        },
    )
    assert resp.status_code == 200
    merged = resp.json()
    assert merged["project_id"] is None
    assert merged["category"] == "preference"
    assert "pinned" in merged["tags"]

    # Explicit category override also respected cleanly.
    p3 = content_service.create_content(
        title="Decision A",
        content="Always run typecheck",
        project_id=None,
        category="preference",
    )
    resp2 = client.post(
        "/api/content/merge",
        headers=headers,
        json={
            "ids": [merged["id"], p3["id"]],
            "title": "Team Policy",
            "content": "Respond concisely in Chinese and always typecheck",
            "project_id": None,
            "category": "decision",
            "expected_versions": {merged["id"]: merged["version"], p3["id"]: 1},
        },
    )
    assert resp2.status_code == 200
    assert resp2.json()["category"] == "decision"
    assert resp2.json()["project_id"] is None


# ---------------------------------------------------------------------------
# Bug #12 (Backend): _render_tool_pairs labels truncated vs untruncated honestly
# ---------------------------------------------------------------------------


def test_bug12_handoff_render_tool_pairs_truncation_honesty():
    short_messages = [
        {"role": "assistant", "tool_calls": [{"id": "tc-1", "name": "read_file", "arguments": '{"path":"a.py"}'}]},
        {"role": "tool", "tool_call_id": "tc-1", "content": "print('ok')"},
    ]
    short_pairs = handoff_service.assemble_tool_pairs(short_messages)
    short_rendered = handoff_service._render_tool_pairs(short_pairs, max_piece_chars=800, max_piece_bytes=800)
    assert "（未截断）" in short_rendered or "(not truncated)" in short_rendered
    assert "（节选）" not in short_rendered
    assert "(excerpt)" not in short_rendered

    long_output = "X" * 1200
    long_messages = [
        {"role": "assistant", "tool_calls": [{"id": "tc-2", "name": "run_command", "arguments": '{"cmd":"ls"}'}]},
        {"role": "tool", "tool_call_id": "tc-2", "content": long_output},
    ]
    long_pairs = handoff_service.assemble_tool_pairs(long_messages)
    long_rendered = handoff_service._render_tool_pairs(long_pairs, max_piece_chars=800, max_piece_bytes=800)
    assert "（节选）" in long_rendered or "(excerpt)" in long_rendered
    assert "（未截断）" not in long_rendered
    assert "(not truncated)" not in long_rendered


def test_discover_relay_conversations_survives_busy_sqlite_lock(monkeypatch: pytest.MonkeyPatch):
    from contextlib import contextmanager
    from app.config import settings

    create_schema()
    project = project_service.create_project("Busy Lock Relay")
    conv_old = conversation_service.upsert_conversation(
        agent="codex",
        project_id=project["id"],
        external_id="codex:busy-old",
        title="Older Chat",
        updated_at="2026-11-14T10:00:00Z",
        messages=[{"role": "user", "content": "Investigate initial lock issue"}],
    )
    # Prime relay index schema once so the read query stays pure SELECT.
    handoff_service.discover_relay_conversations(consumer="omp", project_id=project["id"], sync_first=False)

    conv_new = conversation_service.upsert_conversation(
        agent="codex",
        project_id=project["id"],
        external_id="codex:busy-new",
        title="Newer Chat",
        updated_at="2026-11-14T11:00:00Z",
        messages=[{"role": "user", "content": "Check latest fallback under SQLite writer contention"}],
    )

    @contextmanager
    def _fast_timeout_db():
        conn = sqlite3.connect(str(settings.db_path), timeout=0.05)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 50")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    monkeypatch.setattr(handoff_service, "db", _fast_timeout_db)

    locker = sqlite3.connect(str(settings.db_path), timeout=5)
    locker.execute("BEGIN IMMEDIATE")
    try:
        res = handoff_service.discover_relay_conversations(
            consumer="omp",
            project_id=project["id"],
            sync_first=False,
        )
        assert res["candidates"][0]["conversation_id"] == conv_new["id"]
        assert res["candidates"][1]["conversation_id"] == conv_old["id"]

        loaded = handoff_service.relay_conversation(
            "1",
            consumer="omp",
            project_id=project["id"],
            sync_first=False,
        )
        assert loaded["conversation_id"] == conv_new["id"]
        assert loaded["must_not_execute"] is True
    finally:
        locker.rollback()
        locker.close()


def test_bootstrap_working_memory_default_budget_accommodates_chinese_global_rules(tmp_path: Path):
    create_schema()
    repo_dir = tmp_path / "fresh-project"
    repo_dir.mkdir(parents=True, exist_ok=True)
    project_service.create_project("Fresh Project", directories=[str(repo_dir)])

    created_ids: list[str] = []
    for idx in range(4):
        body = f"全局规则 {idx}：" + ("所有接口修改后必须核对权限、版本历史与回归测试，不允许跳过验证步骤。" * 13)
        assert len(body) >= 400
        item = content_service.create_content(
            title=f"全局治理规则 {idx}",
            content=body,
            category="decision",
            project_id=None,
            tags=["pinned"],
            agent="user",
        )
        created_ids.append(item["id"])

    res = mcp_server.bootstrap_working_memory(cwd=str(repo_dir))
    assert res["status"] == "ready"
    assert res["must_not_execute"] is False
    delivered_ids = {c["id"] for c in res["applicable_constraints"]}
    assert set(created_ids) <= delivered_ids
    for c in res["applicable_constraints"]:
        if c["id"] in created_ids:
            assert len(c["content"]) >= 400


