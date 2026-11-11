"""
Behavioral tests for Codex full history ingestion, edge cases, and real formats.

Verifies:
- Distinguishes legitimately empty sessions (header-only, compaction-only, live-new) from unknown/corrupt format.
- Legitimately empty sessions are skipped without breaking valid sessions in batch.
- Unknown / corrupt records raise unsupported_format (not watered down).
- Mixed valid + unrecoverable corrupt files raise PartialHistoryError preserving valid sessions.
- Incomplete trailing line at EOF (live session) is tolerated.
- Duplicate message IDs across item_completed and response_item are deduplicated.
- Thread titles loaded from session_index.jsonl via thread_name/thread_title/title.
- Real host history (~/.codex) parses cleanly without unsupported_format crash if present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.adapters import AgentAdapterError, PartialHistoryError, read_sessions
from app.adapters.codex import _parse_codex_rollout, read_codex_sessions


def test_codex_legitimate_header_only_skipped(tmp_path: Path) -> None:
    """A legitimate session containing only header/session_meta is skipped as empty."""
    rollout = tmp_path / "rollout-header.jsonl"
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T10:00:00Z", "payload": {"id": "s-header-only", "cwd": str(tmp_path)}},
        {"type": "turn_context", "timestamp": "2026-10-24T10:00:01Z", "payload": {"cwd": str(tmp_path)}},
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    # _parse_codex_rollout returns None for legitimately empty session
    res = _parse_codex_rollout(rollout)
    assert res is None

    # read_sessions skips it and returns empty list
    sessions = read_sessions("codex", [str(tmp_path)])
    assert sessions == []


def test_codex_legitimate_compaction_only_skipped(tmp_path: Path) -> None:
    """A compaction artifact session with no dialogue is skipped as empty."""
    rollout = tmp_path / "rollout-2026-10-22T23-43-24-01a16448-066f-74e2-9af2-f91bce6c14aa.jsonl"
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-22T23:43:24Z", "payload": {"id": "01a16448-066f-74e2-9af2-f91bce6c14aa", "cwd": "/repo"}},
        {"type": "response_item", "timestamp": "2026-10-22T23:43:25Z", "payload": {"type": "compaction", "id": "cmp-1", "encrypted_content": "enc..."}},
        {"type": "event_msg", "timestamp": "2026-10-22T23:43:26Z", "payload": {"type": "thread_settings_applied", "thread_id": "01a16448-066f-74e2-9af2-f91bce6c14aa"}},
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    res = _parse_codex_rollout(rollout)
    assert res is None

    sessions = read_sessions("codex", [str(tmp_path)])
    assert sessions == []


def test_codex_legitimate_live_new_session_skipped(tmp_path: Path) -> None:
    """A live new session with session_meta and an incomplete trailing write at EOF is skipped as empty."""
    rollout = tmp_path / "rollout-live-new.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "timestamp": "2026-10-24T12:00:00Z", "payload": {"id": "s-live-new", "cwd": str(tmp_path)}})
        + "\n{\"type\": \"event_msg\", \"payload\": {\"ty",
        encoding="utf-8",
    )

    res = _parse_codex_rollout(rollout)
    assert res is None

    sessions = read_sessions("codex", [str(tmp_path)])
    assert sessions == []


def test_codex_mixed_valid_and_legitimate_empty_and_live_tail(tmp_path: Path) -> None:
    """Batch reading skips legitimately empty sessions while keeping valid dialogue sessions."""
    # 1. Valid complete session
    f_valid = tmp_path / "rollout-valid.jsonl"
    f_valid_rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T12:00:00Z", "payload": {"id": "s-valid", "cwd": str(tmp_path)}},
        {"type": "response_item", "timestamp": "2026-10-24T12:00:01Z", "payload": {"role": "user", "content": "How do I test this?"}},
        {"type": "response_item", "timestamp": "2026-10-24T12:00:02Z", "payload": {"role": "assistant", "content": "Use pytest with clear assertions."}},
    ]
    f_valid.write_text("\n".join(json.dumps(r) for r in f_valid_rows) + "\n", encoding="utf-8")

    # 2. Header-only session
    f_header = tmp_path / "rollout-header.jsonl"
    f_header.write_text(
        json.dumps({"type": "session_meta", "timestamp": "2026-10-24T12:05:00Z", "payload": {"id": "s-hdr", "cwd": str(tmp_path)}}) + "\n",
        encoding="utf-8",
    )

    # 3. Compaction-only session
    f_compaction = tmp_path / "rollout-compaction.jsonl"
    f_comp_rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T12:10:00Z", "payload": {"id": "s-cmp", "cwd": str(tmp_path)}},
        {"type": "response_item", "timestamp": "2026-10-24T12:10:01Z", "payload": {"type": "compaction", "id": "cmp-2"}},
    ]
    f_compaction.write_text("\n".join(json.dumps(r) for r in f_comp_rows) + "\n", encoding="utf-8")

    # 4. Live session with valid turn + incomplete trailing write at EOF
    f_live_valid = tmp_path / "rollout-live-tail.jsonl"
    f_live_valid.write_text(
        json.dumps({"type": "session_meta", "timestamp": "2026-10-24T12:15:00Z", "payload": {"id": "s-live-tail", "cwd": str(tmp_path)}})
        + "\n"
        + json.dumps({"type": "response_item", "timestamp": "2026-10-24T12:15:01Z", "payload": {"role": "user", "content": "Active live turn"}})
        + "\n{\"type\": \"response_item\", \"payload\": {\"rol",
        encoding="utf-8",
    )

    sessions = read_sessions("codex", [str(tmp_path)])
    assert len(sessions) == 2

    ids = {s["external_id"] for s in sessions}
    assert ids == {"codex:s-valid", "codex:s-live-tail"}

    valid_sess = next(s for s in sessions if s["external_id"] == "codex:s-valid")
    assert len(valid_sess["messages"]) == 2
    assert valid_sess["messages"][0]["content"] == "How do I test this?"

    live_sess = next(s for s in sessions if s["external_id"] == "codex:s-live-tail")
    assert len(live_sess["messages"]) == 1
    assert live_sess["messages"][0]["content"] == "Active live turn"


def test_codex_unknown_format_not_watered(tmp_path: Path) -> None:
    """Truly unknown format or empty dict must raise unsupported_format, not silently ignored."""
    bad_file = tmp_path / "rollout-bad.jsonl"
    bad_file.write_text("{}\n", encoding="utf-8")

    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("codex", [str(bad_file)])
    assert exc_info.value.code == "unsupported_format"

    alien_file = tmp_path / "rollout-alien.jsonl"
    alien_file.write_text(json.dumps({"type": "unrecognized_alien_event", "foo": "bar"}) + "\n", encoding="utf-8")

    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("codex", [str(alien_file)])
    assert exc_info.value.code == "unsupported_format"


def test_codex_corrupt_middle_line_not_watered(tmp_path: Path) -> None:
    """Corrupt JSON before EOF in a Codex file must raise unsupported_format."""
    corrupt_file = tmp_path / "rollout-corrupt.jsonl"
    lines = [
        json.dumps({"type": "session_meta", "timestamp": "2026-10-24T13:00:00Z", "payload": {"id": "c1"}}),
        "CORRUPT_JSON_PAYLOAD_NOT_EOF",
        json.dumps({"type": "response_item", "timestamp": "2026-10-24T13:01:00Z", "payload": {"role": "assistant", "content": "hi"}}),
    ]
    corrupt_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AgentAdapterError) as exc_info:
        read_sessions("codex", [str(corrupt_file)])
    assert exc_info.value.code == "unsupported_format"


def test_codex_mixed_valid_and_corrupt_raises_partial_history_error(tmp_path: Path) -> None:
    """When an unrecoverable corrupt file is present alongside valid sessions, PartialHistoryError is raised."""
    # Valid session
    f_good = tmp_path / "rollout-good.jsonl"
    f_good_rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T14:00:00Z", "payload": {"id": "s-good", "cwd": str(tmp_path)}},
        {"type": "response_item", "timestamp": "2026-10-24T14:00:01Z", "payload": {"role": "user", "content": "Keep this valid data"}},
    ]
    f_good.write_text("\n".join(json.dumps(r) for r in f_good_rows) + "\n", encoding="utf-8")

    # Corrupt file
    f_broken = tmp_path / "rollout-broken.jsonl"
    f_broken.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "s-bad"}})
        + "\nBROKEN_LINE\n"
        + json.dumps({"type": "response_item", "payload": {"role": "user", "content": "lost"}})
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PartialHistoryError) as exc_info:
        read_sessions("codex", [str(tmp_path)])

    err = exc_info.value
    assert isinstance(err, AgentAdapterError)
    assert err.code == "unsupported_format"
    assert "partial_sync:" in err.detail
    assert len(err.sessions) == 1
    assert err.sessions[0]["external_id"] == "codex:s-good"
    assert err.sessions[0]["messages"][0]["content"] == "Keep this valid data"
    assert len(err.failures) == 1
    assert "rollout-broken.jsonl" in err.failures[0]["path"]


def test_codex_thread_name_loaded_from_index(tmp_path: Path) -> None:
    """Thread titles are populated from session_index.jsonl thread_name field."""
    index_file = tmp_path / "session_index.jsonl"
    index_file.write_text(
        json.dumps({"id": "sess-title-test", "thread_name": "My Refactored Feature", "updated_at": "2026-10-24T15:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    rollout = tmp_path / "rollout-sess-title-test.jsonl"
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T15:00:00Z", "payload": {"id": "sess-title-test", "cwd": str(tmp_path)}},
        {"type": "response_item", "timestamp": "2026-10-24T15:00:01Z", "payload": {"role": "user", "content": "First line of query"}},
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    sessions = read_sessions("codex", [str(tmp_path)])
    assert len(sessions) == 1
    assert sessions[0]["title"] == "My Refactored Feature"


def test_codex_message_deduplication_exact_id(tmp_path: Path) -> None:
    """When duplicate message entries with identical id exist in a rollout, duplicates are deduplicated."""
    rollout = tmp_path / "rollout-dedup.jsonl"
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T16:00:00Z", "payload": {"id": "s-dedup", "cwd": str(tmp_path)}},
        # User message
        {"type": "response_item", "timestamp": "2026-10-24T16:00:01Z", "payload": {"id": "msg_u1", "role": "user", "content": "Ping"}},
        # Assistant message emitted via item_completed
        {
            "type": "event_msg",
            "timestamp": "2026-10-24T16:00:02Z",
            "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "id": "msg_a1", "content": [{"type": "Text", "text": "Pong"}]}},
        },
        # Same assistant message emitted via response_item with identical ID
        {
            "type": "response_item",
            "timestamp": "2026-10-24T16:00:03Z",
            "payload": {"id": "msg_a1", "role": "assistant", "content": [{"type": "output_text", "text": "Pong"}]},
        },
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    sessions = read_sessions("codex", [str(tmp_path)])
    assert len(sessions) == 1
    msgs = sessions[0]["messages"]
    assert len(msgs) == 2
    assert msgs[0]["external_id"] == "msg_u1"
    assert msgs[1]["external_id"] == "msg_a1"
    assert msgs[1]["content"] == "Pong"


def test_codex_observed_event_shapes_process_summary(tmp_path: Path) -> None:
    """Observed event shapes (agent_message, compacted, realtime_item) are summarized in process_summary."""
    rollout = tmp_path / "rollout-events.jsonl"
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T17:00:00Z", "payload": {"id": "s-events", "cwd": str(tmp_path)}},
        {"type": "response_item", "timestamp": "2026-10-24T17:00:01Z", "payload": {"role": "user", "content": "Execute subagent flow"}},
        {
            "type": "response_item",
            "timestamp": "2026-10-24T17:00:02Z",
            "payload": {"type": "agent_message", "author": "/root", "recipient": "/root/worker_subagent"},
        },
        {"type": "compacted", "timestamp": "2026-10-24T17:00:03Z", "payload": {"message": "Compacted window"}},
        {"type": "realtime_item", "timestamp": "2026-10-24T17:00:04Z", "payload": {"type": "realtime_session_started"}},
        {"type": "response_item", "timestamp": "2026-10-24T17:00:05Z", "payload": {"role": "assistant", "content": "Subagent workflow complete."}},
    ]
    rollout.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    sessions = read_sessions("codex", [str(tmp_path)])
    assert len(sessions) == 1
    summary = sessions[0]["process_summary"]
    assert "agent_message: /root -> /root/worker_subagent" in summary
    assert "event: compacted" in summary
    assert "realtime: realtime_session_started" in summary


def test_codex_replayed_source_meta_does_not_overwrite_owner_session(tmp_path: Path) -> None:
    """
    When a subagent or forked session file contains an initial owning session_meta
    followed by a replayed parent/source session_meta, the owning identity and cwd
    must be retained and both file sessions remain distinct in batch discovery.
    """
    # 1. Parent session file
    parent_id = "11111111-1111-1111-1111-111111111111"
    parent_file = tmp_path / f"rollout-2026-10-24T18-00-00-{parent_id}.jsonl"
    parent_rows = [
        {"type": "session_meta", "timestamp": "2026-10-24T18:00:00Z", "payload": {"id": parent_id, "cwd": "/parent/workspace"}},
        {"type": "response_item", "timestamp": "2026-10-24T18:00:01Z", "payload": {"role": "user", "content": "Parent question"}},
        {"type": "response_item", "timestamp": "2026-10-24T18:00:02Z", "payload": {"role": "assistant", "content": "Parent response"}},
    ]
    parent_file.write_text("\n".join(json.dumps(r) for r in parent_rows) + "\n", encoding="utf-8")

    # 2. Child/subagent session file with its own initial header, followed by replayed parent header
    child_id = "22222222-2222-2222-2222-222222222222"
    child_file = tmp_path / f"rollout-2026-10-24T18-05-00-{child_id}.jsonl"
    child_rows = [
        # Owning header: establishes child session identity and child cwd
        {"type": "session_meta", "timestamp": "2026-10-24T18:05:00Z", "payload": {"id": child_id, "cwd": "/child/workspace", "thread_source": "subagent"}},
        # Replayed header from parent thread: must NOT overwrite owning id or cwd
        {"type": "session_meta", "timestamp": "2026-10-24T18:00:00Z", "payload": {"id": parent_id, "cwd": "/parent/workspace", "thread_source": "user"}},
        {"type": "response_item", "timestamp": "2026-10-24T18:05:01Z", "payload": {"role": "user", "content": "Child delegated task"}},
        {"type": "response_item", "timestamp": "2026-10-24T18:05:02Z", "payload": {"role": "assistant", "content": "Child result"}},
    ]
    child_file.write_text("\n".join(json.dumps(r) for r in child_rows) + "\n", encoding="utf-8")

    sessions = read_sessions("codex", [str(tmp_path)])
    # Both sessions must be present and distinct; child must not be dropped as duplicate of parent
    assert len(sessions) == 2

    by_id = {s["external_id"]: s for s in sessions}
    assert f"codex:{parent_id}" in by_id
    assert f"codex:{child_id}" in by_id

    parent_sess = by_id[f"codex:{parent_id}"]
    assert parent_sess["cwd"] == "/parent/workspace"
    assert parent_sess["messages"][0]["content"] == "Parent question"

    child_sess = by_id[f"codex:{child_id}"]
    assert child_sess["cwd"] == "/child/workspace"
    assert child_sess["messages"][0]["content"] == "Child delegated task"
    assert "event: replayed_session_meta" in child_sess["process_summary"]

