import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import create_schema, db, encode, new_id, now_iso, seed_data, set_app_setting, get_app_setting
from app.main import app
from app.services import connection_service, conversation_service, distillation_service, handoff_service, project_service
from app.services.conversation_service import normalize_iso_timestamp
from app.transport import DistillResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def chronology_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_chronology.db")
        monkeypatch.setenv("CONTEXTCORE_DB", db_path)
        create_schema(Path(db_path))
        seed_data(Path(db_path))

        tokens = {
            "codex": "test-token-codex",
            "omp": "test-token-omp",
            "cursor": "test-token-cursor",
            "antigravity": "test-token-antigravity",
        }
        with db() as conn:
            for agent, tok in tokens.items():
                tok_h = hashlib.sha256(tok.encode("utf-8")).hexdigest()
                conn.execute(
                    "UPDATE connections SET enabled = 1, paused = 0, token_hash = ? WHERE id = ?",
                    (tok_h, agent),
                )

        client = TestClient(
            app,
            base_url="http://127.0.0.1:8420",
            client=("127.0.0.1", 50000),
        )

        sess_res = client.get("/api/session")
        assert sess_res.status_code == 200
        csrf_token = sess_res.json()["csrf_token"]
        headers = {
            "X-CSRF-Token": csrf_token,
        }

        yield {
            "client": client,
            "headers": headers,
            "tokens": tokens,
            "db_path": db_path,
            "tmpdir": tmpdir,
        }


def test_history_ordering_vs_import_order(chronology_env):
    """
    Ingesting conversations out of order must preserve true dialogue chronology.
    list_conversations and handoff predecessor resolution must follow dialogue chronology,
    not ingestion order.
    """
    # 3 sessions with distinct dialogue chronologies
    # Session 1: Oldest (morning)
    t1_start = "2026-10-24T08:00:00Z"
    t1_end = "2026-10-24T08:30:00Z"

    # Session 2: Midday
    t2_start = "2026-10-24T12:00:00Z"
    t2_end = "2026-10-24T12:30:00Z"

    # Session 3: Newest (evening)
    t3_start = "2026-10-24T18:00:00Z"
    t3_end = "2026-10-24T18:30:00Z"

    proj = project_service.create_project(name="Chronology Project", directories=["/workspace/chrono"])
    proj_id = proj["id"]

    # Ingest in reversed order: Newest (3), then Oldest (1), then Middle (2)
    c3 = conversation_service.upsert_conversation(
        agent="codex",
        title="Session 3 Newest",
        external_id="sess-3",
        project_id=proj_id,
        messages=[{"role": "user", "content": "Evening task"}],
        started_at=t3_start,
        updated_at=t3_end,
    )

    c1 = conversation_service.upsert_conversation(
        agent="omp",
        title="Session 1 Oldest",
        external_id="sess-1",
        project_id=proj_id,
        messages=[{"role": "user", "content": "Morning task"}],
        started_at=t1_start,
        updated_at=t1_end,
    )

    c2 = conversation_service.upsert_conversation(
        agent="cursor",
        title="Session 2 Middle",
        external_id="sess-2",
        project_id=proj_id,
        messages=[{"role": "user", "content": "Midday task"}],
        started_at=t2_start,
        updated_at=t2_end,
    )

    # 1. list_conversations must order by updated_at DESC (3, 2, 1) despite import order (3, 1, 2)
    all_convs = conversation_service.list_conversations(project_id=proj_id)
    assert len(all_convs) == 3
    assert [c["id"] for c in all_convs] == [c3["id"], c2["id"], c1["id"]]
    assert all_convs[0]["title"] == "Session 3 Newest"
    assert all_convs[1]["title"] == "Session 2 Middle"
    assert all_convs[2]["title"] == "Session 1 Oldest"

    # 2. Date filters check dialogue chronology
    since_noon = conversation_service.list_conversations(
        project_id=proj_id, since="2026-10-24T10:00:00Z"
    )
    assert [c["id"] for c in since_noon] == [c3["id"], c2["id"]]

    until_afternoon = conversation_service.list_conversations(
        project_id=proj_id, until="2026-10-24T13:00:00Z"
    )
    assert [c["id"] for c in until_afternoon] == [c2["id"], c1["id"]]

    # Multiple predecessors remain explicit choices, ordered by source chronology.
    result = handoff_service.relay_project(project_id=proj_id, consumer="omp", sync_first=False)
    assert result["status"] == "selection_required"
    assert result["must_not_execute"] is True
    assert [candidate["id"] for candidate in result["candidates"]] == [c3["id"], c2["id"]]


def test_idempotent_timestamp_correction(chronology_env):
    """
    Existing imported DB records with legacy/inaccurate timestamps must self-correct
    on resync when source timestamps are provided, even if content_hash is unchanged,
    WITHOUT requeue, version bump, or audit spam.
    """
    # Create conversation with dummy/inaccurate timestamps (simulating older import)
    old_dummy_time = "2026-10-24T23:59:59+00:00"
    messages = [
        {"role": "user", "content": "Discuss architecture principles", "timestamp": ""},
        {"role": "assistant", "content": "Use SQLite WAL and clean boundaries", "timestamp": ""},
    ]

    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Architecture Discussion",
        external_id="conv-arch-sync-1",
        messages=messages,
        started_at=old_dummy_time,
        updated_at=old_dummy_time,
    )
    conv_id = conv["id"]

    # Initial state
    with db() as conn:
        q_count_before = conn.execute(
            "SELECT COUNT(*) as c FROM distillation_queue WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()["c"]
        audit_count_before = conn.execute(
            "SELECT COUNT(*) as c FROM audit_logs WHERE target = ?",
            (conv_id,),
        ).fetchone()["c"]

    assert q_count_before == 1

    # Resync with identical content but authoritative source timestamps
    real_started_at = "2026-10-20T09:00:00Z"
    real_updated_at = "2026-10-20T09:45:00Z"

    resync_res = conversation_service.upsert_conversation(
        agent="omp",
        title="Architecture Discussion",
        external_id="conv-arch-sync-1",
        messages=messages,
        started_at=real_started_at,
        updated_at=real_updated_at,
        incremental=False,
    )

    # Content did not change
    assert resync_res["_changed"] is False

    # Check database timestamps self-corrected to UTC normalized source timestamps
    with db() as conn:
        row = conn.execute("SELECT started_at, updated_at FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        assert row["started_at"] == "2026-10-20T09:00:00+00:00"
        assert row["updated_at"] == "2026-10-20T09:45:00+00:00"

        q_count_after = conn.execute(
            "SELECT COUNT(*) as c FROM distillation_queue WHERE conversation_id = ?",
            (conv_id,),
        ).fetchone()["c"]
        audit_count_after = conn.execute(
            "SELECT COUNT(*) as c FROM audit_logs WHERE target = ?",
            (conv_id,),
        ).fetchone()["c"]

    # No requeue spam!
    assert q_count_after == q_count_before
    # No audit log spam!
    assert audit_count_after == audit_count_before

    # Second resync with same timestamps is completely idempotent
    repeat_res = conversation_service.upsert_conversation(
        agent="omp",
        title="Architecture Discussion",
        external_id="conv-arch-sync-1",
        messages=messages,
        started_at=real_started_at,
        updated_at=real_updated_at,
        incremental=False,
    )
    assert repeat_res["_changed"] is False


@pytest.mark.anyio
async def test_pending_error_compute_not_making_old_sessions_recent(chronology_env):
    """
    Distillation pending or error loops must NOT overwrite conversations.updated_at.
    Processing status timestamp belongs on distillation_queue.updated_at, not conversations.
    Old sessions must never jump to the top of the conversation list during worker execution.
    """
    # 1. Insert an old session (5 days ago)
    old_ts_start = "2026-10-19T08:00:00Z"
    old_ts_end = "2026-10-19T08:30:00Z"
    old_conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Old Legacy Session",
        external_id="sess-old-compute",
        messages=[{"role": "user", "content": "Ancient prompt"}],
        started_at=old_ts_start,
        updated_at=old_ts_end,
    )
    old_id = old_conv["id"]

    # 2. Insert a recent session (yesterday)
    recent_ts_start = "2026-10-23T14:00:00Z"
    recent_ts_end = "2026-10-23T14:30:00Z"
    recent_conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Recent Active Session",
        external_id="sess-recent-compute",
        messages=[{"role": "user", "content": "Recent prompt"}],
        started_at=recent_ts_start,
        updated_at=recent_ts_end,
    )
    recent_id = recent_conv["id"]

    # Verify initial sort order: Recent is first, Old is second
    convs = conversation_service.list_conversations()
    assert convs[0]["id"] == recent_id
    assert convs[1]["id"] == old_id

    # 3. Simulate distillation queue item for old conversation in pending state
    # compute execution not authorized keeps queue item pending
    set_app_setting("compute_authorized", False)
    set_app_setting("compute_host", "omp")

    with db() as conn:
        q_row = conn.execute(
            "SELECT id FROM distillation_queue WHERE conversation_id = ? AND status = 'pending'",
            (old_id,),
        ).fetchone()
        assert q_row is not None
        queue_id = q_row["id"]

    res_pending = await distillation_service.process_distillation_queue_item(queue_id)
    assert res_pending is not None
    assert res_pending.get("status") == "pending"

    # Verify:
    # conversations.updated_at for old_conv is STILL 2026-10-19!
    # distillation_queue.updated_at is updated to now (processing date separated!)
    with db() as conn:
        conv_db = conn.execute("SELECT updated_at, processing_status FROM conversations WHERE id = ?", (old_id,)).fetchone()
        assert conv_db["updated_at"] == "2026-10-19T08:30:00+00:00"
        assert conv_db["processing_status"] == "pending"

        dq_db = conn.execute("SELECT updated_at, status FROM distillation_queue WHERE id = ?", (queue_id,)).fetchone()
        assert dq_db["status"] == "pending"
        # queue updated_at is recent, distinct from dialogue timestamp
        assert dq_db["updated_at"] != conv_db["updated_at"]

    # list_conversations must STILL list Recent first, Old second!
    convs_after_pending = conversation_service.list_conversations()
    assert convs_after_pending[0]["id"] == recent_id
    assert convs_after_pending[1]["id"] == old_id

    # 4. Simulate distillation queue item encountering error
    set_app_setting("compute_authorized", True)
    set_app_setting("compute_host", "omp")

    mock_fail = DistillResult(
        memories=[],
        status="failed",
        error="LLM provider rate limit exceeded",
        usage=None,
    )
    with patch("app.transport.run_distill", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_fail
        res_error = await distillation_service.process_distillation_queue_item(queue_id)
        assert res_error is not None
        assert res_error.get("status") == "error"

    # conversations.updated_at for old_conv must STILL be untouched!
    with db() as conn:
        conv_db_err = conn.execute("SELECT updated_at, processing_status FROM conversations WHERE id = ?", (old_id,)).fetchone()
        assert conv_db_err["updated_at"] == "2026-10-19T08:30:00+00:00"
        assert conv_db_err["processing_status"] == "error"

    # Still correctly sorted
    convs_after_error = conversation_service.list_conversations()
    assert convs_after_error[0]["id"] == recent_id
    assert convs_after_error[1]["id"] == old_id


def test_incremental_hook_timestamp_forward(chronology_env):
    """
    Incremental hook ingestion must advance conversations.updated_at forward with turns,
    preserve started_at, and reject backward timestamp drift on out-of-order events.
    """
    client = chronology_env["client"]
    token = chronology_env["tokens"]["codex"]
    headers = {
        "Authorization": f"Bearer {token}",
        "X-CSRF-Token": chronology_env["headers"]["X-CSRF-Token"],
    }

    sess_id = "codex-stream-sess-100"
    cwd = "/workspace/project_alpha"

    # 1. Initial turn at T0
    t0 = "2026-10-24T10:00:00Z"
    t0_norm = "2026-10-24T10:00:00+00:00"

    p0 = {
        "event": "UserPromptSubmit",
        "session_id": sess_id,
        "prompt": "Implement vector index search",
        "cwd": cwd,
        "timestamp": t0,
    }
    r0 = client.post("/api/connections/codex/hook", json=p0, headers=headers)
    assert r0.status_code == 200

    with db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE external_id = ?", (f"codex:{sess_id}",)).fetchone()
        assert row is not None
        assert row["started_at"] == t0_norm
        assert row["updated_at"] == t0_norm

    # 2. Incremental assistant turn at T1 > T0
    t1 = "2026-10-24T10:05:00Z"
    t1_norm = "2026-10-24T10:05:00+00:00"

    p1 = {
        "event": "Stop",
        "session_id": sess_id,
        "message": "Vector index created using HNSW graph",
        "cwd": cwd,
        "timestamp": t1,
    }
    r1 = client.post("/api/connections/codex/hook", json=p1, headers=headers)
    assert r1.status_code == 200

    with db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE external_id = ?", (f"codex:{sess_id}",)).fetchone()
        assert row["started_at"] == t0_norm
        assert row["updated_at"] == t1_norm

    # 3. Third turn at T2 > T1
    t2 = "2026-10-24T10:12:00Z"
    t2_norm = "2026-10-24T10:12:00+00:00"

    p2 = {
        "event": "UserPromptSubmit",
        "session_id": sess_id,
        "prompt": "Add cosine similarity metric",
        "cwd": cwd,
        "timestamp": t2,
    }
    r2 = client.post("/api/connections/codex/hook", json=p2, headers=headers)
    assert r2.status_code == 200

    with db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE external_id = ?", (f"codex:{sess_id}",)).fetchone()
        assert row["started_at"] == t0_norm
        assert row["updated_at"] == t2_norm

    # 4. Out-of-order or duplicate hook with older timestamp T_old (10:02)
    t_old = "2026-10-24T10:02:00Z"
    p_old = {
        "event": "PostToolUse",
        "session_id": sess_id,
        "tool_name": "ast_grep",
        "output": "Found 3 occurrences",
        "cwd": cwd,
        "timestamp": t_old,
    }
    r_old = client.post("/api/connections/codex/hook", json=p_old, headers=headers)
    assert r_old.status_code == 200

    # updated_at must NOT drift backward! It must remain T2!
    with db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE external_id = ?", (f"codex:{sess_id}",)).fetchone()
        assert row["started_at"] == t0_norm
        assert row["updated_at"] == t2_norm


def test_timezone_normalization_and_consistent_sort(chronology_env):
    """
    Valid known ISO timestamps with offsets (+08:00, -05:00, Z) must normalize to UTC
    so that SQLite string comparison (ORDER BY updated_at DESC, >=, <=) sorts consistently.
    """
    # conv_utc10: 18:00+08:00 is 10:00 UTC
    conv_utc10 = conversation_service.upsert_conversation(
        agent="cursor",
        title="East Asia Session (18:00 +08:00)",
        external_id="conv-tz-10",
        messages=[{"role": "user", "content": "Morning Tokyo"}],
        started_at="2026-10-24T18:00:00+08:00",
        updated_at="2026-10-24T18:15:00+08:00",
    )

    # conv_utc11: 11:00Z is 11:00 UTC
    conv_utc11 = conversation_service.upsert_conversation(
        agent="codex",
        title="London Session (11:00 Z)",
        external_id="conv-tz-11",
        messages=[{"role": "user", "content": "Midday London"}],
        started_at="2026-10-24T11:00:00Z",
        updated_at="2026-10-24T11:15:00Z",
    )

    # conv_utc12: 07:00-05:00 is 12:00 UTC
    conv_utc12 = conversation_service.upsert_conversation(
        agent="omp",
        title="New York Session (07:00 -05:00)",
        external_id="conv-tz-12",
        messages=[{"role": "user", "content": "Morning New York"}],
        started_at="2026-10-24T07:00:00-05:00",
        updated_at="2026-10-24T07:15:00-05:00",
    )

    # In DB: all are UTC
    assert conv_utc10["updated_at"] == "2026-10-24T10:15:00+00:00"
    assert conv_utc11["updated_at"] == "2026-10-24T11:15:00+00:00"
    assert conv_utc12["updated_at"] == "2026-10-24T12:15:00+00:00"

    # Sorting order must be 12:15 UTC > 11:15 UTC > 10:15 UTC
    # (Without normalization, "18:15" would have incorrectly sorted higher than "12:15"!)
    ordered = conversation_service.list_conversations()
    assert [c["id"] for c in ordered] == [conv_utc12["id"], conv_utc11["id"], conv_utc10["id"]]


def test_ui_manual_edit_unaffected(chronology_env):
    """
    UI manual edit via update_conversation updates updated_at to now,
    while started_at is unaffected and audit log is recorded.
    """
    initial_start = "2026-10-20T08:00:00Z"
    initial_end = "2026-10-20T08:30:00Z"

    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Original Title",
        external_id="conv-ui-edit",
        messages=[{"role": "user", "content": "Hello"}],
        started_at=initial_start,
        updated_at=initial_end,
    )
    conv_id = conv["id"]

    updated = conversation_service.update_conversation(
        conversation_id=conv_id,
        title="Renamed by User",
    )

    assert updated["title"] == "Renamed by User"
    assert updated["started_at"] == "2026-10-20T08:00:00+00:00"
    # updated_at was refreshed to current time
    assert updated["updated_at"] > "2026-10-24T00:00:00+00:00"


def test_message_timestamp_fallback_derivation(chronology_env):
    """
    When upsert_conversation is called without explicit started_at / updated_at,
    it derives timestamps from the messages rather than falling back to now.
    Only when timestamps are truly missing does it fall back to now.
    """
    m1_ts = "2026-10-22T08:00:00Z"
    m2_ts = "2026-10-22T08:25:00Z"

    messages = [
        {"role": "user", "content": "Question", "timestamp": m1_ts},
        {"role": "assistant", "content": "Answer", "timestamp": m2_ts},
    ]

    conv = conversation_service.upsert_conversation(
        agent="antigravity",
        title="Derived from Messages",
        external_id="conv-derive-ts",
        messages=messages,
    )

    assert conv["started_at"] == "2026-10-22T08:00:00+00:00"
    assert conv["updated_at"] == "2026-10-22T08:25:00+00:00"

    # When timestamps are truly missing (empty messages, no timestamps)
    conv_no_ts = conversation_service.upsert_conversation(
        agent="antigravity",
        title="Truly Missing Timestamps",
        external_id="conv-missing-ts",
        messages=[{"role": "user", "content": "No timestamp message"}],
    )
    assert conv_no_ts["started_at"] is not None
    assert conv_no_ts["updated_at"] is not None
