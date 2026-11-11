"""
Tests for Antigravity Full History and Ingestion Robustness.

Validates:
- Companion transcript fallback (e.g. transcript.jsonl used when transcript_full.jsonl contains
  an unformatted tool dump in the middle, and vice versa).
- Exact source_path fidelity to the chosen candidate.
- Batch directory partial history reporting via PartialHistoryError when genuine corruption occurs.
- Single explicit file strictness without companion fallback.
- Never tolerating corrupt lines in the middle of a file.
- Live trailing incomplete writes tolerated at EOF.
- UTF-8 BOM handling and full textual dialogue fidelity (>10 KB preserved).
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from app.adapters import (
    AgentAdapterError,
    PartialHistoryError,
    read_sessions,
)


def test_antigravity_companion_fallback_when_full_has_unformatted_line(tmp_path: Path) -> None:
    """
    When transcript_full.jsonl has an unformatted tool output injected in the middle (real Antigravity issue),
    the adapter transparently falls back to companion transcript.jsonl with source_path fidelity.
    """
    session_dir = tmp_path / "brain" / "sess-fallback" / ".system_generated" / "logs"
    session_dir.mkdir(parents=True)

    norm_file = session_dir / "transcript.jsonl"
    full_file = session_dir / "transcript_full.jsonl"

    norm_rows = [
        {"step_index": 1, "type": "USER_INPUT", "content": "Analyze performance bottleneck", "created_at": "2026-10-24T10:00:00Z"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "I inspected the database queries.", "created_at": "2026-10-24T10:00:01Z"},
    ]
    norm_file.write_text("\n".join(json.dumps(r) for r in norm_rows) + "\n", encoding="utf-8")

    # full_file has an invalid line in the middle (line 2 is raw unformatted dump)
    full_lines = [
        json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Analyze performance bottleneck", "created_at": "2026-10-24T10:00:00Z"}),
        r'\n{\"name\":\"migrations\",\"isDir\":true}\n',  # Real observed unformatted tool dump
        json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "I inspected the database queries.", "created_at": "2026-10-24T10:00:01Z"}),
    ]
    full_file.write_text("\n".join(full_lines) + "\n", encoding="utf-8")

    sessions = read_sessions("antigravity", [str(tmp_path)])
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess["external_id"] == "antigravity:sess-fallback"
    assert sess["source_path"] == str(norm_file)
    assert len(sess["messages"]) == 2
    assert sess["messages"][0]["content"] == "Analyze performance bottleneck"
    assert sess["messages"][1]["content"] == "I inspected the database queries."


def test_antigravity_companion_fallback_when_norm_is_corrupt(tmp_path: Path) -> None:
    """
    When transcript.jsonl has corruption in the middle, the adapter successfully
    uses valid companion transcript_full.jsonl.
    """
    session_dir = tmp_path / "brain" / "sess-full-ok" / ".system_generated" / "logs"
    session_dir.mkdir(parents=True)

    norm_file = session_dir / "transcript.jsonl"
    full_file = session_dir / "transcript_full.jsonl"

    norm_lines = [
        json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Query", "created_at": "2026-10-24T10:00:00Z"}),
        "{corrupted middle json}",
        json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Response", "created_at": "2026-10-24T10:00:01Z"}),
    ]
    norm_file.write_text("\n".join(norm_lines) + "\n", encoding="utf-8")

    full_rows = [
        {"step_index": 1, "type": "USER_INPUT", "content": "Query", "created_at": "2026-10-24T10:00:00Z"},
        {"step_index": 2, "type": "VIEW_FILE", "status": "ok", "created_at": "2026-10-24T10:00:01Z", "content": "internal code"},
        {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Response", "created_at": "2026-10-24T10:00:02Z"},
    ]
    full_file.write_text("\n".join(json.dumps(r) for r in full_rows) + "\n", encoding="utf-8")

    sessions = read_sessions("antigravity", [str(tmp_path)])
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess["external_id"] == "antigravity:sess-full-ok"
    assert sess["source_path"] == str(full_file)
    assert len(sess["messages"]) == 2


def test_antigravity_batch_with_genuine_corruption_raises_partial_history(tmp_path: Path) -> None:
    """
    In a batch scan where one session is healthy and another is genuinely corrupt
    (all candidate files have middle corruption), the adapter raises PartialHistoryError
    carrying the valid sessions and sanitized failure metadata.
    """
    # 1. Healthy session
    healthy_dir = tmp_path / "brain" / "sess-healthy" / ".system_generated" / "logs"
    healthy_dir.mkdir(parents=True)
    healthy_rows = [
        {"step_index": 1, "type": "USER_INPUT", "content": "Healthy prompt", "created_at": "2026-10-24T10:00:00Z"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Healthy reply", "created_at": "2026-10-24T10:00:01Z"},
    ]
    (healthy_dir / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in healthy_rows) + "\n", encoding="utf-8")

    # 2. Genuinely corrupt session (both transcript.jsonl and transcript_full.jsonl are corrupt in middle)
    corrupt_dir = tmp_path / "brain" / "sess-corrupt" / ".system_generated" / "logs"
    corrupt_dir.mkdir(parents=True)
    bad_lines = [
        json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Start"}),
        "INVALID_JSON_MIDDLE",
        json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "End"}),
    ]
    (corrupt_dir / "transcript.jsonl").write_text("\n".join(bad_lines) + "\n", encoding="utf-8")
    (corrupt_dir / "transcript_full.jsonl").write_text("\n".join(bad_lines) + "\n", encoding="utf-8")

    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("antigravity", [str(tmp_path)])

    err = exc_info.value
    assert isinstance(err, PartialHistoryError)
    assert err.code == "unsupported_format"
    assert "partial_sync" in err.detail

    # Verify partial sessions preserved
    assert len(err.sessions) == 1
    assert err.sessions[0]["external_id"] == "antigravity:sess-healthy"

    # Verify failure metadata captured without raw dialogue
    assert len(err.failures) == 1
    failure = err.failures[0]
    assert "sess-corrupt" in failure["path"]
    assert failure["code"] == "unsupported_format"
    assert "Invalid JSON at line 2" in failure["detail"]


def test_antigravity_single_explicit_file_strict(tmp_path: Path) -> None:
    """
    Passing a single explicit file root enforces strict parsing without directory companion fallback.
    If the specified file is corrupt, it raises AgentAdapterError immediately.
    """
    session_dir = tmp_path / "brain" / "sess-strict" / ".system_generated" / "logs"
    session_dir.mkdir(parents=True)

    bad_full = session_dir / "transcript_full.jsonl"
    good_norm = session_dir / "transcript.jsonl"

    good_norm.write_text(
        json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Good"}) + "\n" +
        json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Good reply"}) + "\n",
        encoding="utf-8",
    )
    bad_full.write_text(
        json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Good"}) + "\n" +
        "NOT_JSON_LINE\n" +
        json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Good reply"}) + "\n",
        encoding="utf-8",
    )

    # Calling with explicit bad_full file must NOT fall back to good_norm
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("antigravity", [str(bad_full)])

    assert exc_info.value.code == "unsupported_format"
    assert "Invalid JSON at line 2" in exc_info.value.detail


def test_antigravity_live_trailing_write_at_eof_tolerated(tmp_path: Path) -> None:
    """
    An incomplete trailing write at EOF (with or without trailing whitespace/newlines)
    is tolerated as an active live process append.
    """
    session_dir = tmp_path / "brain" / "sess-live" / ".system_generated" / "logs"
    session_dir.mkdir(parents=True)
    f = session_dir / "transcript.jsonl"

    content = (
        json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Run tests"}) + "\n" +
        json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Executing tests..."}) + "\n" +
        '{"step_index": 3, "type": "RUN_COMMAND", "content": "part'  # Incomplete cut-off write at EOF
    )
    f.write_text(content, encoding="utf-8")

    sessions = read_sessions("antigravity", [str(session_dir)])
    assert len(sessions) == 1
    assert len(sessions[0]["messages"]) == 2
    assert sessions[0]["messages"][0]["content"] == "Run tests"


def test_antigravity_utf8_bom_handled(tmp_path: Path) -> None:
    """UTF-8 byte order mark (BOM) is handled transparently on transcript files."""
    session_dir = tmp_path / "brain" / "sess-bom" / ".system_generated" / "logs"
    session_dir.mkdir(parents=True)
    f = session_dir / "transcript.jsonl"

    rows = [
        {"step_index": 1, "type": "USER_INPUT", "content": "BOM test query"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "BOM test answer"},
    ]
    # Write with utf-8-sig to prepend BOM
    raw = "\n".join(json.dumps(r) for r in rows) + "\n"
    f.write_text(raw, encoding="utf-8-sig")

    sessions = read_sessions("antigravity", [str(session_dir)])
    assert len(sessions) == 1
    assert sessions[0]["messages"][0]["content"] == "BOM test query"


def test_antigravity_large_payload_dialogue_fidelity(tmp_path: Path) -> None:
    """Large text payloads (>10KB) preserve complete original text without clipping."""
    session_dir = tmp_path / "brain" / "sess-large" / ".system_generated" / "logs"
    session_dir.mkdir(parents=True)
    f = session_dir / "transcript.jsonl"

    large_prompt = "Large user specification: " + ("X" * 15000)
    large_reply = "Detailed architectural response: " + ("Y" * 15000)

    rows = [
        {"step_index": 1, "type": "USER_INPUT", "content": f"<USER_REQUEST>\n{large_prompt}\n</USER_REQUEST>"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "content": large_reply},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    sessions = read_sessions("antigravity", [str(session_dir)])
    assert len(sessions) == 1
    assert len(sessions[0]["messages"][0]["content"]) > 15000
    assert len(sessions[0]["messages"][1]["content"]) > 15000
    assert sessions[0]["messages"][0]["content"] == large_prompt
    assert sessions[0]["messages"][1]["content"] == large_reply


def test_antigravity_all_corrupt_batch_raises_error(tmp_path: Path) -> None:
    """
    When all session targets in a batch directory fail, PartialHistoryError
    is raised with sessions=[] and all failure records captured.
    """
    for i in range(2):
        s_dir = tmp_path / "brain" / f"sess-bad-{i}" / ".system_generated" / "logs"
        s_dir.mkdir(parents=True)
        bad_lines = [
            json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Start"}),
            "MALFORMED_LINE_CORRUPTION",
            json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "End"}),
        ]
        (s_dir / "transcript.jsonl").write_text("\n".join(bad_lines) + "\n", encoding="utf-8")

    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("antigravity", [str(tmp_path)])

    err = exc_info.value
    assert isinstance(err, PartialHistoryError)
    assert len(err.sessions) == 0
    assert len(err.failures) == 2


def test_antigravity_legitimate_tool_only_session_skipped_in_batch(tmp_path: Path) -> None:
    """
    A legitimately tool-only / system session without user dialogue is skipped cleanly in batch
    without being marked as requiring repair. When parsed as a single explicit file, it raises unsupported_format.
    """
    # 1. Healthy dialogue session
    good_dir = tmp_path / "brain" / "sess-chat" / ".system_generated" / "logs"
    good_dir.mkdir(parents=True)
    good_rows = [
        {"step_index": 1, "type": "USER_INPUT", "content": "Chat query", "created_at": "2026-10-24T10:00:00Z"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Chat reply", "created_at": "2026-10-24T10:00:01Z"},
    ]
    (good_dir / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in good_rows) + "\n", encoding="utf-8")

    # 2. Legitimate tool-only session (valid JSON, standard types, no USER_INPUT text)
    tool_dir = tmp_path / "brain" / "sess-tool-only" / ".system_generated" / "logs"
    tool_dir.mkdir(parents=True)
    tool_rows = [
        {"step_index": 1, "type": "SYSTEM_MESSAGE", "content": "Initializing subagent environment", "created_at": "2026-10-24T10:00:00Z"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-10-24T10:00:01Z", "tool_calls": [{"name": "run_command"}]},
        {"step_index": 3, "type": "RUN_COMMAND", "content": "ls -la", "created_at": "2026-10-24T10:00:02Z"},
        {"step_index": 4, "type": "GENERIC", "content": "Process finished", "created_at": "2026-10-24T10:00:03Z"},
    ]
    tool_file = tool_dir / "transcript.jsonl"
    tool_file.write_text("\n".join(json.dumps(r) for r in tool_rows) + "\n", encoding="utf-8")

    # In batch directory discovery, tool-only session is skipped cleanly (no PartialHistoryError raised)
    sessions = read_sessions("antigravity", [str(tmp_path)])
    assert len(sessions) == 1
    assert sessions[0]["external_id"] == "antigravity:sess-chat"

    # As a single explicit file, it strictly reports no extractable text
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("antigravity", [str(tool_file)])
    assert exc_info.value.code == "unsupported_format"
    assert "no extractable user or planner text" in exc_info.value.detail
