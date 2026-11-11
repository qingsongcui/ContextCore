from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.adapters import AgentAdapterError, PartialHistoryError, parse_hook, read_sessions


def _write_jsonl(path: Path, entries: list[dict], trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(item) for item in entries)
    path.write_text(body + ("\n" if trailing_newline else ""), encoding="utf-8")


def _claude_entries() -> list[dict]:
    return [
        {"type": "summary", "summary": "Fix the login flow", "uuid": "s0", "timestamp": "2026-10-01T10:00:00Z"},
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": "session-1", "cwd": "/repo",
         "timestamp": "2026-10-01T10:00:01Z", "message": {"role": "user", "content": "Please fix the login flow."}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": "session-1", "cwd": "/repo",
         "timestamp": "2026-10-01T10:00:02Z", "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "inspect"},
             {"type": "text", "text": "I will inspect the form."},
             {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "login.tsx"}},
         ]}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": "session-1", "cwd": "/repo",
         "timestamp": "2026-10-01T10:00:03Z", "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"},
         ]}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": "session-1", "cwd": "/repo",
         "timestamp": "2026-10-01T10:00:04Z", "message": {"role": "assistant", "content": [
             {"type": "text", "text": "The session token was not refreshed."},
         ]}},
        {"type": "assistant", "uuid": "abandoned", "parentUuid": "u1", "sessionId": "other", "cwd": "/repo",
         "timestamp": "2026-10-01T10:00:05Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Abandoned branch."}]}},
        {"type": "assistant", "uuid": "side", "parentUuid": "a2", "isSidechain": True, "sessionId": "session-1",
         "timestamp": "2026-10-01T10:00:06Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Subagent only."}]}},
    ]


def test_claude_code_reads_leaf_chain_and_skips_sidechain(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "projects"
    _write_jsonl(root / "-repo" / "session-1.jsonl", _claude_entries())
    sessions = read_sessions("claude-code", [str(root)])
    assert len(sessions) == 1
    session = sessions[0]
    assert session["external_id"] == "claude-code:session-1"
    assert session["title"] == "Fix the login flow"
    assert session["cwd"] == "/repo"
    assert [item["content"] for item in session["messages"]] == [
        "Please fix the login flow.",
        "I will inspect the form.",
        "The session token was not refreshed.",
    ]
    assert "Abandoned branch." not in json.dumps(session, ensure_ascii=False)
    assert "Subagent only." not in json.dumps(session, ensure_ascii=False)
    assert "Read" in session["process_summary"]


def test_claude_code_tolerates_eof_half_line_and_rejects_middle_corruption(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "projects" / "-repo"
    root.mkdir(parents=True)
    path = root / "s.jsonl"
    entries = _claude_entries()[:3]
    _write_jsonl(path, entries, trailing_newline=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"assistant","uuid":"partial"')
    sessions = read_sessions("claude-code", [str(path.parent)])
    assert len(sessions) == 1
    assert len(sessions[0]["messages"]) == 2

    path.write_text(json.dumps(entries[0]) + "\n{broken}\n" + json.dumps(entries[1]) + "\n", encoding="utf-8")
    with pytest.raises(AgentAdapterError):
        read_sessions("claude-code", [str(path)])


def test_claude_code_partial_and_hook_normalization(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "projects"
    _write_jsonl(root / "-repo" / "good.jsonl", _claude_entries())
    bad = root / "-repo" / "bad.jsonl"
    bad.write_text("{broken}\n", encoding="utf-8")
    with pytest.raises(PartialHistoryError) as exc:
        read_sessions("claude-code", [str(root)])
    assert exc.value.sessions and exc.value.failures

    record = parse_hook("claude-code", {
        "event": "UserPromptSubmit", "session_id": "sid", "prompt_id": "p1",
        "prompt": "continue from Claude Code", "cwd": "/repo",
        "transcript_path": "/tmp/claude/session-9.jsonl", "timestamp": "2026-10-01T10:00:00Z",
    })[0]
    assert record["external_id"] == "claude-code:session-9"
    assert record["messages"][0]["content"] == "continue from Claude Code"


def _opencode_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT, title TEXT,
                               time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        """
    )
    connection.execute("INSERT INTO session VALUES ('ses_main', 'p', NULL, '/repo', 'Session title', 1000, 4000)")
    connection.execute("INSERT INTO session VALUES ('ses_sub', 'p', 'ses_main', '/repo', 'Subagent', 1100, 1200)")
    connection.execute("INSERT INTO message VALUES ('msg_u', 'ses_main', 2000, 2000, ?)",
                       (json.dumps({"role": "user", "time": {"created": 2000}}),))
    connection.execute("INSERT INTO message VALUES ('msg_a', 'ses_main', 3000, 3000, ?)",
                       (json.dumps({"role": "assistant", "time": {"created": 3000}}),))
    connection.execute("INSERT INTO message VALUES ('msg_sub', 'ses_sub', 1100, 1100, ?)",
                       (json.dumps({"role": "user", "time": {"created": 1100}}),))
    parts = [
        ("prt_u", "msg_u", "ses_main", 2000, {"type": "text", "text": "Fix the sync bug"}),
        ("prt_syn", "msg_u", "ses_main", 2001, {"type": "text", "text": "Injected context", "synthetic": True}),
        ("prt_a", "msg_a", "ses_main", 3000, {"type": "text", "text": "Found the duplicate write"}),
        ("prt_tool", "msg_a", "ses_main", 3001, {"type": "tool", "tool": "bash", "state": {"status": "completed", "output": "ok"}}),
        ("prt_step", "msg_a", "ses_main", 3002, {"type": "step-finish"}),
        ("prt_sub", "msg_sub", "ses_sub", 1100, {"type": "text", "text": "Subagent message"}),
    ]
    connection.executemany("INSERT INTO part VALUES (?, ?, ?, ?, ?)", [(a, b, c, d, json.dumps(e)) for a, b, c, d, e in parts])
    connection.commit()
    connection.close()


def test_opencode_reads_sqlite_and_excludes_synthetic_and_subagents(tmp_path: Path) -> None:
    db = tmp_path / "opencode.db"
    _opencode_db(db)
    sessions = read_sessions("opencode", [str(db)])
    assert len(sessions) == 1
    session = sessions[0]
    assert session["external_id"] == "opencode:ses_main"
    assert session["title"] == "Session title"
    assert [item["content"] for item in session["messages"]] == ["Fix the sync bug", "Found the duplicate write"]
    assert "Injected context" not in json.dumps(session, ensure_ascii=False)
    assert "Subagent message" not in json.dumps(session, ensure_ascii=False)
    assert "bash" in session["process_summary"]


def test_opencode_legacy_fallback_and_missing_schema(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    (storage / "session" / "p").mkdir(parents=True)
    (storage / "session" / "p" / "ses_legacy.json").write_text(json.dumps({
        "id": "ses_legacy", "title": "Legacy", "directory": "/repo",
        "time": {"created": 1000, "updated": 2000},
    }), encoding="utf-8")
    (storage / "message" / "ses_legacy").mkdir(parents=True)
    (storage / "message" / "ses_legacy" / "msg_1.json").write_text(json.dumps({
        "id": "msg_1", "role": "user", "time": {"created": 1000},
    }), encoding="utf-8")
    (storage / "part" / "msg_1").mkdir(parents=True)
    (storage / "part" / "msg_1" / "prt_1.json").write_text(json.dumps({"type": "text", "text": "legacy text"}), encoding="utf-8")
    sessions = read_sessions("opencode", [str(tmp_path)])
    assert sessions[0]["messages"][0]["content"] == "legacy text"

    empty = tmp_path / "empty"
    empty.mkdir()
    sqlite3.connect(empty / "opencode.db").close()
    with pytest.raises(AgentAdapterError):
        read_sessions("opencode", [str(empty)])


def test_claude_hook_response_is_silent_and_stop_is_non_blocking() -> None:
    from app.integrations.cc_hook import format_host_response

    response = format_host_response("claude-code", "UserPromptSubmit", {"injection": "x" * 12000})
    assert response == {}
    assert format_host_response("claude-code", "Stop", {"injection": "ignored"}) == {}
