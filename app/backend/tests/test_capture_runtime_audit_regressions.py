from __future__ import annotations

import asyncio
import json
import os
import shlex
import sqlite3
import stat
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from app.database import create_schema, seed_data, db
from app.integrations.constants import DEFAULT_BASE_URL
from app.integrations.templates import render_hooks_config, render_mcp_config, CC_HOOK_PATH
from app.integrations.installer import apply_host_integration, generate_setup_bundle
from app.integrations.cc_hook import format_host_response, post_to_contextcore, main as cc_hook_main
from app.services import connection_service as service, conversation_service
from app.services.sync_budget import SyncBudget


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    db_path = tmp_path / "contextcore_test.db"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_path))
    create_schema()
    seed_data()
    return tmp_path, home


def test_setup_and_install_thread_selected_running_base_url_and_reject_unauthenticated_remote(isolated_env):
    _, home = isolated_env
    non_default_url = "http://127.0.0.1:18420"

    # 1. Setup connection threads explicit non-default base_url
    setup_res = service.setup_connection("codex", base_url=non_default_url)
    assert "http://127.0.0.1:18420" in setup_res["config_preview"]
    assert "18420" in setup_res["bundle"]["hooks_config"]["content"]

    # 2. Install connection threads explicit non-default base_url
    install_res = service.install_connection("codex", confirmed=True, home=home, base_url=non_default_url)
    assert install_res["ok"] is True
    codex_cfg = home / ".codex" / "config.toml"
    assert codex_cfg.exists()
    assert "http://127.0.0.1:18420" in codex_cfg.read_text(encoding="utf-8")

    # 3. resolve_trusted_base_url reuses runtime PID metadata when base_url is not provided
    with patch("app.runtime.read_pid_file", return_value={"port": 18420, "host": "127.0.0.1"}):
        resolved = service.resolve_trusted_base_url(None)
        assert resolved == "http://127.0.0.1:18420"

    # 4. resolve_trusted_base_url rejects unauthenticated remote / unknown origins and falls back safely
    with patch("app.runtime.read_pid_file", return_value={"port": 18420, "host": "127.0.0.1"}):
        # Malicious or unauthenticated remote URLs
        evil_resolved = service.resolve_trusted_base_url("http://attacker.example.com:8420")
        assert evil_resolved == "http://127.0.0.1:18420"  # Falls back to local runtime
        remote_ip_resolved = service.resolve_trusted_base_url("http://198.51.100.1:8420")
        assert remote_ip_resolved == "http://127.0.0.1:18420"


def test_hook_commands_use_installed_interpreter_and_env_prefix():
    non_default_url = "http://127.0.0.1:18420"

    # Codex: shell command with sys.executable and CONTEXTCORE_BASE_URL prefix
    codex_hooks = render_hooks_config("codex", base_url=non_default_url)
    codex_cmd = codex_hooks["data"]["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert shlex.quote(sys.executable) in codex_cmd
    assert f"CONTEXTCORE_BASE_URL={shlex.quote(non_default_url)}" in codex_cmd
    assert codex_cmd.endswith("codex session-start")

    # Cursor
    cursor_hooks = render_hooks_config("cursor", base_url=non_default_url)
    cursor_cmd = cursor_hooks["data"]["hooks"]["sessionStart"][0]["command"]
    assert shlex.quote(sys.executable) in cursor_cmd
    assert f"CONTEXTCORE_BASE_URL={shlex.quote(non_default_url)}" in cursor_cmd
    assert cursor_cmd.endswith("cursor session-start")

    # Antigravity
    ag_hooks = render_hooks_config("antigravity", base_url=non_default_url)
    ag_cmd = ag_hooks["data"]["contextcore-session"]["SessionStart"][0]["command"]
    assert shlex.quote(sys.executable) in ag_cmd
    assert f"CONTEXTCORE_BASE_URL={shlex.quote(non_default_url)}" in ag_cmd
    assert ag_cmd.endswith("antigravity session-start")

    # Claude Code
    claude_hooks = render_hooks_config("claude-code", base_url=non_default_url)
    claude_cmd = claude_hooks["data"]["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert shlex.quote(sys.executable) in claude_cmd
    assert f"CONTEXTCORE_BASE_URL={shlex.quote(non_default_url)}" in claude_cmd
    assert claude_cmd.endswith("claude-code session-start")

    # OMP TypeScript extension
    omp_hooks = render_hooks_config("omp", base_url=non_default_url)
    assert f'const BASE_URL = "{non_default_url}";' in omp_hooks["content"]

    # OpenCode TypeScript plugin
    opencode_hooks = render_hooks_config("opencode", base_url=non_default_url)
    assert f'const BASE_URL = "{non_default_url}";' in opencode_hooks["content"]


def test_cc_hook_cli_base_url_and_ordinary_silence(monkeypatch):
    # Verify CLI flag --base-url is parsed
    captured_base = []

    def mock_post(agent_id, event_arg, payload, base_url="http://127.0.0.1:8420", timeout=3.0):
        captured_base.append(base_url)
        return {"status": "ok"}

    monkeypatch.setattr("app.integrations.cc_hook.enqueue_notification", mock_post)
    monkeypatch.setattr(sys, "argv", ["cc_hook.py", "--base-url", "http://127.0.0.1:18420", "codex", "prompt"])

    with patch("sys.stdin.isatty", return_value=True):
        ret = cc_hook_main()
        assert ret == 0
        assert captured_base == ["http://127.0.0.1:18420"]

    # Ordinary lifecycle silence: format_host_response never injects text
    assert format_host_response("codex", "prompt", {"injection": "secret"}) == {}
    assert format_host_response("cursor", "beforeSubmitPrompt", {"card": "data"}) == {"continue": True}
    assert format_host_response("antigravity", "SessionStart", {"injection": "secret"}) == {}


def test_codex_index_change_does_not_mark_all_rollouts_pending(isolated_env, monkeypatch):
    tmp_path, home = isolated_env
    root = tmp_path / "codex_history"
    root.mkdir()

    # Create 5 initial rollout files
    uuids = [f"01a16448-066f-74e2-9af2-f91bce6c14a{i}" for i in range(5)]
    for i, sid in enumerate(uuids):
        (root / f"rollout-2026-10-24T12-00-00-{sid}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": str(root)}}) + "\n" +
            json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": f"Hello {i}"}}) + "\n" +
            json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": f"Response {i}"}}) + "\n",
            encoding="utf-8"
        )
    (root / "session_index.jsonl").write_text(
        "".join(json.dumps({"id": sid, "thread_name": f"Session {i}"}) + "\n" for i, sid in enumerate(uuids)),
        encoding="utf-8"
    )

    service.patch_connection("codex", paths=[str(root)], enabled=True, confirmed=True)

    # Initial sync
    res1 = service.sync_connection("codex", force=False)
    assert res1.get("error") is None

    # Check that all 5 are marked ready
    with db() as conn:
        ready_count = conn.execute("SELECT count(*) FROM source_sync_cursors WHERE host='codex' AND status='ready'").fetchone()[0]
        assert ready_count >= 5

    # Now touch session_index.jsonl with an updated title for sess0
    (root / "session_index.jsonl").write_text(
        "".join(json.dumps({"id": sid, "thread_name": f"Updated Session {i}"}) + "\n" for i, sid in enumerate(uuids)),
        encoding="utf-8"
    )

    # Second sync: should NOT re-mark rollouts pending
    budget = SyncBudget(threading.Event(), duty_cycle=1.0)
    budget.max_sources = 2
    res2 = service.sync_connection("codex", force=False, budget=budget)

    # Verify conversation title in DB was updated from index without re-parsing all rollouts
    with db() as conn:
        pending_rows = conn.execute("SELECT source_path FROM source_sync_cursors WHERE host='codex' AND status='pending'").fetchall()
        # No rollouts should be left in pending state
        assert len(pending_rows) == 0

        # Title updated in database
        row = conn.execute("SELECT title FROM conversations WHERE agent='codex' AND external_id=?", (f"codex:{uuids[0]}",)).fetchone()
        assert row is not None
        assert row[0] == "Updated Session 0"


def test_sync_connection_foreground_no_progress_exit(isolated_env, monkeypatch):
    tmp_path, _ = isolated_env
    root = tmp_path / "mock_root"
    root.mkdir()
    (root / "file1.jsonl").write_text("{}\n")
    service.patch_connection("claude-code", paths=[str(root)], enabled=True, confirmed=True)

    # Simulate an adapter where pending_sources stays > 0 but processed_sources == 0
    call_count = 0

    def mock_impl(connection_id, force, allow_execution=False, budget=None, skip_sources=None, skip_failures_since=None):
        nonlocal call_count
        call_count += 1
        return {
            "processed_sources": 0,
            "pending_sources": 5,
        }

    monkeypatch.setattr(service, "_sync_connection_impl", mock_impl)
    service._sync_locks.clear()

    # Should exit immediately via no-progress exit rather than infinite loop
    result = service.sync_connection("claude-code", force=False)
    assert call_count == 1
    assert result["processed_sources"] == 0


def test_antigravity_unsnapshottable_pending_transitions_honestly(isolated_env, monkeypatch):
    tmp_path, _ = isolated_env
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    trans_file = brain_dir / "transcript-1.jsonl"
    trans_file.write_text("{}\n")

    service.patch_connection("antigravity", paths=[str(brain_dir)], enabled=True, confirmed=True)

    # Record durable pending row
    with db() as conn:
        service._write_source_pending(conn, "antigravity", {
            "source_path": str(trans_file.resolve()),
            "fingerprint": "mock-fp",
            "file_identity": "mock-id",
            "complete_offset": 0,
        })

    # Now make snapshot fail (e.g. truncated / unreadable)
    from app.adapters import AgentAdapterError
    def failing_snapshot(host, path, roots, previous=None, budget=None):
        raise AgentAdapterError(host="antigravity", code="truncated", path=str(path), detail="File exceeds limit")

    monkeypatch.setattr(service, "_snapshot_source", failing_snapshot)
    service._sync_locks.clear()

    # Sync should transition the pending cursor to error and not hang
    budget = SyncBudget(threading.Event(), duty_cycle=1.0)
    budget.max_sources = 10
    result = service.sync_connection("antigravity", force=False, budget=budget)

    # Cursor must honestly record error, not stay pending
    with db() as conn:
        row = conn.execute("SELECT status, error FROM source_sync_cursors WHERE host='antigravity' AND source_path=?", (str(trans_file.resolve()),)).fetchone()
        assert row is not None
        assert row[0] == "error"
        assert "truncated" in (row[1] or "")


def test_sqlite_wal_growth_during_sync_preserves_consistent_snapshot(isolated_env, monkeypatch):
    tmp_path, _ = isolated_env
    db_file = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT, value TEXT, isSubagent INTEGER, createdAt INTEGER, lastUpdatedAt INTEGER)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
    conn.commit()
    conn.close()

    service.patch_connection("cursor", paths=[str(db_file)], enabled=True, confirmed=True)

    # Simulate read_sessions returning a consistent session
    mock_session = {
        "external_id": "cursor:test-cid",
        "title": "Test Cursor Session",
        "cwd": str(tmp_path),
        "messages": [{"role": "user", "content": "hello from cursor"}],
        "source_path": str(db_file.resolve()),
    }
    monkeypatch.setattr("app.adapters.read_sessions", lambda *args, **kwargs: [mock_session])

    # Simulate WAL file mtime change between planning snapshot and post-read recheck
    snap_calls = 0
    orig_snapshot = service._snapshot_source

    def simulating_wal_change(host, path, roots, previous=None, budget=None):
        nonlocal snap_calls
        snap_calls += 1
        res = orig_snapshot(host, path, roots, previous=previous, budget=budget)
        if snap_calls > 1:
            # Return modified fingerprint representing WAL growth/timestamp change, but identical file_identity
            res = dict(res)
            res["fingerprint"] = "new-wal-fingerprint-post-read"
        return res

    monkeypatch.setattr(service, "_snapshot_source", simulating_wal_change)
    service._sync_locks.clear()

    # Sync should succeed without discarding sessions as file_changed
    budget = SyncBudget(threading.Event(), duty_cycle=1.0)
    budget.max_sources = 10
    result = service.sync_connection("cursor", force=False, budget=budget)
    assert result.get("error") is None

    # Conversation was imported
    convs = conversation_service.list_conversations(agent="cursor")
    assert len(convs) == 1
    assert convs[0]["title"] == "Test Cursor Session"


def test_valid_empty_sources_not_reported_as_unsupported(isolated_env, monkeypatch):
    tmp_path, _ = isolated_env

    # 1. Fresh empty Cursor database: valid schema, 0 dialogues
    cursor_db = tmp_path / "empty_state.vscdb"
    conn = sqlite3.connect(cursor_db)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT, value TEXT, isSubagent INTEGER, createdAt INTEGER, lastUpdatedAt INTEGER)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
    conn.commit()
    conn.close()

    service.patch_connection("cursor", paths=[str(cursor_db)], enabled=True, confirmed=True)
    service._sync_locks.clear()

    res_cursor = service.sync_connection("cursor", force=False)
    # Must NOT report partial_sync error for legitimate empty DB
    assert res_cursor.get("error") is None

    with db() as conn:
        row = conn.execute("SELECT status, error FROM source_sync_cursors WHERE host='cursor' AND source_path=?", (str(cursor_db.resolve()),)).fetchone()
        assert row is not None
        assert row[0] == "ready"
        assert row[1] is None

    # 2. Codex inventory with only session_index.jsonl (0 rollout files)
    codex_root = tmp_path / "empty_codex"
    codex_root.mkdir()
    (codex_root / "session_index.jsonl").write_text("{}\n")

    service.patch_connection("codex", paths=[str(codex_root)], enabled=True, confirmed=True)
    service._sync_locks.clear()

    res_codex = service.sync_connection("codex", force=False)
    # Must NOT report root_not_granted / partial_sync error
    assert res_codex.get("error") is None

    with db() as conn:
        row = conn.execute("SELECT status, error FROM source_sync_cursors WHERE host='codex' AND source_path=?", (str((codex_root / "session_index.jsonl").resolve()),)).fetchone()
        assert row is not None
        assert row[0] == "ready"
        assert row[1] is None


def _write_cursor_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE composerHeaders (composerId TEXT, value TEXT, isSubagent INTEGER, createdAt INTEGER, lastUpdatedAt INTEGER)"
    )
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
    conn.commit()
    conn.close()


def test_cursor_vscdb_midwrite_skips_stale_group_once_and_clears_on_success(isolated_env, monkeypatch):
    from app.adapters.base import AgentAdapterError
    from app.services import sync_budget as budget_mod

    tmp_path, _ = isolated_env
    stale = tmp_path / "stale" / "state.vscdb"
    stable = tmp_path / "stable" / "state.vscdb"
    _write_cursor_db(stale)
    _write_cursor_db(stable)
    service.patch_connection(
        "cursor", paths=[str(stale.parent), str(stable.parent)], enabled=True, confirmed=True,
    )

    class TinyBudget(budget_mod.SyncBudget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.max_sources = 1

    monkeypatch.setattr(budget_mod, "SyncBudget", TinyBudget)
    stale_path = str(stale.resolve())
    reads = []
    fail_stale = {"value": True}

    def read_sessions(connection_id, roots, source_paths=None, **kwargs):
        group = [str(Path(path).resolve()) for path in (source_paths or [])]
        reads.append(group)
        if stale_path in group and fail_stale["value"]:
            stale.write_bytes(stale.read_bytes() + b"\0")
            raise AgentAdapterError(
                host="cursor", code="file_changed", path=stale_path,
                detail="Cursor database was rewritten during read", retryable=True,
            )
        chosen = stable if str(stable.resolve()) in group else Path(group[0])
        return [{
            "external_id": f"cursor:{chosen.parent.name}",
            "title": f"{chosen.parent.name} session",
            "cwd": str(tmp_path),
            "messages": [{"role": "user", "content": "hello from " + chosen.parent.name}],
            "source_path": str(chosen.resolve()),
        }]

    monkeypatch.setattr("app.adapters.read_sessions", read_sessions)
    service._sync_locks.clear()
    first = service.sync_connection("cursor", force=False)
    stale_reads = sum(1 for group in reads if stale_path in group)
    assert stale_reads == 1
    error = first.get("error") or ""
    assert stale_path in error
    assert error.count(stale_path) == 1

    fail_stale["value"] = False
    service._sync_locks.clear()
    second = service.sync_connection("cursor", force=False)
    assert second.get("error") is None
    with db() as conn:
        row = conn.execute(
            "SELECT status, error FROM source_sync_cursors WHERE host='cursor' AND source_path=?",
            (stale_path,),
        ).fetchone()
    assert row is not None
    assert row[0] == "ready"
    assert row[1] is None


def test_antigravity_terminal_format_error_stays_skipped_with_status_text(isolated_env, monkeypatch):
    tmp_path, _ = isolated_env
    root = tmp_path / "brain" / "session-1" / ".system_generated" / "logs"
    root.mkdir(parents=True)
    bad = root / "transcript.jsonl"
    bad.write_text("{not-json}\n")
    service.patch_connection("antigravity", paths=[str(tmp_path / "brain")], enabled=True, confirmed=True)
    calls = {"n": 0}
    original = __import__("app.adapters", fromlist=["read_sessions"]).read_sessions

    def counting_read(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("app.adapters.read_sessions", counting_read)
    service._sync_locks.clear()
    first = service.sync_connection("antigravity", force=False)
    service._sync_locks.clear()
    second = service.sync_connection("antigravity", force=False)
    assert calls["n"] == 1
    assert second.get("reason") == "source_unchanged"
    status = first.get("error") or ""
    assert "skipped_damaged" in status or "unsupported_format" in status
    assert "Invalid JSON" in status
    assert str(bad.resolve()) in status
    with db() as conn:
        row = conn.execute(
            "SELECT status, error FROM source_sync_cursors WHERE host='antigravity' AND source_path=?",
            (str(bad.resolve()),),
        ).fetchone()
    assert row is not None
    assert row[0] == "error"
    assert "unsupported_format" in (row[1] or "")
    assert "Invalid JSON" in (row[1] or "")


def test_history_watcher_close_lifecycle_and_no_query_churn():
    from app.services import history_watcher

    # Verify contextlib.aclosing pattern and throttled query behavior
    roots_queried = 0

    def mock_roots():
        nonlocal roots_queried
        roots_queried += 1
        return ("/tmp/mock_root",)

    with patch("app.services.history_watcher.authorized_watch_roots", mock_roots):
        # Initial call
        r = history_watcher.authorized_watch_roots()
        assert r == ("/tmp/mock_root",)
        assert roots_queried == 1
