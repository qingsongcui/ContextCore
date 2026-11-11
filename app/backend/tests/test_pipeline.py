import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import create_schema, db, encode, new_id, now_iso, row_to_dict, seed_data, set_app_setting
from app.main import app
from app.services import connection_service, conversation_service, distillation_service, project_service
from app.transport import DistillResult

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def pipeline_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_pipeline.db")
        monkeypatch.setenv("CONTEXTCORE_DB", db_path)
        home_path = Path(tmpdir) / "test_home"
        home_path.mkdir(parents=True, exist_ok=True)
        create_schema(Path(db_path))
        seed_data(Path(db_path))
        set_app_setting("compute_host", "omp")
        set_app_setting("compute_authorized", True)

        client = TestClient(
            app,
            base_url="http://127.0.0.1:8420",
            client=("127.0.0.1", 50000),
        )

        sess_res = client.get("/api/session")
        assert sess_res.status_code == 200
        csrf_token = sess_res.json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        yield {
            "client": client,
            "headers": headers,
            "db_path": db_path,
            "home": home_path,
            "tmpdir": tmpdir,
        }


# ============================================================================
# 1. Project Reconstruction & Resolution Tests
# ============================================================================


def test_project_reconstruction_from_cwd(pipeline_env):
    """When a conversation has a new cwd, reconstruct a new project without hidden defaults."""
    tmpdir = pipeline_env["tmpdir"]
    workspace = os.path.join(tmpdir, "workspaces", "my-service")
    os.makedirs(workspace, exist_ok=True)

    # Initially no project matches
    matched, is_ambiguous = project_service.resolve_project_by_path(workspace)
    assert matched is None
    assert is_ambiguous is False

    # Reconstruct project
    pid = project_service.reconstruct_or_resolve_project(workspace, metadata={"name": "My Service"})
    assert pid is not None

    proj = project_service.get_project(pid)
    assert proj["name"] == "My Service"
    assert any(os.path.abspath(workspace) == os.path.abspath(d) for d in proj["directories"])

    # Subsequent resolution must resolve to this project
    matched2, is_ambiguous2 = project_service.resolve_project_by_path(workspace)
    assert matched2 is not None
    assert matched2["id"] == pid
    assert is_ambiguous2 is False

    # Second call to reconstruct_or_resolve_project returns same pid
    pid2 = project_service.reconstruct_or_resolve_project(workspace)
    assert pid2 == pid


# ============================================================================
# 2. Content Revision Hash & Idempotent Sync Tests
# ============================================================================


def test_content_revision_hash_and_idempotent_sync(pipeline_env):
    """Repeated unchanged sync must not requeue distillation or record duplicate audit entries."""
    agent = "omp"
    title = "Session Alpha"
    ext_id = "session-123"
    messages = [
        {"role": "user", "content": "How do I implement caching?", "timestamp": "2026-10-24T10:00:00Z"},
        {"role": "assistant", "content": "Use Redis or in-memory dict.", "timestamp": "2026-10-24T10:00:05Z"},
    ]

    # First upsert creates conversation and queues distillation
    conv1 = conversation_service.upsert_conversation(
        agent=agent,
        title=title,
        external_id=ext_id,
        messages=messages,
        process_summary="Discussed caching",
    )
    assert conv1.get("_changed") is True
    assert conv1["processing_status"] == "pending"
    conv_id = conv1["id"]

    with db() as conn:
        q_count1 = conn.execute("SELECT COUNT(*) as c FROM distillation_queue WHERE conversation_id = ?", (conv_id,)).fetchone()["c"]
        audit_count1 = conn.execute(
            "SELECT COUNT(*) as c FROM audit_logs WHERE target = ? AND action IN ('conversation.create', 'conversation.update_sync')",
            (conv_id,),
        ).fetchone()["c"]
    assert q_count1 == 1
    assert audit_count1 == 1

    # Second upsert with identical content must be a no-op
    conv2 = conversation_service.upsert_conversation(
        agent=agent,
        title=title,
        external_id=ext_id,
        messages=messages,
        process_summary="Discussed caching",
    )
    assert conv2.get("_changed") is False
    assert conv2["id"] == conv_id

    with db() as conn:
        q_count2 = conn.execute("SELECT COUNT(*) as c FROM distillation_queue WHERE conversation_id = ?", (conv_id,)).fetchone()["c"]
        audit_count2 = conn.execute(
            "SELECT COUNT(*) as c FROM audit_logs WHERE target = ? AND action IN ('conversation.create', 'conversation.update_sync')",
            (conv_id,),
        ).fetchone()["c"]
    # Queue count and audit count must NOT have increased!
    assert q_count2 == 1
    assert audit_count2 == 1

    # Third upsert with changed content must trigger update & requeue
    updated_messages = list(messages) + [{"role": "user", "content": "Tell me more about TTL.", "timestamp": "2026-10-24T10:01:00Z"}]
    conv3 = conversation_service.upsert_conversation(
        agent=agent,
        title=title,
        external_id=ext_id,
        messages=updated_messages,
        process_summary="Discussed caching and TTL",
    )
    assert conv3.get("_changed") is True
    assert conv3["id"] == conv_id
    assert conv3["content_hash"] != conv1["content_hash"]

    with db() as conn:
        audit_count3 = conn.execute(
            "SELECT COUNT(*) as c FROM audit_logs WHERE target = ? AND action = 'conversation.update_sync'",
            (conv_id,),
        ).fetchone()["c"]
    assert audit_count3 == 1


# ============================================================================
# 3. Stable Tombstone Key vs Content Hash (Anti-Resurrection)
# ============================================================================


def test_stable_tombstone_prevents_resurrection(pipeline_env):
    """Deleting conversation tombstones the stable source identity key, preventing resurrection on sync."""
    agent = "codex"
    ext_id = "codex-rollout-999"
    title = "Refactoring Session"

    conv = conversation_service.upsert_conversation(
        agent=agent,
        title=title,
        external_id=ext_id,
        messages=[{"role": "user", "content": "Refactor database.py"}],
    )
    conv_id = conv["id"]

    # Delete conversation
    del_res = conversation_service.delete_conversation(conv_id, actor="test_user")
    assert del_res["ok"] is True

    # Check conversation is physically deleted
    with pytest.raises(Exception):
        conversation_service.get_conversation(conv_id)

    # Check tombstone exists with stable source identity key
    expected_tombstone_key = conversation_service.compute_source_identity_key(agent, None, title, ext_id)
    with db() as conn:
        tb = conn.execute(
            "SELECT * FROM tombstones WHERE entity_type = 'conversation' AND dedupe_key = ?",
            (expected_tombstone_key,),
        ).fetchone()
        assert tb is not None

    # Attempting to sync or upsert the tombstoned session must fail with 410 (never resurrect)
    with pytest.raises(Exception) as excinfo:
        conversation_service.upsert_conversation(
            agent=agent,
            title=title,
            external_id=ext_id,
            messages=[{"role": "user", "content": "Refactor database.py (modified)"}],
        )
    assert "410" in str(excinfo.value) or "tombstone" in str(excinfo.value).lower()


# ============================================================================
# 4. Incremental Hook Events: Append/Upsert Turns
# ============================================================================


def test_incremental_hook_events_append_turns(pipeline_env):
    """Incremental hook events must append/upsert turns within a session without replacing past turns."""
    client = pipeline_env["client"]
    headers = pipeline_env["headers"]
    home = pipeline_env["home"]

    # 1. Install/enable omp connection to generate token
    install_res = connection_service.install_connection("omp", confirmed=True, home=home)
    assert install_res["ok"] is True

    # Read generated token from secure credentials file
    token_file = home / ".contextcore" / "credentials" / "omp.token"
    token = token_file.read_text(encoding="utf-8").strip()
    hook_headers = {**headers, "Authorization": f"Bearer {token}"}

    session_id = "hook-session-777"

    # Event 1: User prompt
    event1 = {
        "event": "message",
        "sessionId": session_id,
        "message": {"role": "user", "content": "Step 1: start migration"},
    }
    r1 = client.post("/api/connections/omp/hook", json=event1, headers=hook_headers)
    assert r1.status_code == 200
    assert r1.json()["processed"] == 1

    # Event 2: Assistant reply (incremental turn)
    event2 = {
        "event": "message",
        "sessionId": session_id,
        "message": {"role": "assistant", "content": "Step 1 completed: schema updated"},
    }
    r2 = client.post("/api/connections/omp/hook", json=event2, headers=hook_headers)
    assert r2.status_code == 200
    assert r2.json()["processed"] == 1

    # Retrieve conversation and verify both turns exist
    convs = conversation_service.list_conversations(agent="omp")
    assert len(convs) == 1
    stored_msgs = convs[0]["messages"]
    assert len(stored_msgs) == 2
    assert stored_msgs[0]["content"] == "Step 1: start migration"
    assert stored_msgs[1]["content"] == "Step 1 completed: schema updated"


# ============================================================================
# 5. Distillation Queue Consumption & Atomic Memory Application
# ============================================================================


@pytest.mark.anyio
async def test_distillation_queue_consumption_success(pipeline_env):
    """Distillation queue consumer calls transport and applies memories, versions, sources, usage atomically."""
    # Enable connection
    connection_service.patch_connection("omp", enabled=True, confirmed=True)

    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Distillation Test",
        external_id="dist-sess-001",
        messages=[{"role": "user", "content": "Always format code with 4 spaces."}],
    )

    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "preference",
                "title": "Code Formatting Indentation",
                "content": "Always format code with 4 spaces.",
                "tags": ["coding", "style"],
                "quote": "Always format code with 4 spaces.",
            }
        ],
        usage={"tokens": 128, "input_tokens": 80, "output_tokens": 48},
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        count = await distillation_service.process_pending_distillations()
        assert count == 1

    # Verify conversation status is processed
    updated_conv = conversation_service.get_conversation(conv["id"])
    assert updated_conv["processing_status"] == "processed"
    assert updated_conv["processing_error"] is None

    # Verify queue status is completed
    with db() as conn:
        q_item = conn.execute("SELECT * FROM distillation_queue WHERE conversation_id = ?", (conv["id"],)).fetchone()
        assert q_item["status"] == "completed"
        assert q_item["error"] is None

        # Verify memory created
        mem = conn.execute("SELECT * FROM contents WHERE title = 'Code Formatting Indentation'").fetchone()
        assert mem is not None
        assert mem["category"] == "preference"
        assert mem["content"] == "Always format code with 4 spaces."
        assert mem["status"] == "active"

        # Verify usage recorded in usage_meters
        usage_row = conn.execute("SELECT * FROM usage_meters WHERE host_id = 'omp'").fetchone()
        assert usage_row is not None
        assert usage_row["tokens"] == 128
        assert usage_row["requests"] == 1


# ============================================================================
# 6. Stale Result Discarding During Compute
# ============================================================================


@pytest.mark.anyio
async def test_stale_result_discarded_if_conversation_modified(pipeline_env):
    """If conversation is modified while compute was running, stale result must be discarded."""
    connection_service.patch_connection("omp", enabled=True, confirmed=True)

    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Concurrent Edit Test",
        external_id="conc-edit-001",
        messages=[{"role": "user", "content": "Initial prompt"}],
    )

    mock_result = DistillResult(
        status="success",
        memories=[{"category": "task", "title": "Old Task", "content": "Old Content"}],
        usage={"tokens": 50},
    )

    async def mock_run_distill(*args, **kwargs):
        # Simulate user modifying conversation during compute
        conversation_service.upsert_conversation(
            agent="omp",
            title="Concurrent Edit Test",
            external_id="conc-edit-001",
            messages=[{"role": "user", "content": "Initial prompt (edited)"}],
        )
        return mock_result

    with patch("app.transport.run_distill", side_effect=mock_run_distill):
        await distillation_service.process_pending_distillations()

    # The old memory should NOT have been created
    with db() as conn:
        mem = conn.execute("SELECT * FROM contents WHERE title = 'Old Task'").fetchone()
        assert mem is None


# ============================================================================
# 7. Conflict Detection & No Indefinite Re-opening
# ============================================================================


@pytest.mark.anyio
async def test_conflict_not_reopened_if_already_resolved(pipeline_env):
    """When a memory conflict was previously resolved by the user, subsequent identical proposals must not re-open."""
    connection_service.patch_connection("omp", enabled=True, confirmed=True)

    # 1. Create an active memory
    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO contents (id, project_id, kind, category, title, content, tags, agent, status, version, related_ids, content_hash, created_at, updated_at)
            VALUES ('mem-arch', NULL, 'memory', 'decision', 'Database Choice', 'Use SQLite WAL', '[]', 'omp', 'active', 1, '[]', 'hash-1', ?, ?)
            """,
            (now, now),
        )

    # 2. Simulate user resolving a conflict previously
    with db() as conn:
        conn.execute(
            """
            INSERT INTO conflicts (id, project_id, existing_id, proposed, reason, status, resolution, created_at, resolved_at)
            VALUES ('conf-prev', NULL, 'mem-arch', ?, 'Conflict', 'resolved', 'keep', ?, ?)
            """,
            (encode({"title": "Database Choice", "content": "Use PostgreSQL"}), now, now),
        )

    # 3. Conversation tries to distill the exact same conflicting proposal
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Distill Conflict",
        external_id="dist-conf-001",
        messages=[{"role": "user", "content": "We should use PostgreSQL instead."}],
    )
    mock_result = DistillResult(
        status="success",
        memories=[{"category": "decision", "title": "Database Choice", "content": "Use PostgreSQL"}],
        usage={"tokens": 60},
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        await distillation_service.process_pending_distillations()

    # Verify no new pending conflict was created
    with db() as conn:
        pending_confs = conn.execute(
            "SELECT * FROM conflicts WHERE existing_id = 'mem-arch' AND status = 'pending'"
        ).fetchall()
        assert len(pending_confs) == 0


# ============================================================================
# 8. Connection Security, Setup Read-Only & Permissions
# ============================================================================


def test_connection_security_and_setup(pipeline_env):
    """Test read-only setup preview, secret redaction, and pause collection boundary."""
    client = pipeline_env["client"]
    headers = pipeline_env["headers"]

    # 1. GET setup is read-only: no tokens in returned preview
    setup_res = client.get("/api/connections/omp/setup")
    assert setup_res.status_code == 200
    setup_data = setup_res.json()
    assert setup_data["token"] is None
    assert "target_paths" in setup_data
    assert "operations" in setup_data
    assert setup_data["target_paths"]["mcp"].endswith("mcp.json")

    # 2. list_connections never exposes token_hash
    conns = client.get("/api/connections").json()
    for c in conns:
        assert "token_hash" not in c

    # 3. POST install without confirmed=True returns 422
    r_unconf = client.post("/api/connections/omp/install", json={"confirmed": False}, headers=headers)
    assert r_unconf.status_code == 422

    # 4. POST uninstall without confirmed=True returns 422
    r_unconf2 = client.post("/api/connections/omp/uninstall", json={"confirmed": False}, headers=headers)
    assert r_unconf2.status_code == 422

    # 5. Paused connection stops sync but allows MCP token verification
    connection_service.patch_connection("omp", enabled=True, paused=True, confirmed=True)
    with db() as conn:
        token_hash = hashlib.sha256(b"dummy-secret-token").hexdigest()
        conn.execute("UPDATE connections SET token_hash = ? WHERE id = 'omp'", (token_hash,))

    # MCP token verification must succeed even when paused!
    verified = connection_service.verify_agent_token("dummy-secret-token")
    assert verified == "omp"

    # But sync must be refused when paused!
    with pytest.raises(Exception) as exc:
        connection_service.sync_connection("omp")
    assert "paused" in str(exc.value).lower()
