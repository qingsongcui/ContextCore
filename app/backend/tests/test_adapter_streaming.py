"""Synthetic bounded-memory adapter checks, never reads a real host directory."""
import json
import sqlite3
import tracemalloc
from pathlib import Path

import pytest

from app.adapters import base, codex, cursor
from app.adapters.base import AgentAdapterError, iter_complete_jsonl


def test_complete_line_iterator_skips_split_utf8_tail_and_handles_bom(tmp_path):
    path = tmp_path / "source.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + '{"text":"完整中文"}\n{"text":"two"}\n'.encode() + b'{"text":"\xe4\xb8')
    assert list(iter_complete_jsonl(path, encoding="utf-8-sig")) == ['{"text":"完整中文"}\n', '{"text":"two"}\n']
    assert list(iter_complete_jsonl(path, encoding="utf-8-sig", offset=path.stat().st_size)) == []


@pytest.mark.parametrize("replace", [False, True])
def test_iterator_stops_at_original_size_and_rejects_append_or_replacement(tmp_path, replace):
    path = tmp_path / "source.jsonl"
    path.write_bytes(b'{"n":1}\n{"n":2}\n')
    lines = iter_complete_jsonl(path, host="codex")
    assert next(lines) == '{"n":1}\n'
    if replace:
        replacement = tmp_path / "replacement.jsonl"
        replacement.write_bytes(b'{"n":999}\n')
        replacement.replace(path)
    else:
        with path.open("ab") as handle:
            handle.write(b'{"n":999}\n')
    assert next(lines) == '{"n":2}\n'
    with pytest.raises(AgentAdapterError) as error:
        next(lines)
    assert error.value.code == "file_changed" and error.value.host == "codex"


def test_committed_decode_error_and_oversize_line_are_explicit(tmp_path, monkeypatch):
    path = tmp_path / "source.jsonl"
    path.write_bytes(b'{"text":"\xff"}\n')
    with pytest.raises(AgentAdapterError) as error:
        list(iter_complete_jsonl(path, host="codex"))
    assert error.value.code == "unsupported_format" and error.value.host == "codex"
    monkeypatch.setattr(base, "MAX_LINE_LENGTH", 32)
    path.write_bytes(b'{"text":"' + b'x' * 64 + b'"}\n')
    with pytest.raises(AgentAdapterError) as error:
        list(iter_complete_jsonl(path))
    assert error.value.code == "truncated"


def test_codex_12mib_tool_payload_is_streamed_and_preserves_identity_time_usage(tmp_path, monkeypatch):
    ident = "12345678-1234-1234-1234-123456789abc"
    path = tmp_path / f"rollout-2026-{ident}.jsonl"
    garbage = json.dumps({"type": "response_item", "payload": {"type": "function_call_output", "output": "X" * 16384}}).encode() + b"\n"
    with path.open("wb") as handle:
        handle.write((json.dumps({"type": "session_meta", "timestamp": "2026-10-24T00:00:00Z", "payload": {"id": ident, "cwd": "/synthetic/own"}}) + "\n").encode())
        # Replayed parent's metadata must not become the child's owning session.
        handle.write((json.dumps({"type": "session_meta", "payload": {"id": "parent", "cwd": "/synthetic/parent"}}) + "\n").encode())
        handle.write((json.dumps({"type": "event_msg", "timestamp": "2026-10-24T01:00:00Z", "payload": {"type": "user_message", "id": "u", "message": "中文任务"}}) + "\n").encode())
        for _ in range(768):
            handle.write(garbage)
        handle.write((json.dumps({"type": "event_msg", "timestamp": "2026-10-24T01:00:01Z", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2}}}}) + "\n").encode())
        handle.write((json.dumps({"type": "event_msg", "timestamp": "2026-10-24T01:00:02Z", "payload": {"type": "agent_message", "id": "a", "message": "已完成"}}) + "\n").encode())
        handle.write(b'{"unfinished":"\xe4\xb8')
    assert 12 * 1024 * 1024 <= path.stat().st_size <= 16 * 1024 * 1024
    monkeypatch.setattr(base, "read_complete_jsonl", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Full JSONL read is forbidden")))
    tracemalloc.start()
    result = codex._parse_codex_rollout(path)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert result["external_id"] == "codex:" + ident and result["cwd"] == "/synthetic/own"
    assert result["started_at"] == "2026-10-24T00:00:00Z" and result["updated_at"] == "2026-10-24T01:00:02Z"
    assert [m["content"] for m in result["messages"]] == ["中文任务", "已完成"]
    assert result["usage_observations"][0]["payload"]["info"]["total_token_usage"]["input_tokens"] == 10
    assert peak < 2 * 1024 * 1024
    print(f"CODEX_STREAM bytes={path.stat().st_size} peak_bytes={peak}")


def test_cursor_12mib_tool_payload_uses_row_iteration_and_retains_only_needed_fields(tmp_path, monkeypatch):
    path = tmp_path / "state.vscdb"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE composerHeaders(composerId TEXT,value TEXT,isSubagent INTEGER)")
        conn.execute("CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY,value TEXT)")
        for cid in ("one", "two"):
            conn.execute("INSERT INTO composerHeaders VALUES (?,?,0)", (cid, json.dumps({"name": cid})))
        tool = json.dumps({"type": 3, "createdAt": "2026-10-24T01:00:01Z", "capabilityType": "tool", "unrelatedToolContext": {"payload": "X" * 16384}})
        for begin in range(0, 768, 64):
            conn.executemany("INSERT INTO cursorDiskKV VALUES (?,?)", [(f"bubbleId:{'one' if i % 2 else 'two'}:t{i}", tool) for i in range(begin, begin + 64)])
        for cid in ("one", "two"):
            conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (f"bubbleId:{cid}:user", json.dumps({"type": 1, "text": "用户 " + cid, "createdAt": "2026-10-24T01:00:00Z"})))
            conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (f"bubbleId:{cid}:assistant", json.dumps({"type": 2, "text": "回答 " + cid, "createdAt": "2026-10-24T01:00:02Z"})))
        # Large non-bubble records must never enter the adapter's value query.
        conn.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("nonBubble:large", "Y" * 1_000_000))
    original = sqlite3.connect
    class NoFetchCursor(sqlite3.Cursor):
        def fetchall(self):
            raise AssertionError("Adapter may not load a whole SQLite result set")
    class NoFetchConnection(sqlite3.Connection):
        def cursor(self, *args, **kwargs):
            return super().cursor(factory=NoFetchCursor)
    monkeypatch.setattr(cursor.sqlite3, "connect", lambda *args, **kwargs: original(*args, factory=NoFetchConnection, **kwargs))
    tracemalloc.start()
    result = cursor._parse_cursor_db(path, [tmp_path])
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert {s["external_id"] for s in result} == {"cursor:one", "cursor:two"}
    for session in result:
        assert session["source_path"] == str(path)
        assert len(session["messages"]) == 2 and all("X" not in m["content"] for m in session["messages"])
        assert session["started_at"] == "2026-10-24T01:00:00Z" and session["updated_at"] == "2026-10-24T01:00:02Z"
        lines = session["process_summary"].splitlines()
        assert lines[:50] == ["- step: tool"] * 50
        assert lines[-1] == "- **omitted**: later process records were not copied into this summary"
    assert peak < 3 * 1024 * 1024
    print(f"CURSOR_STREAM bytes={path.stat().st_size} peak_bytes={peak}")
