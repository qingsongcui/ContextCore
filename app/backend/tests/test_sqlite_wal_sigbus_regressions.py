"""Regression & multi-process concurrency tests for SQLite WAL SIGBUS prevention."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path

import pytest

from app.adapters import read_sessions
from app.adapters.base import AgentAdapterError, isolated_sqlite_snapshot
from app.database import backup_database, create_schema, db, hold_wal_keeper


def _seed_databases(base: Path) -> tuple[Path, Path, Path, Path]:
    oc_dir = base / "opencode"
    oc_dir.mkdir()
    oc_db = oc_dir / "opencode.db"
    with closing(sqlite3.connect(oc_db)) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE session(id TEXT, title TEXT, directory TEXT, parent_id TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message(id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part(id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
            INSERT INTO session VALUES('oc-s1', 'OpenCode WAL', '/synthetic/repo', NULL, 1000, 2000);
            INSERT INTO message VALUES('oc-m1', 'oc-s1', 1000, '{"role":"user"}');
            INSERT INTO part VALUES('oc-p1', 'oc-m1', 'oc-s1', 1000, '{"type":"text","text":"initial opencode wal"}');
            """
        )
        conn.commit()

    codex_dir = base / "codex"
    codex_dir.mkdir()
    state_db = codex_dir / "state_5.sqlite"
    with closing(sqlite3.connect(state_db)) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT, cwd TEXT, created_at_ms INTEGER, updated_at_ms INTEGER);
            INSERT INTO threads VALUES('cdx-t1', 'Codex WAL', '/synthetic/repo', 1000, 2000);
            """
        )
        conn.commit()
    hist_db = codex_dir / "thread_history_1.sqlite"
    with closing(sqlite3.connect(hist_db)) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE thread_items(
                thread_id TEXT, item_id TEXT, rollout_ordinal INTEGER,
                created_at_ms INTEGER, completed_at_ms INTEGER, item_type TEXT, item_json TEXT
            );
            INSERT INTO thread_items VALUES(
                'cdx-t1', 'i-1', 0, 1000, 1000, 'userMessage', '{"id":"i-1","text":"initial codex wal"}'
            );
            """
        )
        conn.commit()

    cursor_dir = base / "cursor"
    cursor_dir.mkdir()
    cursor_db = cursor_dir / "state.vscdb"
    with closing(sqlite3.connect(cursor_db)) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE composerHeaders(composerId TEXT, value TEXT, isSubagent INTEGER, createdAt INTEGER, lastUpdatedAt INTEGER);
            CREATE TABLE cursorDiskKV(key TEXT, value TEXT);
            INSERT INTO composerHeaders VALUES('cur-c1', '{"name":"Cursor WAL","workspaceIdentifier":{"uri":{"fsPath":"/synthetic/repo"}}}', 0, 1000, 2000);
            INSERT INTO cursorDiskKV VALUES('bubbleId:cur-c1:b1', '{"type":1,"bubbleId":"b1","text":"initial cursor wal","createdAt":"2026-11-14T10:00:00Z"}');
            """
        )
        conn.commit()
    return oc_db, state_db, hist_db, cursor_db


_WRITER_SCRIPT = r"""
import json, sqlite3, sys, time
from pathlib import Path

oc_db, state_db, hist_db, cursor_db, duration_s = sys.argv[1:6]
deadline = time.monotonic() + float(duration_s)
idx = 0
while time.monotonic() < deadline:
    idx += 1
    c_oc = sqlite3.connect(oc_db, timeout=2.0)
    c_oc.execute("PRAGMA journal_mode=WAL")
    c_oc.execute("INSERT INTO message VALUES(?,?,?,?)", (f"m-{idx}", "oc-s1", 2000 + idx, json.dumps({"role": "user"})))
    c_oc.execute("INSERT INTO part VALUES(?,?,?,?,?)", (f"p-{idx}", f"m-{idx}", "oc-s1", 2000 + idx, json.dumps({"type": "text", "text": f"wal turn {idx}"})))
    c_oc.commit()
    c_oc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c_oc.close()  # unlink -shm/-wal

    c_st = sqlite3.connect(state_db, timeout=2.0)
    c_st.execute("PRAGMA journal_mode=WAL")
    c_st.execute("UPDATE threads SET updated_at_ms=? WHERE id='cdx-t1'", (2000 + idx,))
    c_st.commit()
    c_st.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c_st.close()

    c_hi = sqlite3.connect(hist_db, timeout=2.0)
    c_hi.execute("PRAGMA journal_mode=WAL")
    c_hi.execute(
        "INSERT INTO thread_items VALUES('cdx-t1', ?, ?, ?, ?, 'userMessage', ?)",
        (f"i-{idx}", idx, 2000 + idx, 2000 + idx, json.dumps({"id": f"i-{idx}", "text": f"codex wal {idx}"})),
    )
    c_hi.commit()
    c_hi.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c_hi.close()

    c_cu = sqlite3.connect(cursor_db, timeout=2.0)
    c_cu.execute("PRAGMA journal_mode=WAL")
    c_cu.execute(
        "INSERT INTO cursorDiskKV VALUES(?,?)",
        (f"bubbleId:cur-c1:b-{idx}", json.dumps({"type": 1, "bubbleId": f"b-{idx}", "text": f"cursor wal {idx}", "createdAt": "2026-11-14T10:00:01Z"})),
    )
    c_cu.commit()
    c_cu.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c_cu.close()
"""


def test_multiprocess_wal_checkpoint_truncate_unlink_never_sigbus(tmp_path: Path) -> None:
    snap_tmp = tmp_path / "snap-tmp"
    snap_tmp.mkdir()
    prev_tempdir = tempfile.tempdir
    tempfile.tempdir = str(snap_tmp)
    try:
        oc_db, state_db, hist_db, cursor_db = _seed_databases(tmp_path)
        proc = subprocess.Popen(
            [sys.executable, "-c", _WRITER_SCRIPT, str(oc_db), str(state_db), str(hist_db), str(cursor_db), "2.5"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        errors: list[BaseException] = []
        counts = {"opencode": 0, "codex": 0, "cursor": 0}
        retryable_counts = {"opencode": 0, "codex": 0, "cursor": 0}
        lock = threading.Lock()

        def _reader(host: str, root: Path, source_paths: list[str] | None = None) -> None:
            for _ in range(45):
                try:
                    sessions = read_sessions(host, [str(root)], source_paths=source_paths)
                    assert sessions, f"{host} returned empty sessions"
                    with lock:
                        counts[host] += 1
                except AgentAdapterError as exc:
                    if exc.code == "file_changed" and exc.retryable:
                        with lock:
                            retryable_counts[host] += 1
                    else:
                        errors.append(exc)
                        return
                except BaseException as exc:
                    errors.append(exc)
                    return

        threads = [
            threading.Thread(target=_reader, args=("opencode", oc_db.parent, [str(oc_db)])),
            threading.Thread(target=_reader, args=("codex", state_db.parent, [str(state_db), str(hist_db)])),
            threading.Thread(target=_reader, args=("cursor", cursor_db.parent, [str(cursor_db)])),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        stdout, stderr = proc.communicate(timeout=30)

        assert proc.returncode == 0, f"writer failed: rc={proc.returncode} stdout={stdout} stderr={stderr}"
        assert not errors, f"unexpected reader errors: {errors}"
        for host in ("opencode", "codex", "cursor"):
            assert counts[host] + retryable_counts[host] == 45
            assert counts[host] >= 1

        # No leaked cc-sqlsnap-* dirs in temp dir, and readers recreated no sidecar files after writer exited
        assert [p.name for p in snap_tmp.iterdir() if p.name.startswith("cc-sqlsnap-")] == []
        for dbp in (oc_db, state_db, hist_db, cursor_db):
            assert not Path(f"{dbp}-shm").exists()
            assert not Path(f"{dbp}-wal").exists()
    finally:
        tempfile.tempdir = prev_tempdir


def test_sqlite_sidecar_symlinks_rejected_as_symlink_escape(tmp_path: Path) -> None:
    oc_db, state_db, hist_db, cursor_db = _seed_databases(tmp_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside")

    for suffix in ("-wal", "-shm", "-journal"):
        for host, dbp, roots, src in (
            ("opencode", oc_db, [str(oc_db.parent)], [str(oc_db)]),
            ("codex", state_db, [str(state_db.parent)], [str(state_db), str(hist_db)]),
            ("cursor", cursor_db, [str(cursor_db.parent)], [str(cursor_db)]),
        ):
            sidecar = Path(f"{dbp}{suffix}")
            bak = Path(f"{sidecar}.orig_bak")
            had_existing = os.path.lexists(sidecar)
            if had_existing:
                sidecar.rename(bak)
            sidecar.symlink_to(outside)
            try:
                with pytest.raises(AgentAdapterError) as exc_info:
                    read_sessions(host, roots, source_paths=src)
                assert exc_info.value.code == "symlink_escape"
                assert exc_info.value.host == host
            finally:
                if os.path.lexists(sidecar):
                    sidecar.unlink()
                if bak.exists():
                    bak.rename(sidecar)


def test_readonly_db_snapshot_preserves_source_permissions_and_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, _, cursor_db = _seed_databases(tmp_path)
    # Add WAL frame then remove write bits on source file
    conn = sqlite3.connect(cursor_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES(?,?)",
        ("bubbleId:cur-c1:b-ro", '{"type":1,"bubbleId":"b-ro","text":"readonly wal frame","createdAt":"2026-11-14T10:01:00Z"}'),
    )
    conn.commit()
    conn.close()

    os.chmod(cursor_db, 0o444)
    try:
        before_stat = os.stat(cursor_db)
        sessions = read_sessions("cursor", [str(cursor_db.parent)], source_paths=[str(cursor_db)])
        assert "readonly wal frame" in [m["content"] for m in sessions[0]["messages"]]
        after_stat = os.stat(cursor_db)
        assert (after_stat.st_mode & 0o777) == 0o444
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns

        # Force fallback path (_CLONEFILE_FN = None) and verify streaming copy still succeeds on 0o444
        import app.adapters.base as base_mod

        monkeypatch.setattr(base_mod, "_CLONEFILE_FN", None)
        with isolated_sqlite_snapshot(cursor_db, host="cursor") as staged:
            assert staged.exists()
            assert (os.stat(staged).st_mode & 0o777) == 0o600
    finally:
        os.chmod(cursor_db, 0o600)


def test_hold_wal_keeper_supports_concurrent_db_and_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_file = tmp_path / "internal.db"
    data_dir = tmp_path / "internal-data"
    data_dir.mkdir()
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_file))
    monkeypatch.setenv("CONTEXTCORE_DATA_DIR", str(data_dir))
    create_schema(db_file)

    worker_errors: list[BaseException] = []
    with hold_wal_keeper(db_file):
        assert Path(f"{db_file}-shm").exists()

        def _rw_worker(idx: int) -> None:
            try:
                for step in range(10):
                    with db(db_file) as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                            (f"k-{idx}-{step}", json.dumps({"n": step}), "2026-11-14T10:00:00Z"),
                        )
                        conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()
            except BaseException as exc:
                worker_errors.append(exc)

        workers = [threading.Thread(target=_rw_worker, args=(i,)) for i in range(4)]
        for w in workers:
            w.start()
        backup_res = backup_database(db_path=db_file)
        for w in workers:
            w.join(timeout=15)

        assert not worker_errors
        assert backup_res["ok"] is True
        assert Path(backup_res["backup_path"]).is_file()
        assert Path(f"{db_file}-shm").exists()

    # After exiting hold_wal_keeper cleanly, backup file remains valid and no extra locks remain
    with db(db_file) as conn:
        count = conn.execute("SELECT COUNT(*) FROM app_settings WHERE key LIKE 'k-%'").fetchone()[0]
    assert count == 40


def test_hold_wal_keeper_survives_external_process_connect_and_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_file = tmp_path / "multiprocess.db"
    data_dir = tmp_path / "multiprocess-data"
    data_dir.mkdir(mode=0o700)
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_file))
    monkeypatch.setenv("CONTEXTCORE_DATA_DIR", str(data_dir))
    create_schema(db_file)

    with hold_wal_keeper(db_file):
        # Parent db() calls must not drop the keeper's POSIX fcntl -shm locks via os.close(fd).
        with db(db_file) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                ("parent-initial", json.dumps({"ok": True}), "2026-11-15T00:00:00Z"),
            )

        child_code_1 = "\n".join(
            [
                "import json, sys",
                "from pathlib import Path",
                "from app.database import db",
                "p = Path(sys.argv[1])",
                "with db(p) as conn:",
                "    conn.execute(",
                "        'INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)',",
                "        ('child-1', json.dumps({'ok': True}), '2026-11-15T00:00:01Z'),",
                "    )",
            ]
        )
        subprocess.run([sys.executable, "-c", child_code_1, str(db_file)], check=True, timeout=15)

        child_code_2 = "\n".join(
            [
                "import sqlite3, sys",
                "c = sqlite3.connect(sys.argv[1], timeout=5)",
                "c.execute('PRAGMA journal_mode=WAL')",
                "c.execute('PRAGMA wal_checkpoint(PASSIVE)')",
                "c.close()",
            ]
        )
        subprocess.run([sys.executable, "-c", child_code_2, str(db_file)], check=True, timeout=15)

        assert Path(f"{db_file}-shm").exists()
        with db(db_file) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                ("parent-after-child", json.dumps({"ok": True}), "2026-11-15T00:00:02Z"),
            )
            keys = {row["key"] for row in conn.execute("SELECT key FROM app_settings").fetchall()}
        assert {"parent-initial", "child-1", "parent-after-child"} <= keys
