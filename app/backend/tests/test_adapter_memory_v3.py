"""Synthetic regression coverage for bounded adapter reads and authorized planning."""
from __future__ import annotations

import json
import sqlite3
import threading
import tracemalloc
from pathlib import Path

import pytest

from app.adapters import base, codex, claude_code, omp, opencode, cursor, antigravity
from app.adapters.base import AgentAdapterError
from app.database import create_schema, seed_data, db
from app.services import connection_service
from app.services import conversation_service
from app.services.sync_budget import SyncBudget, SyncCancelled


def _line(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"


def test_codex_session_index_is_read_only_within_authorized_roots(tmp_path, monkeypatch):
    root = tmp_path / "authorized"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "session_index.jsonl").write_bytes(_line({"id": "s", "thread_name": "OUTSIDE"}))
    rollout = root / "rollout-s.jsonl"
    rollout.write_bytes(_line({"type": "session_meta", "payload": {"id": "s"}}) +
                        _line({"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}}))
    assert codex.read_codex_sessions([str(rollout)])[0]["title"] != "OUTSIDE"

    (root / "session_index.jsonl").write_bytes(_line({"id": "s", "thread_name": "INSIDE"}))
    assert codex.read_codex_sessions([str(root)])[0]["title"] == "INSIDE"

    (root / "session_index.jsonl").unlink()
    try:
        (root / "session_index.jsonl").symlink_to(outside / "session_index.jsonl")
    except OSError:
        pytest.skip("symlink creation unavailable")
    assert codex.read_codex_sessions([str(root)])[0]["title"] != "OUTSIDE"


@pytest.mark.parametrize("host,mod,entry_name,discover_name", [
    ("codex", codex, "rollout-s.jsonl", "_iter_jsonl"),
    ("claude-code", claude_code, "s.jsonl", "_iter_transcripts"),
    ("omp", omp, "s.jsonl", "rglob"),
    ("cursor", cursor, "state.vscdb", "_find_cursor_databases"),
    ("antigravity", antigravity, "transcript.jsonl", "_discover_session_targets"),
])
def test_source_paths_bypass_full_tree_discovery(tmp_path, monkeypatch, host, mod, entry_name, discover_name):
    root = tmp_path / "authorized"
    root.mkdir()
    source = root / entry_name
    if host == "codex":
        source.write_bytes(_line({"type": "session_meta", "payload": {"id": "s"}}) +
                           _line({"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}}))
        read = codex.read_codex_sessions
    elif host == "claude-code":
        source.write_bytes(_line({"type": "user", "uuid": "u", "sessionId": "s", "message": {"role": "user", "content": "hello"}}))
        read = claude_code.read_claude_code_sessions
    elif host == "omp":
        source.write_bytes(_line({"type": "session", "id": "s", "version": 3}) +
                           _line({"type": "message", "id": "u", "message": {"role": "user", "content": "hello"}}))
        read = omp.read_omp_sessions
    elif host == "cursor":
        with sqlite3.connect(source) as conn:
            conn.execute("CREATE TABLE composerHeaders(composerId TEXT, value TEXT, isSubagent INTEGER)")
            conn.execute("CREATE TABLE cursorDiskKV(key TEXT, value TEXT)")
        monkeypatch.setattr(cursor, "_parse_cursor_db", lambda *_args, **_kwargs: [])
        read = cursor.read_cursor_sessions
    else:
        source.write_bytes(_line({"type": "USER_INPUT", "step_index": 1, "content": "hello"}))
        read = antigravity.read_antigravity_sessions

    def forbidden(*_args, **_kwargs):
        raise AssertionError(f"{host} performed full-tree discovery despite source_paths")

    if discover_name == "rglob":
        monkeypatch.setattr(Path, "rglob", forbidden)
    else:
        monkeypatch.setattr(mod, discover_name, forbidden)
    result = read([str(root)], source_paths=[str(source)])
    assert isinstance(result, list)


def test_claude_checks_line_bound_before_unbounded_read_and_streams_records(tmp_path, monkeypatch):
    root = tmp_path / "authorized"
    root.mkdir()
    oversized = root / "oversized.jsonl"
    oversized.write_bytes(b'{"type":"summary","summary":"' + b"x" * 100_000 + b'"}\n')
    monkeypatch.setattr(base, "MAX_LINE_LENGTH", 1024)
    tracemalloc.start()
    with pytest.raises(AgentAdapterError) as error:
        claude_code._parse_file(oversized, [root])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert error.value.code == "truncated"
    assert peak < 64 * 1024
    oversized_peak = peak

    source = root / "many.jsonl"
    summary_line = _line({"type": "summary", "summary": "x" * 2048, "unused": "y" * 2048})
    source.write_bytes(summary_line * 1024)
    monkeypatch.setattr(base, "MAX_LINE_LENGTH", 8 * 1024 * 1024)
    monkeypatch.setattr(base, "read_complete_jsonl", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("whole-file read")))
    tracemalloc.start()
    result = claude_code._parse_file(source, [root])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert result is None
    assert peak < 1024 * 1024
    print(f"CLAUDE_STREAM bytes={source.stat().st_size} peak_bytes={peak} oversized_peak_bytes={oversized_peak}")


def test_claude_thin_metadata_index_is_disk_backed(tmp_path):
    root = tmp_path.resolve()
    source = root / "claude.jsonl"
    row = json.dumps({"type": "summary", "summary": "x"}, separators=(",", ":")).encode() + b"\n"
    source.write_bytes(row * 30_000)
    tracemalloc.start()
    result = claude_code._parse_file(source, [root])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert result is None
    assert peak < 1024 * 1024
    print(f"CLAUDE_THIN_RECORDS rows=30000 bytes={source.stat().st_size} peak_bytes={peak}")


def test_omp_retains_only_active_chain_metadata_and_payload(tmp_path):
    root = tmp_path / "authorized"
    root.mkdir()
    source = root / "s.jsonl"
    rows = [_line({"type": "session", "id": "s", "version": 3})]
    parent = None
    for index in range(1024):
        rows.append(_line({"id": f"e{index}", "parentId": parent, "type": "reset_boundary", "unused": "x" * 4096}))
        parent = f"e{index}"
    source.write_bytes(b"".join(rows))
    tracemalloc.start()
    result = omp.parse_omp_session_file(source, [root])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert result["messages"] == []
    assert result["process_summary"].startswith("- **reset_boundary**")
    assert peak < 1024 * 1024
    print(f"OMP_ACTIVE_CHAIN bytes={source.stat().st_size} peak_bytes={peak}")


def test_omp_thin_parent_index_is_disk_backed(tmp_path):
    root = tmp_path.resolve()
    source = root / "omp.jsonl"
    parent = None
    with source.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "session", "id": "o", "version": 3}, separators=(",", ":")) + "\n")
        for index in range(30_000):
            handle.write(json.dumps({"id": f"e{index}", "parentId": parent, "type": "reset_boundary"}, separators=(",", ":")) + "\n")
            parent = f"e{index}"
    tracemalloc.start()
    result = omp.parse_omp_session_file(source, [root])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert result["messages"] == []
    assert len(result["process_summary"]) <= 2048
    assert "later process records were not copied" in result["process_summary"]
    assert peak < 1024 * 1024
    print(f"OMP_THIN_RECORDS rows=30000 bytes={source.stat().st_size} peak_bytes={peak}")


def test_opencode_iterates_database_rows_without_fetchall(tmp_path, monkeypatch):
    source = tmp_path / "opencode.db"
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """)
        conn.executemany("INSERT INTO session VALUES(?,?,?,?,?,?)", [
            ("s1", "one", "/synthetic", None, 1, 3), ("s2", "two", "/synthetic", None, 4, 6)])
        for sid in ("s1", "s2"):
            conn.execute("INSERT INTO message VALUES(?,?,?,?)", ("m" + sid, sid, 1, json.dumps({"role": "user"})))
            conn.execute("INSERT INTO part VALUES(?,?,?,?,?)", ("text-" + sid, "m" + sid, sid, 0,
                json.dumps({"type": "text", "text": "synthetic"})))
            for index in range(256):
                conn.execute("INSERT INTO part VALUES(?,?,?,?,?)", (f"p{sid}-{index}", "m" + sid, sid, index,
                    json.dumps({"type": "snapshot", "unused": "z" * 4096})))

    original = sqlite3.connect

    class NoFetchCursor(sqlite3.Cursor):
        def fetchall(self):
            raise AssertionError("OpenCode adapter must iterate SQLite rows")

    class NoFetchConnection(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            return super().cursor(factory=NoFetchCursor)

    monkeypatch.setattr(opencode.sqlite3, "connect", lambda *args, **kwargs: original(*args, factory=NoFetchConnection, **kwargs))
    tracemalloc.start()
    sessions = opencode._parse_database(source, [tmp_path])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert {item["external_id"] for item in sessions} == {"opencode:s1", "opencode:s2"}
    assert peak < 2 * 1024 * 1024
    print(f"OPENCODE_ITER rows=514 peak_bytes={peak}")


def test_opencode_spool_closes_when_import_cancels_after_first_session(tmp_path, monkeypatch):
    create_schema()
    seed_data()
    root = tmp_path / "opencode-cancel"
    root.mkdir()
    source = root / "opencode.db"
    body = "x" * 50_000
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """)
        for index in range(2):
            sid, mid = f"s{index}", f"m{index}"
            conn.execute("INSERT INTO session VALUES(?,?,?,?,?,?)", (sid, sid, "/synthetic", None, index, index + 1))
            conn.execute("INSERT INTO message VALUES(?,?,?,?)", (mid, sid, index, json.dumps({"role": "user"})))
            conn.execute("INSERT INTO part VALUES(?,?,?,?,?)", (f"p{index}", mid, sid, index,
                json.dumps({"type": "text", "text": body})))
    connection_service.patch_connection("opencode", paths=[str(root)], enabled=True, confirmed=True)

    import tempfile
    from app.services.sync_budget import SyncBudget, SyncCancelled
    real_spool = tempfile.SpooledTemporaryFile
    opened = []

    def tracked_spool(*args, **kwargs):
        spool = real_spool(*args, **kwargs)
        opened.append(spool)
        return spool

    monkeypatch.setattr(connection_service.tempfile, "SpooledTemporaryFile", tracked_spool)
    budget = SyncBudget(threading.Event(), duty_cycle=1)
    upsert = conversation_service.upsert_conversation
    calls = 0

    def cancel_after_first(**kwargs):
        nonlocal calls
        upsert(**kwargs)
        calls += 1
        if calls == 1:
            raise SyncCancelled()

    monkeypatch.setattr(conversation_service, "upsert_conversation", cancel_after_first)
    with pytest.raises(SyncCancelled):
        connection_service.sync_connection("opencode", force=False, budget=budget)
    assert calls == 1 and opened
    assert opened[0]._rolled and opened[0].closed
    with db() as conn:
        cursor = conn.execute("SELECT status FROM source_sync_cursors WHERE host='opencode'").fetchone()
    assert cursor["status"] == "pending"

    monkeypatch.setattr(conversation_service, "upsert_conversation", upsert)
    replay = connection_service.sync_connection("opencode", force=False, budget=SyncBudget(threading.Event(), duty_cycle=1))
    assert replay.get("error") is None
    assert len(conversation_service.list_conversations(agent="opencode")) == 2
    with db() as conn:
        cursor = conn.execute("SELECT status FROM source_sync_cursors WHERE host='opencode'").fetchone()
    assert cursor["status"] == "ready"


def test_opencode_successful_legacy_replay_clears_old_child_error_cursors(tmp_path):
    create_schema()
    seed_data()
    root = tmp_path / "opencode-old-cursors"
    storage = root / "storage"
    (storage / "session").mkdir(parents=True)
    (storage / "message" / "s").mkdir(parents=True)
    (storage / "part" / "m").mkdir(parents=True)
    session_path = storage / "session" / "s.json"
    session_path.write_text(json.dumps({"id": "s", "title": "legacy", "directory": "/repo"}), encoding="utf-8")
    (storage / "message" / "s" / "m.json").write_text(
        json.dumps({"id": "m", "role": "user", "time": {"created": 1}}), encoding="utf-8")
    (storage / "part" / "m" / "p.json").write_text(
        json.dumps({"type": "text", "text": "legacy history"}), encoding="utf-8")
    connection_service.patch_connection("opencode", paths=[str(root)], enabled=True, confirmed=True)
    with db() as conn:
        connection_service.ensure_source_cursor_schema(conn)
        conn.execute("""INSERT INTO source_sync_cursors
            (host,source_path,fingerprint,file_identity,complete_offset,status,error,updated_at)
            VALUES('opencode',?,'old','','0','error','unsupported_format','2026-10-23T00:00:00Z')""",
            (str(session_path.resolve()),))

    result = connection_service.sync_connection("opencode", force=True)
    assert result.get("error") is None
    with db() as conn:
        child = conn.execute("SELECT 1 FROM source_sync_cursors WHERE host='opencode' AND source_path=?",
                             (str(session_path.resolve()),)).fetchone()
        directory = conn.execute("SELECT status FROM source_sync_cursors WHERE host='opencode' AND source_path=?",
                                 (str(storage.resolve()),)).fetchone()
    assert child is None and directory["status"] == "ready"


def test_opencode_stream_does_not_silently_cap_sessions_at_5000(tmp_path):
    source = tmp_path / "opencode.db"
    session_count = 5_001
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """)
        for index in range(session_count):
            sid, mid = f"s{index:05}", f"m{index:05}"
            conn.execute("INSERT INTO session VALUES(?,?,?,?,?,?)", (sid, sid, "/synthetic", None, index, index))
            conn.execute("INSERT INTO message VALUES(?,?,?,?)", (mid, sid, index, json.dumps({"role": "user"})))
            conn.execute("INSERT INTO part VALUES(?,?,?,?,?)", (f"p{index:05}", mid, sid, index,
                json.dumps({"type": "text", "text": "complete synthetic body"})))
    tracemalloc.start()
    observed = sum(1 for _ in opencode.iter_opencode_sessions([str(tmp_path)], source_paths=[str(source)]))
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert observed == session_count
    assert peak < 2 * 1024 * 1024
    print(f"OPENCODE_SESSION_STREAM sessions={observed} peak_bytes={peak}")


def test_opencode_sync_consumes_one_session_at_a_time(tmp_path, monkeypatch):
    create_schema()
    seed_data()
    root = tmp_path / "opencode"
    root.mkdir()
    source = root / "opencode.db"
    body = "s" * (16 * 1024)
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """)
        for index in range(128):
            sid = f"ses_{index:04}"
            mid = f"msg_{index:04}"
            conn.execute("INSERT INTO session VALUES(?,?,?,?,?,?)", (sid, sid, "/synthetic", None, index, index + 1))
            conn.execute("INSERT INTO message VALUES(?,?,?,?)", (mid, sid, index, json.dumps({"role": "user"})))
            conn.execute("INSERT INTO part VALUES(?,?,?,?,?)", (f"prt_{index:04}", mid, sid, index,
                json.dumps({"type": "text", "text": body})))
    connection_service.patch_connection("opencode", paths=[str(root)], enabled=True, confirmed=True)
    monkeypatch.setattr(opencode, "read_opencode_sessions", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("sync must use the incremental session reader")))
    tracemalloc.start()
    result = connection_service.sync_connection("opencode", force=False)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert not result.get("error")
    assert peak < 4 * 1024 * 1024
    sessions = conversation_service.list_conversations(agent="opencode")
    assert len(sessions) == 128
    assert all(len(session["messages"][0]["content"]) == len(body) for session in sessions)
    print(f"OPENCODE_SYNC sessions={len(sessions)} body_bytes={len(body) * len(sessions)} peak_bytes={peak}")


def test_opencode_legacy_streams_300_sessions_and_service_sync(tmp_path):
    create_schema()
    seed_data()
    root = tmp_path / "opencode-legacy"
    storage = root / "storage"
    body = "x" * 4096
    session_count = 300
    for index in range(session_count):
        session_id, message_id = f"s{index}", f"m{index}"
        session_file = storage / "session" / f"{session_id}.json"
        message_file = storage / "message" / session_id / f"{message_id}.json"
        part_file = storage / "part" / message_id / "p.json"
        session_file.parent.mkdir(parents=True, exist_ok=True)
        message_file.parent.mkdir(parents=True, exist_ok=True)
        part_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text(json.dumps({"id": session_id, "title": session_id, "directory": "/synthetic"}), encoding="utf-8")
        message_file.write_text(json.dumps({"id": message_id, "role": "user", "time": {"created": index}}), encoding="utf-8")
        part_file.write_text(json.dumps({"type": "text", "text": body}), encoding="utf-8")
    source_paths = [str(path.resolve()) for path in storage.rglob("*.json")]

    tracemalloc.start()
    parsed_count = sum(1 for _ in opencode.iter_opencode_sessions([str(root.resolve())], source_paths=source_paths))
    parse_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert parsed_count == session_count
    assert parse_peak < 2 * 1024 * 1024

    connection_service.patch_connection("opencode", paths=[str(root)], enabled=True, confirmed=True)
    tracemalloc.start()
    result = connection_service.sync_connection("opencode", force=False)
    sync_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    conversations = conversation_service.list_conversations(agent="opencode")
    assert not result.get("error")
    assert len(conversations) == session_count
    assert {item["external_id"] for item in conversations} == {f"opencode:s{i}" for i in range(session_count)}
    assert all(item["messages"][0]["content"] == body for item in conversations)
    assert sync_peak < 3 * 1024 * 1024
    print(f"OPENCODE_LEGACY sessions={parsed_count} paths={len(source_paths)} parse_peak_bytes={parse_peak} sync_peak_bytes={sync_peak}")


def test_source_planning_is_bounded_and_cancellable(tmp_path):
    create_schema()
    seed_data()
    measurements = []
    for source_count in (1_000, 10_000, 20_000, 40_000):
        root = tmp_path / f"many-{source_count}"
        root.mkdir()
        for index in range(source_count):
            (root / f"{index:05}.jsonl").write_bytes(b"{}\n")
        budget = SyncBudget(threading.Event(), duty_cycle=1)
        budget.max_sources = 16
        tracemalloc.start()
        selected, snapshots, failures, pending = connection_service._source_plan(
            "claude-code", [str(root)], False, budget=budget)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert len(selected) == len(snapshots) == 16
        assert pending == source_count - 16
        assert not failures
        # 3 MiB leaves headroom for shared-runner noise while the original
        # 14.49 MB whole-inventory implementation remains a clear failure.
        assert peak < 3 * 1024 * 1024
        measurements.append((source_count, peak))
        print(f"SOURCE_PLAN files={source_count} selected=16 pending={pending} peak_bytes={peak}")
    # Report growth per additional discovered path rather than comparing tiny
    # peaks where allocator noise dominates. The old whole-inventory plan was
    # over 14 MB for this scale; the bounded planner stays below 128 B/path.
    added_paths = measurements[-1][0] - measurements[0][0]
    peak_growth_per_path = max(0, measurements[-1][1] - measurements[0][1]) / added_paths
    assert peak_growth_per_path < 128

    cancelled = SyncBudget(threading.Event(), duty_cycle=1)
    cancelled.stop.set()
    root = tmp_path / "many-1000"
    with pytest.raises(SyncCancelled):
        connection_service._source_plan("claude-code", [str(root)], False, budget=cancelled)


def test_foreground_sync_finishes_all_sources_across_bounded_batches(tmp_path):
    create_schema()
    seed_data()
    root = tmp_path / "many-foreground"
    root.mkdir()
    source_count = 260
    for index in range(source_count):
        (root / f"{index:04}.jsonl").write_bytes(_line({
            "type": "user", "uuid": f"u{index}", "sessionId": f"s{index}",
            "message": {"role": "user", "content": f"synthetic source {index}"},
        }))
    connection_service.patch_connection("claude-code", paths=[str(root)], enabled=True, confirmed=True)
    result = connection_service.sync_connection("claude-code", force=False)
    conversations = conversation_service.list_conversations(agent="claude-code")
    assert result["processed_sources"] == source_count
    assert result["pending_sources"] == 0
    assert len(conversations) == source_count
