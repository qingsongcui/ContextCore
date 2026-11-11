"""
Behavioral tests for ContextCore Host Adapters.

Tests discovery, path security, symlink containment, OMP JSONL v3 active-branch parsing,
compaction and tool summarization, app-server delegation for Codex, explicit unsupported errors
for Cursor and Antigravity, and hook event normalization.

All fixtures are synthetic public structures; this suite does not touch user files or real host credentials.
NOTE: Per workflow directives, do not run test commands mid-flight; parent agent runs them after integration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch
import pytest

from app.adapters import (
    AgentAdapterError,
    CAPABILITY_MATRIX,
    discover,
    parse_hook,
    read_sessions,
)
from app.adapters.base import validate_path
from app.integrations.constants import SUPPORTED_HOSTS


# --- 1. Discovery Tests ---

def test_discover_empty_home(tmp_path: Path) -> None:
    """When none of the host directories exist, discovery reports discovered=False."""
    connections = discover(home=tmp_path)
    assert len(connections) == len(SUPPORTED_HOSTS)

    conn_map = {c["id"]: c for c in connections}
    for host_id in SUPPORTED_HOSTS:
        assert host_id in conn_map

    for c in connections:
        assert c["discovered"] is False
        assert c["paths"] == []
        assert c["enabled"] is False
        assert c["paused"] is False
        assert c["capabilities"] == CAPABILITY_MATRIX[c["id"]]


def test_discover_populated_home(tmp_path: Path) -> None:
    """When candidate directories exist, discovery detects them without inspecting private files."""
    (tmp_path / ".codex").mkdir(parents=True)
    (tmp_path / ".omp" / "agent" / "sessions").mkdir(parents=True)
    (tmp_path / ".cursor").mkdir(parents=True)
    (tmp_path / ".gemini" / "antigravity").mkdir(parents=True)
    (tmp_path / ".claude" / "projects").mkdir(parents=True)
    (tmp_path / ".local" / "share" / "opencode").mkdir(parents=True)

    connections = discover(home=tmp_path)
    conn_map = {c["id"]: c for c in connections}

    assert conn_map["codex"]["discovered"] is True
    assert any(".codex" in p for p in conn_map["codex"]["paths"])

    assert conn_map["omp"]["discovered"] is True
    assert any(".omp" in p for p in conn_map["omp"]["paths"])

    assert conn_map["cursor"]["discovered"] is True
    assert any(".cursor" in p for p in conn_map["cursor"]["paths"])

    assert conn_map["antigravity"]["discovered"] is True
    assert any(".gemini/antigravity" in p or "antigravity" in p for p in conn_map["antigravity"]["paths"])


# --- 2. Security and Path Validation Tests ---

def test_path_validation_contained(tmp_path: Path) -> None:
    """A valid file within an authorized root (dir or file) is safely resolved."""
    root = tmp_path / "authorized_root"
    root.mkdir()
    target_file = root / "session.jsonl"
    target_file.write_text("{}", encoding="utf-8")

    # Directory root
    resolved = validate_path(target_file, [str(root)])
    assert resolved == target_file.resolve()

    # Exact authorized file root
    resolved_file = validate_path(target_file, [str(target_file)])
    assert resolved_file == target_file.resolve()


def test_validate_file_safety_missing_file(tmp_path: Path) -> None:
    """A missing file raises root_not_granted (not not_installed)."""
    from app.adapters.base import validate_file_safety
    missing_file = tmp_path / "does_not_exist.jsonl"
    with pytest.raises(AgentAdapterError) as exc_info:
        validate_file_safety(missing_file, host="omp")
    assert exc_info.value.code == "root_not_granted"

def test_path_validation_rejects_escape(tmp_path: Path) -> None:
    """Relative parent traversals escaping the root are rejected."""
    root = tmp_path / "authorized_root"
    root.mkdir()
    outside_file = tmp_path / "secret.jsonl"
    outside_file.write_text("{}", encoding="utf-8")

    with pytest.raises(AgentAdapterError) as exc_info:
        validate_path(root / ".." / "secret.jsonl", [str(root)])
    assert exc_info.value.code == "symlink_escape"


def test_path_validation_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink pointing outside the authorized root is strictly rejected."""
    root = tmp_path / "authorized_root"
    root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "target.jsonl"
    outside_file.write_text("{}", encoding="utf-8")

    symlink_file = root / "symlink.jsonl"
    try:
        symlink_file.symlink_to(outside_file)
    except OSError:
        pytest.skip("Symlink creation not supported in this environment")

    with pytest.raises(AgentAdapterError) as exc_info:
        validate_path(symlink_file, [str(root)])
    assert exc_info.value.code == "symlink_escape"


# --- 3. OMP Session v3 Parser Tests ---

def test_omp_session_active_branch_and_tool_summary(tmp_path: Path) -> None:
    """
    Test OMP session parsing with:
    - Version 3 header
    - Branching entries: active leaf is traced back via parentId, ignoring abandoned fork
    - User and assistant dialogues preserved
    - Tool outputs summarized in process_summary without dumping raw stdout
    - Compaction and reset boundary tracked
    """
    root = tmp_path / "omp_sessions"
    root.mkdir()
    session_file = root / "2026-10-24T12-00-00Z_test-session.jsonl"

    entries = [
        # 1. Header line
        {
            "type": "session",
            "version": 3,
            "id": "sess-1234",
            "cwd": "/Users/developer/project-alpha",
            "title": "Alpha Feature Implementation",
            "timestamp": "2026-10-24T12:00:00Z",
        },
        # 2. Root user message
        {
            "type": "message",
            "id": "msg-001",
            "parentId": None,
            "timestamp": "2026-10-24T12:00:01Z",
            "message": {"role": "user", "content": "Please implement the new caching layer."},
        },
        # 3. Assistant reply with tool call
        {
            "type": "message",
            "id": "msg-002",
            "parentId": "msg-001",
            "timestamp": "2026-10-24T12:00:05Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "I will inspect the existing cache module first."},
                    {"type": "toolCall", "name": "read_file", "arguments": {"path": "src/cache.py"}},
                ],
            },
        },
        # 4. Tool result with large stdout (must be summarized, not dumped raw)
        {
            "type": "message",
            "id": "msg-003",
            "parentId": "msg-002",
            "timestamp": "2026-10-24T12:00:06Z",
            "message": {
                "role": "toolResult",
                "toolName": "read_file",
                "content": "Line 1: class Cache:\nLine 2: ... 10,000 lines of raw output ...",
                "isError": False,
            },
        },
        # 5. Abandoned branch fork (diverges from msg-002, does not lead to leaf)
        {
            "type": "message",
            "id": "abandoned-msg",
            "parentId": "msg-002",
            "timestamp": "2026-10-24T12:00:07Z",
            "message": {"role": "assistant", "content": "This response was rewound and discarded."},
        },
        # 6. Compaction entry on active branch
        {
            "type": "compaction",
            "id": "compact-001",
            "parentId": "msg-003",
            "timestamp": "2026-10-24T12:00:10Z",
            "summary": "Compacted initial discussion about cache architecture.",
            "tokensBefore": 15000,
            "firstKeptEntryId": "msg-003",
        },
        # 7. Final active assistant message (the leaf)
        {
            "type": "message",
            "id": "msg-004",
            "parentId": "compact-001",
            "timestamp": "2026-10-24T12:00:15Z",
            "message": {"role": "assistant", "content": "The caching layer has been verified and completed."},
        },
    ]

    with session_file.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")

    sessions = read_sessions("omp", [str(root)])
    assert len(sessions) == 1
    sess = sessions[0]

    assert sess["external_id"] == "omp:sess-1234"
    assert sess["title"] == "Alpha Feature Implementation"
    assert sess["cwd"] == "/Users/developer/project-alpha"
    assert sess["project_name"] == "project-alpha"
    assert sess["started_at"] == "2026-10-24T12:00:00Z"
    assert sess["updated_at"] == "2026-10-24T12:00:15Z"

    # Verify messages on active branch (user and assistant only; abandoned message excluded)
    messages = sess["messages"]
    assert len(messages) == 3
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Please implement the new caching layer."
    assert messages[1]["role"] == "assistant"
    assert "I will inspect the existing cache module first." in messages[1]["content"]
    assert messages[2]["role"] == "assistant"
    assert "The caching layer has been verified and completed." in messages[2]["content"]

    # Verify abandoned message is NOT in messages
    all_content = " ".join(m["content"] for m in messages)
    assert "This response was rewound and discarded." not in all_content

    # Verify process_summary: formatted Markdown string per contract
    summary_str = sess["process_summary"]
    assert isinstance(summary_str, str)
    assert "read_file" in summary_str
    assert "compaction" in summary_str
    assert "10,000 lines" not in summary_str
    assert len(summary_str) <= 2048

def test_omp_session_256b_title_slot(tmp_path: Path) -> None:
    """Test OMP session with a 256-byte fixed title slot at file start."""
    root = tmp_path / "omp_sessions"
    root.mkdir()
    session_file = root / "title_slot_session.jsonl"

    slot_obj = {"type": "title", "title": "Slot Overridden Title"}
    slot_json = json.dumps(slot_obj)
    # Pad with spaces to exactly 255 bytes + newline = 256 bytes
    slot_bytes = slot_json.encode("utf-8")
    assert len(slot_bytes) < 256
    padding = b" " * (255 - len(slot_bytes)) + b"\n"
    padded_slot = slot_bytes + padding
    assert len(padded_slot) == 256

    header_obj = {
        "type": "session",
        "version": 3,
        "id": "slot-sess",
        "cwd": "/path/to/repo",
        "title": "Header Fallback Title",
        "timestamp": "2026-10-24T10:00:00Z",
    }
    header_line = json.dumps(header_obj).encode("utf-8") + b"\n"

    with session_file.open("wb") as f:
        f.write(padded_slot)
        f.write(header_line)

    sessions = read_sessions("omp", [str(root)])
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Slot Overridden Title"


def test_omp_session_unpadded_title_line(tmp_path: Path) -> None:
    """Test OMP session with unpadded title on line 1, followed by session header on line 2."""
    root = tmp_path / "omp_sessions"
    root.mkdir()
    session_file = root / "unpadded_title_session.jsonl"

    lines = [
        json.dumps({"type": "title", "title": "Unpadded Title Line"}),
        json.dumps({
            "type": "session",
            "version": 3,
            "id": "unpadded-sess",
            "cwd": "/workspace",
            "timestamp": "2026-10-24T10:00:00Z",
        }),
        json.dumps({
            "type": "message",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-10-24T10:00:01Z",
            "message": {"role": "user", "content": "Hello ContextCore"},
        }),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sessions = read_sessions("omp", [str(root)])
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Unpadded Title Line"
    assert len(sessions[0]["messages"]) == 1


def test_omp_schema_version_unsupported(tmp_path: Path) -> None:
    """When OMP session version > 3, an explicit schema_version_unsupported error is raised."""
    root = tmp_path / "omp_sessions"
    root.mkdir()
    session_file = root / "future_v4.jsonl"

    header_obj = {
        "type": "session",
        "version": 4,
        "id": "future-sess",
        "cwd": "/path",
        "timestamp": "2026-10-24T10:00:00Z",
    }
    session_file.write_text(json.dumps(header_obj) + "\n", encoding="utf-8")

    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("omp", [str(root)])
    assert exc_info.value.code == "schema_version_unsupported"
    assert "version 4" in exc_info.value.detail


def test_omp_v1_session_migration(tmp_path: Path) -> None:
    """OMP v1 sessions (entries without id/parentId) are migrated in-memory without losing dialogue."""
    root = tmp_path / "omp_v1"
    root.mkdir()
    session_file = root / "v1_session.jsonl"

    v1_entries = [
        {"type": "session", "version": 1, "id": "v1-sess", "cwd": "/repo", "timestamp": "2026-10-24T08:00:00Z"},
        {"type": "message", "message": {"role": "user", "content": "How does v1 migration work?"}},
        {"type": "message", "message": {"role": "assistant", "content": "Entries are chained sequentially in memory."}},
    ]
    with session_file.open("w", encoding="utf-8") as f:
        for e in v1_entries:
            f.write(json.dumps(e) + "\n")

    sessions = read_sessions("omp", [str(session_file)])
    assert len(sessions) == 1
    sess = sessions[0]
    assert len(sess["messages"]) == 2
    assert sess["messages"][0]["content"] == "How does v1 migration work?"
    assert sess["messages"][1]["content"] == "Entries are chained sequentially in memory."
    assert sess["messages"][0]["external_id"] == "omp:v1-sess:v1_entry_2"
    assert sess["messages"][1]["external_id"] == "omp:v1-sess:v1_entry_3"
    assert sess["messages"][0]["external_id"] != sess["messages"][1]["external_id"]


def test_omp_dict_tool_output(tmp_path: Path) -> None:
    """Dictionary tool outputs in toolResult are safely summarized as JSON strings."""
    root = tmp_path / "omp_dict"
    root.mkdir()
    session_file = root / "dict_tool_session.jsonl"

    entries = [
        {"type": "session", "version": 3, "id": "dict-sess", "cwd": "/repo", "timestamp": "2026-10-24T09:00:00Z"},
        {"type": "message", "id": "m1", "parentId": None, "message": {"role": "user", "content": "Run tool"}},
        {
            "type": "message",
            "id": "m2",
            "parentId": "m1",
            "message": {
                "role": "toolResult",
                "toolName": "fetch_data",
                "content": {"status": 200, "items_count": 42},
                "isError": False,
            },
        },
    ]
    with session_file.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    sessions = read_sessions("omp", [str(session_file)])
    assert len(sessions) == 1
    summary = sessions[0]["process_summary"]
    assert "fetch_data" in summary
    assert "items_count" in summary


def test_omp_directory_traversal_surfaces_file_errors(tmp_path: Path) -> None:
    """Directory scanning propagates critical file errors (e.g. truncated) instead of swallowing them."""
    root = tmp_path / "omp_errors"
    root.mkdir()
    huge_line_file = root / "overflow.jsonl"
    with patch("app.adapters.omp.MAX_LINE_LENGTH", 100):
        huge_line_file.write_text('{"type": "session", "version": 3, "long": "' + "A" * 150 + '"}\n', encoding="utf-8")
        with pytest.raises(AgentAdapterError) as exc_info:
            read_sessions("omp", [str(root)])
        assert exc_info.value.code == "truncated"

def test_omp_trailing_partial_line_tolerated(tmp_path: Path) -> None:
    """Live incomplete trailing line at EOF is safely tolerated without failing session."""
    session_file = tmp_path / "live.jsonl"
    valid_lines = [
        json.dumps({"type": "session", "version": 3, "id": "live-1", "title": "Live Session"}),
        json.dumps({"type": "message", "id": "m1", "parentId": None, "message": {"role": "user", "content": "Hello"}}),
    ]
    # Add incomplete trailing write at EOF
    content = "\n".join(valid_lines) + "\n{\"type\": \"message\", \"id\": \"m2\", \"incomplete"
    session_file.write_text(content, encoding="utf-8")
    sessions = read_sessions("omp", [str(session_file)])
    assert len(sessions) == 1
    assert len(sessions[0]["messages"]) == 1
    assert sessions[0]["messages"][0]["content"] == "Hello"


def test_omp_unicode_line_separators_inside_json_stay_one_record(tmp_path: Path) -> None:
    """U+2028 and U+0085 are legal inside a JSON string. They must not split the record."""
    text = "保持\u2028一行\u0085记录"
    session_file = tmp_path / "separators.jsonl"
    lines = [
        json.dumps({"type": "session", "version": 3, "id": "sep-1", "title": "Separators"}),
        json.dumps({"type": "message", "id": "m1", "parentId": None, "message": {"role": "user", "content": text}}, ensure_ascii=False),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raw = session_file.read_bytes()
    assert "\u2028".encode() in raw and "\u0085".encode() in raw
    sessions = read_sessions("omp", [str(session_file)])
    assert sessions[0]["messages"][0]["content"] == text


def test_omp_corrupt_middle_line_raises_unsupported_format(tmp_path: Path) -> None:
    """Corrupt JSON in the middle of an OMP session raises unsupported_format."""
    session_file = tmp_path / "corrupt.jsonl"
    lines = [
        json.dumps({"type": "session", "version": 3, "id": "corrupt-1"}),
        "{this is not valid json}",
        json.dumps({"type": "message", "id": "m1", "parentId": None, "message": {"role": "user", "content": "Hello"}}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("omp", [str(session_file)])
    assert exc_info.value.code == "unsupported_format"


def test_omp_overlapping_roots_deduplication(tmp_path: Path) -> None:
    """Overlapping authorized roots do not cause duplicate session parsing."""
    sub = tmp_path / "nested"
    sub.mkdir()
    session_file = sub / "sess.jsonl"
    lines = [
        json.dumps({"type": "session", "version": 3, "id": "dup-test", "title": "Dedup Test"}),
        json.dumps({"type": "message", "id": "m1", "parentId": None, "message": {"role": "user", "content": "Testing dedup"}}),
    ]
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Pass both parent and sub directory as overlapping roots
    sessions = read_sessions("omp", [str(tmp_path), str(sub)])
    assert len(sessions) == 1
    assert sessions[0]["external_id"] == "omp:dup-test"


# --- 4. Codex Tests ---

def test_codex_local_file_requires_extractable_text(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("codex", [str(rollout)])
    assert exc_info.value.code == "unsupported_format"


def test_codex_local_file_extracts_dialogue(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T00:00:00Z", "payload": {"id": "s1", "cwd": str(tmp_path)}},
        {"type": "response_item", "timestamp": "2026-10-24T00:00:01Z", "payload": {"id": "m1", "role": "user", "type": "message", "content": "本地原话"}},
        {"type": "response_item", "timestamp": "2026-10-24T00:00:02Z", "payload": {"type": "function_call", "name": "bash", "arguments": "SECRET_TOOL_DUMP"}},
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    sessions = read_sessions("codex", [str(tmp_path)])
    assert sessions[0]["messages"][0]["content"] == "本地原话"
    assert "SECRET_TOOL_DUMP" not in sessions[0]["process_summary"]


def test_codex_app_server_delegation() -> None:
    """read_sessions('codex', ...) delegates to transport app-server client and forwards allow_execution."""
    mock_sessions = [
        {
            "external_id": "codex:thread-123",
            "title": "Refactoring Auth",
            "cwd": "/repo",
            "project_name": "repo",
            "started_at": "2026-10-24T09:00:00Z",
            "updated_at": "2026-10-24T09:30:00Z",
            "messages": [
                {"external_id": "codex:thread-123:turn-1:user", "role": "user", "content": "Refactor tokens", "timestamp": "2026-10-24T09:00:00Z"},
            ],
            "process_summary": "- **auth_refactor**: ok",
            "source_path": "codex://thread/thread-123",
        }
    ]

    with patch("app.transport.fetch_codex_sessions_sync", return_value=mock_sessions) as mock_fetch:
        result = read_sessions("codex", ["/repo"], allow_execution=True)
        assert len(result) == 1
        assert result[0]["external_id"] == "codex:thread-123"
        mock_fetch.assert_called_once_with(["/repo"], allow_execution=True)

def test_codex_item_completed_and_text_fidelity(tmp_path: Path) -> None:
    """Codex parser supports item_completed format and preserves full text without 4000-char truncation."""
    rollout = tmp_path / "rollout-2026-10-24T12-00-00-01a14e0e-a7a0-7241-865b-e51ebb2fcce4.jsonl"
    long_user_text = "用户输入的长文本指令: " + ("A" * 5500)
    long_asst_text = "助手回复的长文本解析: " + ("B" * 5500)

    rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T12:00:00Z", "payload": {"id": "01a14e0e-a7a0-7241-865b-e51ebb2fcce4", "cwd": "/repo"}},
        {"type": "turn_context", "payload": {"model": "gpt-5", "cwd": "/repo"}},
        {
            "type": "event_msg",
            "timestamp": "2026-10-24T12:00:01Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "id": "item-user-1",
                    "content": [{"type": "text", "text": long_user_text}],
                },
            },
        },
        {
            "type": "event_msg",
            "timestamp": "2026-10-24T12:00:05Z",
            "payload": {
                "type": "item_completed",
                "item": {
                    "type": "AgentMessage",
                    "id": "item-agent-1",
                    "content": [{"type": "text", "text": long_asst_text}],
                },
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    sessions = read_sessions("codex", [str(tmp_path)])
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess["external_id"] == "codex:01a14e0e-a7a0-7241-865b-e51ebb2fcce4"
    assert sess["cwd"] == "/repo"
    # Check text fidelity - no clipping at 4000!
    assert sess["messages"][0]["content"] == long_user_text
    assert len(sess["messages"][0]["content"]) > 5000
    assert sess["messages"][1]["content"] == long_asst_text
    assert len(sess["messages"][1]["content"]) > 5000


def test_codex_trailing_line_tolerated(tmp_path: Path) -> None:
    """Active Codex rollout with incomplete trailing line at EOF is parsed safely."""
    rollout = tmp_path / "rollout-2026-10-24T14-00-00-11111111-2222-3333-4444-555555555555.jsonl"
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T14:00:00Z", "payload": {"id": "11111111-2222-3333-4444-555555555555"}},
        {"type": "response_item", "timestamp": "2026-10-24T14:00:01Z", "payload": {"role": "user", "content": "Valid user query"}},
    ]
    content = "\n".join(json.dumps(r) for r in rows) + "\n{\"type\": \"response_item\", \"payload\": {\"role\": \"assistant\", \"con"
    rollout.write_text(content, encoding="utf-8")
    sessions = read_sessions("codex", [str(rollout)])
    assert len(sessions) == 1
    assert sessions[0]["messages"][0]["content"] == "Valid user query"


def test_codex_corrupt_middle_line_raises(tmp_path: Path) -> None:
    """Corrupt JSON in the middle of a Codex file raises unsupported_format."""
    rollout = tmp_path / "rollout-corrupt.jsonl"
    lines = [
        json.dumps({"type": "session_meta", "payload": {"id": "c1"}}),
        "{malformed line here",
        json.dumps({"type": "response_item", "payload": {"role": "user", "content": "hi"}}),
    ]
    rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("codex", [str(rollout)])
    assert exc_info.value.code == "unsupported_format"


def test_codex_ignores_non_codex_jsonl(tmp_path: Path) -> None:
    """Scanning a directory ignores non-Codex arbitrary JSONL files."""
    random_file = tmp_path / "dataset.jsonl"
    random_file.write_text(json.dumps({"key": "value"}) + "\n", encoding="utf-8")
    # With no allow_execution and no valid rollout files, raises root_not_granted
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("codex", [str(tmp_path)], allow_execution=False)
    assert exc_info.value.code == "root_not_granted"

# --- 5. Cursor & Antigravity History Tests ---

def test_cursor_missing_root_raises_error() -> None:
    """Calling read_sessions for Cursor with non-existent root raises root_not_granted."""
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("cursor", ["/non/existent/path/state.vscdb"])
    assert exc_info.value.host == "cursor"
    assert exc_info.value.code == "root_not_granted"


def test_cursor_db_missing_required_tables_raises_unsupported_format(tmp_path: Path) -> None:
    """SQLite DB missing composerHeaders/cursorDiskKV raises unsupported_format."""
    db_file = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE other_table (id TEXT)")
    conn.close()
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("cursor", [str(db_file)])
    assert exc_info.value.host == "cursor"
    assert exc_info.value.code == "unsupported_format"


def test_cursor_db_empty_messages_raises_unsupported_format(tmp_path: Path) -> None:
    """SQLite DB with composer tables but no dialogues raises unsupported_format."""
    db_file = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT, value TEXT, isSubagent INTEGER, createdAt INTEGER, lastUpdatedAt INTEGER)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")
    conn.close()
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("cursor", [str(db_file)])
    assert exc_info.value.host == "cursor"
    assert exc_info.value.code == "unsupported_format"


def test_cursor_state_vscdb_full_extraction(tmp_path: Path) -> None:
    """
    Test Cursor state.vscdb parsing:
    - Top-level composer with workspaceIdentifier cwd and name
    - Subagent composer (isSubagent=1) excluded
    - User message with text > 4000 chars preserved (no clipping)
    - Assistant message with Lexical richText AST extracted to plain text
    - Tool bubble summarized in process_summary without secret leaks
    - Stable session external_id and message external_id
    """
    db_file = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE composerHeaders (composerId TEXT, value TEXT, isSubagent INTEGER, createdAt INTEGER, lastUpdatedAt INTEGER)")
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT, value TEXT)")

    long_prompt = "User prompt for Cursor: " + ("C" * 5200)
    cid = "915dcaa9-a253-4f2b-b782-462f9c6ef33e"
    sub_cid = "subagent-1111-2222-3333-444444444444"

    # Insert headers
    header_val = json.dumps({
        "name": "Cursor Feature Implementation",
        "workspaceIdentifier": {"uri": {"fsPath": "/Users/developer/my-project"}},
        "createdAt": 1700000000000,
        "lastUpdatedAt": 1700000010000,
    })
    conn.execute(
        "INSERT INTO composerHeaders (composerId, value, isSubagent, createdAt, lastUpdatedAt) VALUES (?, ?, ?, ?, ?)",
        (cid, header_val, 0, 1700000000000, 1700000010000),
    )
    conn.execute(
        "INSERT INTO composerHeaders (composerId, value, isSubagent, createdAt, lastUpdatedAt) VALUES (?, ?, ?, ?, ?)",
        (sub_cid, json.dumps({"name": "Subagent"}), 1, 1700000000000, 1700000010000),
    )

    # Insert bubbles for active composer
    b1 = json.dumps({
        "type": 1,
        "bubbleId": "b1",
        "text": long_prompt,
        "createdAt": "2026-10-24T10:00:00Z",
    })
    # Bubble 2: Lexical richText
    rich_text_ast = {
        "root": {
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {"type": "text", "text": "First line of reply."},
                        {"type": "linebreak"},
                        {"type": "text", "text": "Second line of reply."},
                    ],
                }
            ]
        }
    }
    b2 = json.dumps({
        "type": 2,
        "bubbleId": "b2",
        "richText": json.dumps(rich_text_ast),
        "createdAt": "2026-10-24T10:00:05Z",
    })
    # Bubble 3: tool bubble
    b3 = json.dumps({
        "type": 2,
        "bubbleId": "b3",
        "capabilityType": "editFile",
        "createdAt": "2026-10-24T10:00:06Z",
    })
    # Bubble for subagent composer (should be skipped)
    b_sub = json.dumps({
        "type": 1,
        "bubbleId": "bsub1",
        "text": "Subagent chatter",
        "createdAt": "2026-10-24T10:00:00Z",
    })

    conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (f"bubbleId:{cid}:b1", b1))
    conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (f"bubbleId:{cid}:b2", b2))
    conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (f"bubbleId:{cid}:b3", b3))
    conn.execute("INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)", (f"bubbleId:{sub_cid}:bsub1", b_sub))
    conn.commit()
    conn.close()

    sessions = read_sessions("cursor", [str(tmp_path)])
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess["external_id"] == f"cursor:{cid}"
    assert sess["title"] == "Cursor Feature Implementation"
    assert sess["cwd"] == "/Users/developer/my-project"
    assert len(sess["messages"]) == 2
    assert sess["messages"][0]["external_id"] == f"cursor:{cid}:b1"
    assert sess["messages"][0]["role"] == "user"
    assert sess["messages"][0]["content"] == long_prompt
    assert len(sess["messages"][0]["content"]) > 5000
    assert sess["messages"][1]["role"] == "assistant"
    assert sess["messages"][1]["content"] == "First line of reply.\nSecond line of reply."
    assert "editFile" in sess["process_summary"]


def test_antigravity_transcript_extracts_observed_text(tmp_path: Path) -> None:
    """Antigravity transcript extracts dialogue, fixes the 'logs' session_id bug, and preserves privacy."""
    transcript = tmp_path / "brain" / "conv" / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        {"step_index": 1, "type": "USER_INPUT", "status": "ok", "created_at": "2026-10-24T00:00:00Z", "content": "用户原话"},
        {"step_index": 2, "type": "VIEW_FILE", "status": "ok", "created_at": "2026-10-24T00:00:01Z", "content": "SECRET_TOOL_DUMP"},
        {"step_index": 3, "type": "PLANNER_RESPONSE", "status": "ok", "created_at": "2026-10-24T00:00:02Z", "content": "助手回复"},
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    sessions = read_sessions("antigravity", [str(tmp_path)])
    assert len(sessions) == 1
    # Verify session ID bug is fixed: identity is conv, NOT logs!
    assert sessions[0]["external_id"] == "antigravity:conv"
    assert sessions[0]["title"] == "用户原话"
    assert sessions[0]["messages"][0]["external_id"] == "conv:1"
    assert [item["content"] for item in sessions[0]["messages"]] == ["用户原话", "助手回复"]
    assert "SECRET_TOOL_DUMP" not in sessions[0]["process_summary"]


def test_antigravity_user_request_unwrap(tmp_path: Path) -> None:
    """Antigravity <USER_REQUEST> wrapper is unwrapped to inner plain text."""
    transcript = tmp_path / "brain" / "sess-wrap" / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        {
            "step_index": 1,
            "type": "USER_INPUT",
            "created_at": "2026-10-24T00:00:00Z",
            "content": "<CONTEXT>system context</CONTEXT>\n<USER_REQUEST>\nPlease optimize this SQL query\n</USER_REQUEST>",
        },
        {"step_index": 2, "type": "PLANNER_RESPONSE", "created_at": "2026-10-24T00:00:01Z", "content": "Here is the optimization."},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    sessions = read_sessions("antigravity", [str(transcript)])
    assert len(sessions) == 1
    assert sessions[0]["messages"][0]["content"] == "Please optimize this SQL query"
    assert sessions[0]["title"] == "Please optimize this SQL query"


def test_antigravity_text_fidelity_no_clipping(tmp_path: Path) -> None:
    """Antigravity dialogue preserves full text without artificial 4000-char truncation."""
    transcript = tmp_path / "brain" / "sess-long" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    long_text = "用户输入超长文本: " + ("D" * 5300)
    rows = [
        {"step_index": 1, "type": "USER_INPUT", "created_at": "2026-10-24T00:00:00Z", "content": long_text},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "created_at": "2026-10-24T00:00:01Z", "content": "回复: " + ("E" * 5300)},
    ]
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    sessions = read_sessions("antigravity", [str(transcript)])
    assert len(sessions[0]["messages"][0]["content"]) > 5000
    assert len(sessions[0]["messages"][1]["content"]) > 5000


def test_antigravity_trailing_line_tolerated(tmp_path: Path) -> None:
    """Active Antigravity transcript with incomplete trailing line at EOF is tolerated."""
    transcript = tmp_path / "brain" / "sess-live" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    rows = [
        {"step_index": 1, "type": "USER_INPUT", "created_at": "2026-10-24T00:00:00Z", "content": "Live prompt"},
    ]
    content = "\n".join(json.dumps(r) for r in rows) + "\n{\"step_index\": 2, \"type\": \"PLANNER_RESPONSE\", \"content\": \"incom"
    transcript.write_text(content, encoding="utf-8")
    sessions = read_sessions("antigravity", [str(transcript)])
    assert len(sessions) == 1
    assert sessions[0]["messages"][0]["content"] == "Live prompt"


def test_antigravity_corrupt_middle_line_raises(tmp_path: Path) -> None:
    """Corrupt JSON in the middle of an Antigravity file raises unsupported_format."""
    transcript = tmp_path / "brain" / "sess-bad" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    lines = [
        json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "Hi"}),
        "{not json at all}",
        json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Hello"}),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("antigravity", [str(transcript)])
    assert exc_info.value.code == "unsupported_format"


def test_antigravity_prefer_full_transcript_dedup(tmp_path: Path) -> None:
    """When both transcript.jsonl and transcript_full.jsonl exist, prefers full and deduplicates."""
    session_dir = tmp_path / "brain" / "sess-dedup" / ".system_generated" / "logs"
    session_dir.mkdir(parents=True)
    short_t = session_dir / "transcript.jsonl"
    full_t = session_dir / "transcript_full.jsonl"
    short_rows = [{"step_index": 1, "type": "USER_INPUT", "content": "Short input"}]
    full_rows = [
        {"step_index": 1, "type": "USER_INPUT", "content": "Full input"},
        {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Full response"},
    ]
    short_t.write_text("\n".join(json.dumps(r) for r in short_rows) + "\n", encoding="utf-8")
    full_t.write_text("\n".join(json.dumps(r) for r in full_rows) + "\n", encoding="utf-8")
    sessions = read_sessions("antigravity", [str(tmp_path)])
    assert len(sessions) == 1
    assert len(sessions[0]["messages"]) == 2
    assert sessions[0]["messages"][0]["content"] == "Full input"
# --- 6. Hook Normalization Tests ---

def test_codex_hook_normalization() -> None:
    """Normalizes official Codex hook payloads as well as CLI wrapper payloads."""
    # 1. Official event name
    prompt_payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "sess-codex-1",
        "turn_id": "turn-1",
        "prompt": "Fix unit test in test_adapters.py",
        "cwd": "/workspace",
        "timestamp": "2026-10-24T14:00:00Z",
    }
    records = parse_hook("codex", prompt_payload)
    assert len(records) == 1
    assert records[0]["event"] == "UserPromptSubmit"
    assert len(records[0]["messages"]) == 1
    assert records[0]["messages"][0]["content"] == "Fix unit test in test_adapters.py"
    assert records[0]["messages"][0]["role"] == "user"

    # 2. CLI wrapper event name and nested payload
    wrapped_stop_payload = {
        "event": "stop",
        "payload": {
            "session_id": "sess-codex-1",
            "turn_id": "turn-1",
            "last_assistant_message": "Tests are now green.",
            "timestamp": "2026-10-24T14:01:00Z",
        },
    }
    records = parse_hook("codex", wrapped_stop_payload)
    assert records[0]["event"] == "Stop"
    assert records[0]["messages"][0]["content"] == "Tests are now green."
    assert records[0]["messages"][0]["role"] == "assistant"


def test_cursor_hook_normalization() -> None:
    """Normalizes official Cursor hook payloads and wrapped payloads with limitations."""
    wrapped_prompt = {
        "event": "prompt",
        "payload": {
            "conversation_id": "cursor-conv-1",
            "generation_id": "gen-1",
            "prompt": "Optimize database queries",
            "timestamp": "2026-10-24T14:00:00Z",
        },
    }
    records = parse_hook("cursor", wrapped_prompt)
    assert len(records) == 1
    assert records[0]["event"] == "beforeSubmitPrompt"
    assert records[0]["messages"][0]["role"] == "user"
    assert records[0]["messages"][0]["content"] == "Optimize database queries"
    limitations = records[0]["metadata"]["limitations"]
    assert any("cannot inject developer context" in l for l in limitations)


def test_antigravity_hook_normalization() -> None:
    """Normalizes official Antigravity hook payloads and documents lack of prompt text."""
    session_payload = {
        "event": "session-start",
        "payload": {
            "conversationId": "ag-conv-99",
            "transcriptPath": "/data/brain/ag-conv-99/transcript.jsonl",
            "timestamp": "2026-10-24T14:00:00Z",
        },
    }
    records = parse_hook("antigravity", session_payload)
    assert len(records) == 1
    assert records[0]["event"] == "SessionStart"
    assert records[0]["session_id"] == "ag-conv-99"
    limitations = records[0]["metadata"]["limitations"]
    assert any("does not include user prompt text" in l for l in limitations)


def test_omp_hook_normalization() -> None:
    """Normalizes OMP extension in-process events."""
    event = {
        "event": "intent_detected",
        "session_id": "omp-intent-1",
        "prompt": "Continue working on adapters",
        "timestamp": "2026-10-24T15:00:00Z",
    }
    records = parse_hook("omp", event)
    assert len(records) == 1
    assert records[0]["event"] == "intent_detected"
    assert records[0]["messages"][0]["content"] == "Continue working on adapters"
    assert records[0]["messages"][0]["role"] == "user"


def test_antigravity_cli_session_does_not_collapse_into_desktop(tmp_path: Path) -> None:
    rows = [{"step_index": 1, "type": "USER_INPUT", "status": "ok", "created_at": "2026-10-24T00:00:00Z", "content": "同一目录名"}]
    for root in ("antigravity", "antigravity-cli"):
        transcript = tmp_path / root / "brain" / "conv" / "transcript.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    sessions = read_sessions("antigravity", [str(tmp_path / "antigravity"), str(tmp_path / "antigravity-cli")])
    assert {item["external_id"] for item in sessions} == {"antigravity:conv", "antigravity:cli:conv"}


def test_unknown_host_error() -> None:
    """An unknown host raises not_installed error."""
    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("non_existent_host", ["/path"])
    assert exc_info.value.code == "not_installed"


def test_antigravity_hook_surface_external_id_and_prompt_extraction() -> None:
    """Antigravity hook external_id distinguishes desktop, CLI, and IDE surfaces and extracts prompt text if present."""
    cli_hook = parse_hook(
        "antigravity",
        {
            "eventName": "PreInvocation",
            "payload": {
                "conversationId": "conv-same",
                "transcriptPath": "/Users/dev/.gemini/antigravity-cli/brain/conv-same/transcript.jsonl",
                "workspacePaths": ["/Users/dev/repo"],
                "prompt": "<USER_REQUEST>\n接着做\n</USER_REQUEST>",
            },
        },
    )[0]
    ide_hook = parse_hook(
        "antigravity",
        {
            "eventName": "PreInvocation",
            "payload": {
                "conversationId": "conv-same",
                "artifactDirectoryPath": "/Users/dev/.gemini/antigravity-ide/brain/conv-same",
            },
        },
    )[0]
    desktop_hook = parse_hook(
        "antigravity",
        {
            "eventName": "PreInvocation",
            "payload": {
                "conversationId": "conv-same",
                "transcriptPath": "/Users/dev/.gemini/antigravity/brain/conv-same/transcript.jsonl",
            },
        },
    )[0]
    assert cli_hook["external_id"] == "antigravity:cli:conv-same"
    assert ide_hook["external_id"] == "antigravity:ide:conv-same"
    assert desktop_hook["external_id"] == "antigravity:conv-same"
    assert cli_hook["cwd"] == "/Users/dev/repo"
    assert len(cli_hook["messages"]) == 1
    assert cli_hook["messages"][0]["role"] == "user"
    assert cli_hook["messages"][0]["content"] == "接着做"
    assert ide_hook["messages"] == []


def test_cursor_cli_store_db_protobuf_reader(tmp_path: Path) -> None:
    """Reads Cursor CLI chats/<hash>/<uuid>/store.db SQLite with hex meta and protobuf root blob."""
    chat_dir = tmp_path / "chats" / "88f29e0ce76d4f7b1a91f1cfb272a7ac" / "26a0267f-b6fc-4fd6-a9b2-cbba631676db"
    chat_dir.mkdir(parents=True)
    store_file = chat_dir / "store.db"

    msg_sys = json.dumps({
        "id": "m0",
        "role": "system",
        "content": "Workspace Path: /Users/dev/cursor-cli-project\nSystem instructions",
    }).encode("utf-8")
    msg_user = json.dumps({
        "id": "m1",
        "role": "user",
        "content": [{"type": "text", "text": "<user_query>\n请检查 store.db 解析器\n</user_query>"}],
    }).encode("utf-8")
    msg_asst = json.dumps({
        "id": "m2",
        "role": "assistant",
        "content": [
            {"type": "text", "text": "已完成 store.db 解析。"},
            {"type": "tool-call", "toolCallId": "tc-1", "toolName": "readFile", "args": {"path": "cursor.py"}},
        ],
    }).encode("utf-8")

    import hashlib
    b0_id = hashlib.sha256(msg_sys).hexdigest()
    b1_id = hashlib.sha256(msg_user).hexdigest()
    b2_id = hashlib.sha256(msg_asst).hexdigest()

    # Build protobuf root blob: repeated field 1 (tag 0x0a, length 0x20, 32-byte digest)
    root_pb = b"".join(b"\x0a\x20" + bytes.fromhex(h) for h in (b0_id, b1_id, b2_id))
    root_id = hashlib.sha256(root_pb).hexdigest()

    meta_json = json.dumps({
        "agentId": "26a0267f-b6fc-4fd6-a9b2-cbba631676db",
        "latestRootBlobId": root_id,
        "name": "Cursor CLI Protobuf Session",
        "mode": "default",
        "createdAt": 1775229110350,
    }).encode("utf-8").hex()

    conn = sqlite3.connect(store_file)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('0', ?)", (meta_json,))
    for bid, payload in ((root_id, root_pb), (b0_id, msg_sys), (b1_id, msg_user), (b2_id, msg_asst)):
        conn.execute("INSERT INTO blobs (id, data) VALUES (?, ?)", (bid, payload))
    conn.commit()
    conn.close()

    sessions = read_sessions("cursor", [str(tmp_path / "chats")])
    assert len(sessions) == 1
    sess = sessions[0]
    assert sess["external_id"] == "cursor:cli:26a0267f-b6fc-4fd6-a9b2-cbba631676db"
    assert sess["title"] == "Cursor CLI Protobuf Session"
    assert sess["cwd"] == "/Users/dev/cursor-cli-project"
    assert [m["role"] for m in sess["messages"]] == ["user", "assistant"]
    assert sess["messages"][0]["content"] == "请检查 store.db 解析器"
    assert sess["messages"][1]["content"] == "已完成 store.db 解析。"
    assert "readFile" in sess["process_summary"]


def test_antigravity_hook_reads_prompt_from_transcript_file_when_payload_lacks_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When Antigravity hook payload only carries transcriptPath (no prompt field), user utterance is read from the transcript file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    transcript = tmp_path / ".gemini" / "antigravity-ide" / "brain" / "conv-disk" / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({
            "step_index": 1,
            "type": "USER_INPUT",
            "status": "ok",
            "created_at": "2026-10-25T10:00:00Z",
            "cwd": "/Users/dev/ide-project",
            "content": "<USER_REQUEST>\n接着做\n</USER_REQUEST>",
        }) + "\n",
        encoding="utf-8",
    )
    hook_record = parse_hook(
        "antigravity",
        {
            "event": "PreInvocation",
            "payload": {
                "conversationId": "conv-disk",
                "transcriptPath": str(transcript),
                "timestamp": "2026-10-25T10:00:01Z",
            },
        },
    )[0]
    assert hook_record["external_id"] == "antigravity:ide:conv-disk"
    assert hook_record["source_path"] == str(transcript)
    assert hook_record["cwd"] == "/Users/dev/ide-project"
    assert len(hook_record["messages"]) == 1
    assert hook_record["messages"][0]["role"] == "user"
    assert hook_record["messages"][0]["content"] == "接着做"


def test_cursor_cli_store_db_binary_protobuf_message_blobs(tmp_path: Path) -> None:
    """Reads Cursor CLI store.db when message blobs are binary protobuf messages rather than plain UTF-8 JSON."""
    import hashlib

    chat_dir = tmp_path / "chats" / "hash2" / "uuid-proto-bin"
    chat_dir.mkdir(parents=True)
    store_file = chat_dir / "store.db"

    def lp_bytes(field_no: int, raw: bytes) -> bytes:
        tag = (field_no << 3) | 2
        length = len(raw)
        len_bytes = bytearray()
        while True:
            b = length & 0x7F
            length >>= 7
            if length:
                len_bytes.append(b | 0x80)
            else:
                len_bytes.append(b)
                break
        return bytes([tag]) + bytes(len_bytes) + raw

    user_pb = lp_bytes(1, b"user") + lp_bytes(2, "<user_query>\n接着做 protobuf 解析\n</user_query>".encode("utf-8"))
    asst_pb = lp_bytes(1, b"assistant") + lp_bytes(2, "已支持二进制 protobuf 消息块。".encode("utf-8"))
    b1_id = hashlib.sha256(user_pb).hexdigest()
    b2_id = hashlib.sha256(asst_pb).hexdigest()

    # Nested DAG node in field 2 wrapping field 3 digest refs
    nested_refs = lp_bytes(3, bytes.fromhex(b1_id)) + lp_bytes(3, bytes.fromhex(b2_id))
    root_pb = lp_bytes(2, nested_refs)
    root_id = hashlib.sha256(root_pb).hexdigest()

    meta_hex = json.dumps({
        "agentId": "uuid-proto-bin",
        "latestRootBlobId": root_id,
        "name": "Binary Protobuf Chat",
        "createdAt": 1775229110350,
    }).encode("utf-8").hex()

    conn = sqlite3.connect(store_file)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("INSERT INTO meta (key, value) VALUES ('0', ?)", (meta_hex,))
    for bid, blob in ((root_id, root_pb), (b1_id, user_pb), (b2_id, asst_pb)):
        conn.execute("INSERT INTO blobs (id, data) VALUES (?, ?)", (bid, blob))
    conn.commit()
    conn.close()

    sessions = read_sessions("cursor", [str(tmp_path / "chats")])
    assert len(sessions) == 1
    assert sessions[0]["external_id"] == "cursor:cli:uuid-proto-bin"
    assert [m["role"] for m in sessions[0]["messages"]] == ["user", "assistant"]
    assert sessions[0]["messages"][0]["content"] == "接着做 protobuf 解析"
    assert sessions[0]["messages"][1]["content"] == "已支持二进制 protobuf 消息块。"


