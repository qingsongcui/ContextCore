import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import create_schema, db, encode, new_id, now_iso, seed_data, set_app_setting, get_app_setting
from app.main import app
from app.services import connection_service, conversation_service, distillation_service
from app.transport import DistillResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def routing_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_compute_routing.db")
        monkeypatch.setenv("CONTEXTCORE_DB", db_path)
        create_schema(Path(db_path))
        seed_data(Path(db_path))

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
            "tmpdir": tmpdir,
        }


# ============================================================================
# 1. Settings Contract Tests (GET and PATCH /api/settings)
# ============================================================================


def test_settings_contract_defaults_and_validation(routing_env):
    """GET /api/settings returns exact contract fields, PATCH enforces explicit confirmation and omp support."""
    client = routing_env["client"]
    headers = routing_env["headers"]

    # 1. Default settings: compute_host is None, compute_authorized is False
    res = client.get("/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert body.get("compute_host") is None
    assert body.get("compute_authorized") is False
    assert "autostart" in body
    assert "platform" in body

    # 2. PATCH compute_host='omp' without compute_confirmed=True fails 400
    res = client.patch("/api/settings", json={"compute_host": "omp"}, headers=headers)
    assert res.status_code == 400

    # 3. PATCH compute_authorized=True without compute_confirmed=True fails 400
    res = client.patch("/api/settings", json={"compute_authorized": True}, headers=headers)
    assert res.status_code == 400

    # 4. PATCH with unsupported host (e.g. cursor) fails 400
    res = client.patch(
        "/api/settings",
        json={"compute_host": "cursor", "compute_confirmed": True},
        headers=headers,
    )
    assert res.status_code == 400

    # 5. PATCH with valid confirmation succeeds and persists in app_settings
    res = client.patch(
        "/api/settings",
        json={"compute_host": "omp", "compute_authorized": True, "compute_confirmed": True},
        headers=headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["compute_host"] == "omp"
    assert body["compute_authorized"] is True

    # 6. GET readback confirms persistence
    readback = client.get("/api/settings").json()
    assert readback["compute_host"] == "omp"
    assert readback["compute_authorized"] is True

    # 7. Disabling compute_authorized=False needs NO spend consent / confirmation
    res = client.patch("/api/settings", json={"compute_authorized": False}, headers=headers)
    assert res.status_code == 200
    assert res.json()["compute_authorized"] is False

    # 8. Unsetting compute_host needs NO confirmation
    res = client.patch("/api/settings", json={"compute_host": None}, headers=headers)
    assert res.status_code == 200
    assert res.json()["compute_host"] is None


# ============================================================================
# 2. Compute Routing: Cursor Source -> OMP Compute & Provenance Preservation
# ============================================================================


@pytest.mark.anyio
async def test_cursor_source_routes_to_omp_compute_and_preserves_provenance(routing_env):
    """Conversations collected from Cursor route to authorized OMP compute, preserving origin actor & provenance."""
    # Enable source connection (cursor) and compute connection (omp)
    connection_service.patch_connection("cursor", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)

    # Authorize OMP compute
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)

    # Create Cursor conversation
    conv = conversation_service.upsert_conversation(
        agent="cursor",
        source_path="/Users/test/.cursor/composer/conv1",
        title="Cursor Feature Architecture",
        external_id="cursor-conv-101",
        messages=[
            {"role": "user", "content": "We must use SQLite for persistence in ContextCore."},
            {"role": "assistant", "content": "Understood, recording SQLite architecture decision."},
        ],
    )
    conv_id = conv["id"]

    # Queue distillation
    q_item = distillation_service.queue_distillation(conv_id)
    queue_id = q_item["id"]

    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Persistence Engine",
                "content": "Use SQLite for persistence in ContextCore.",
                "quote": "We must use SQLite for persistence in ContextCore.",
                "user_message_index": 0,
            }
        ],
        usage={"tokens": 1200, "requests": 1},
        raw_output="omp jsonl output",
    )

    with patch("app.transport.run_distill", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_result
        res = await distillation_service.process_distillation_queue_item(queue_id)

        assert res is not None
        assert res.get("ok") is True

        # Verify transport was called with host_id='omp', NOT 'cursor'
        mock_run.assert_called_once()
        call_host_id = mock_run.call_args[0][0]
        call_payload = mock_run.call_args[0][1]
        assert call_host_id == "omp"
        # Origin agent is preserved in payload metadata
        assert call_payload["agent"] == "cursor"

    # Verify origin actor/provenance is preserved in contents table
    with db() as conn:
        contents = conn.execute("SELECT * FROM contents WHERE title = 'Persistence Engine'").fetchall()
        assert len(contents) == 1
        item = contents[0]
        assert item["agent"] == "cursor"

        # Check content_sources links to cursor conversation
        sources = conn.execute("SELECT * FROM content_sources WHERE content_id = ?", (item["id"],)).fetchall()
        assert len(sources) == 1
        assert sources[0]["source_id"] == conv_id
        assert sources[0]["source_agent"] == "cursor"

        # Check queue completed and conversation processed
        q_row = conn.execute("SELECT status FROM distillation_queue WHERE id = ?", (queue_id,)).fetchone()
        assert q_row["status"] == "completed"

        c_row = conn.execute("SELECT processing_status FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        assert c_row["processing_status"] == "processed"


# ============================================================================
# 3. Usage Metering Attribution: Meter Chosen Compute Host (Never Source)
# ============================================================================


@pytest.mark.anyio
async def test_usage_metering_attributes_to_compute_host_never_source(routing_env):
    """Usage meters record consumption under compute_host ('omp'), never source host ('cursor')."""
    connection_service.patch_connection("cursor", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)

    conv = conversation_service.upsert_conversation(
        agent="cursor",
        source_path="/Users/test/.cursor/composer/conv2",
        title="Cursor Task",
        external_id="cursor-conv-102",
        messages=[{"role": "user", "content": "Deploy ContextCore to macOS."}],
    )
    q_item = distillation_service.queue_distillation(conv["id"])

    mock_result = DistillResult(
        status="success",
        memories=[],
        usage={"tokens": 750, "requests": 1},
        raw_output="done",
    )

    with patch("app.transport.run_distill", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_result
        await distillation_service.process_distillation_queue_item(q_item["id"])

    with db() as conn:
        meters = conn.execute("SELECT host_id, tokens FROM usage_meters").fetchall()
        assert len(meters) == 1
        # Metered to chosen compute host omp, NEVER source host cursor
        assert meters[0]["host_id"] == "omp"
        assert meters[0]["tokens"] == 750

        cursor_meters = conn.execute("SELECT * FROM usage_meters WHERE host_id = 'cursor'").fetchall()
        assert len(cursor_meters) == 0


# ============================================================================
# 4. Absent Consent / Host Disabled: Keep Queue Pending, No Calls, No Error Loop
# ============================================================================


@pytest.mark.anyio
async def test_absent_consent_keeps_queue_pending_with_no_calls(routing_env):
    """When compute authorization is missing, queue item and conversation stay pending, making zero transport calls."""
    connection_service.patch_connection("cursor", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)

    # Missing compute_authorized (default False)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", False)

    conv = conversation_service.upsert_conversation(
        agent="cursor",
        source_path="/Users/test/.cursor/composer/conv3",
        title="Cursor Session",
        external_id="cursor-conv-103",
        messages=[{"role": "user", "content": "Test conversation"}],
    )
    q_item = distillation_service.queue_distillation(conv["id"])
    queue_id = q_item["id"]

    with patch("app.transport.run_distill", new_callable=AsyncMock) as mock_run:
        res = await distillation_service.process_distillation_queue_item(queue_id)

        # No calls made
        assert mock_run.call_count == 0
        assert res is not None
        assert res.get("ok") is False
        assert res.get("status") == "pending"

    # Queue remains pending and conversation honestly reports pending
    with db() as conn:
        q_row = conn.execute("SELECT status, error FROM distillation_queue WHERE id = ?", (queue_id,)).fetchone()
        assert q_row["status"] == "pending"

        c_row = conn.execute("SELECT processing_status, processing_error FROM conversations WHERE id = ?", (conv["id"],)).fetchone()
        assert c_row["processing_status"] == "pending"

    # Re-running process does not error forever; still pending and still zero calls
    with patch("app.transport.run_distill", new_callable=AsyncMock) as mock_run:
        res2 = await distillation_service.process_distillation_queue_item(queue_id)
        assert mock_run.call_count == 0
        assert res2.get("status") == "pending"


@pytest.mark.anyio
async def test_compute_host_disabled_or_paused_keeps_queue_pending(routing_env):
    """When compute host connection is disabled or paused, queue item remains pending without calls."""
    connection_service.patch_connection("cursor", enabled=True, confirmed=True)
    # Pause OMP connection
    connection_service.patch_connection("omp", enabled=True, paused=True, confirmed=True)

    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)

    conv = conversation_service.upsert_conversation(
        agent="cursor",
        source_path="/Users/test/.cursor/composer/conv4",
        title="Cursor Session 4",
        external_id="cursor-conv-104",
        messages=[{"role": "user", "content": "Another test"}],
    )
    q_item = distillation_service.queue_distillation(conv["id"])

    with patch("app.transport.run_distill", new_callable=AsyncMock) as mock_run:
        res = await distillation_service.process_distillation_queue_item(q_item["id"])
        assert mock_run.call_count == 0
        assert res.get("ok") is False
        assert res.get("status") == "pending"

    with db() as conn:
        q_row = conn.execute("SELECT status FROM distillation_queue WHERE id = ?", (q_item["id"],)).fetchone()
        assert q_row["status"] == "pending"


# ============================================================================
# 5. Revocation Mid-Compute Discards Result
# ============================================================================


@pytest.mark.anyio
async def test_revocation_during_compute_discards_result(routing_env):
    """If user revokes compute authorization during compute, result is discarded and not applied."""
    connection_service.patch_connection("cursor", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)

    conv = conversation_service.upsert_conversation(
        agent="cursor",
        source_path="/Users/test/.cursor/composer/conv5",
        title="Cursor Session 5",
        external_id="cursor-conv-105",
        messages=[
            {"role": "user", "content": "We decide to use Tailwind CSS."},
        ],
    )
    q_item = distillation_service.queue_distillation(conv["id"])
    queue_id = q_item["id"]

    # In mock transport, simulate revoking compute authorization mid-compute
    async def mock_distill(host_id, payload, allow_execution=False, timeout=30.0):
        set_app_setting("compute_authorized", False)  # Revoke mid-compute!
        return DistillResult(
            status="success",
            memories=[
                {
                    "category": "decision",
                    "title": "Frontend Styling",
                    "content": "We decide to use Tailwind CSS.",
                    "quote": "We decide to use Tailwind CSS.",
                    "user_message_index": 0,
                }
            ],
            usage={"tokens": 500, "requests": 1},
            raw_output="done",
        )

    with patch("app.transport.run_distill", side_effect=mock_distill):
        res = await distillation_service.process_distillation_queue_item(queue_id)
        assert res is not None
        assert res.get("ok") is False
        assert res.get("status") == "pending"

    # Verify atomic discard: NO memories inserted into contents
    with db() as conn:
        memories = conn.execute("SELECT * FROM contents WHERE title = 'Frontend Styling'").fetchall()
        assert len(memories) == 0

        # Queue item remains pending for when consent is re-granted
        q_row = conn.execute("SELECT status, error FROM distillation_queue WHERE id = ?", (queue_id,)).fetchone()
        assert q_row["status"] == "pending"

        # Usage was still metered under compute host because execution actually took place
        meters = conn.execute("SELECT host_id, tokens FROM usage_meters").fetchall()
        assert len(meters) == 1
        assert meters[0]["host_id"] == "omp"
        assert meters[0]["tokens"] == 500


# ============================================================================
# 6. Source Connection Precedence & Mid-Compute Revocation
# ============================================================================


@pytest.mark.anyio
async def test_source_paused_checked_before_compute_config(routing_env):
    """Source connection paused is checked before compute configuration, failing with connection error."""
    # Source connection is paused
    connection_service.patch_connection("cursor", enabled=True, paused=True, confirmed=True)
    # Compute authorized is False (missing)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", False)

    conv = conversation_service.upsert_conversation(
        agent="cursor",
        source_path="/Users/test/.cursor/composer/conv6",
        title="Cursor Session 6",
        external_id="cursor-conv-106",
        messages=[{"role": "user", "content": "Test"}],
    )
    q_item = distillation_service.queue_distillation(conv["id"])

    with patch("app.transport.run_distill", new_callable=AsyncMock) as mock_run:
        res = await distillation_service.process_distillation_queue_item(q_item["id"])
        assert mock_run.call_count == 0
        assert res.get("ok") is False
        # Expected error is source connection paused, not compute config

    with db() as conn:
        q_row = conn.execute("SELECT status FROM distillation_queue WHERE id = ?", (q_item["id"],)).fetchone()
        assert q_row["status"] == "error"


@pytest.mark.anyio
async def test_source_connection_paused_mid_compute_discards(routing_env):
    """If source connection is paused mid-compute, result is discarded and queue marks error."""
    connection_service.patch_connection("cursor", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)

    conv = conversation_service.upsert_conversation(
        agent="cursor",
        source_path="/Users/test/.cursor/composer/conv7",
        title="Cursor Session 7",
        external_id="cursor-conv-107",
        messages=[{"role": "user", "content": "Source check"}],
    )
    q_item = distillation_service.queue_distillation(conv["id"])

    async def mock_distill(host_id, payload, allow_execution=False, timeout=30.0):
        # Pause source connection mid-compute
        connection_service.patch_connection("cursor", enabled=True, paused=True, confirmed=True)
        return DistillResult(status="success", memories=[{"category": "knowledge", "title": "K1", "content": "C1"}])

    with patch("app.transport.run_distill", side_effect=mock_distill):
        res = await distillation_service.process_distillation_queue_item(q_item["id"])
        assert res.get("ok") is False

    with db() as conn:
        contents = conn.execute("SELECT * FROM contents WHERE title = 'K1'").fetchall()
        assert len(contents) == 0


# ============================================================================
# 7. No Fallback on Compute Failure
# ============================================================================


@pytest.mark.anyio
async def test_no_automatic_fallback_on_compute_failure(routing_env):
    """When authorized OMP compute fails, system never falls back to source host or another host."""
    connection_service.patch_connection("cursor", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)

    conv = conversation_service.upsert_conversation(
        agent="cursor",
        source_path="/Users/test/.cursor/composer/conv8",
        title="Cursor Session 8",
        external_id="cursor-conv-108",
        messages=[{"role": "user", "content": "No fallback test"}],
    )
    q_item = distillation_service.queue_distillation(conv["id"])

    mock_fail = DistillResult(status="failed", error="OMP model execution crashed with exit code 1")

    with patch("app.transport.run_distill", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_fail
        res = await distillation_service.process_distillation_queue_item(q_item["id"])

        # Transport was called exactly once for OMP
        assert mock_run.call_count == 1
        assert mock_run.call_args[0][0] == "omp"
        assert res.get("ok") is False
        assert "crashed" in res.get("error")

    with db() as conn:
        q_row = conn.execute("SELECT status, error FROM distillation_queue WHERE id = ?", (q_item["id"],)).fetchone()
        assert q_row["status"] == "error"
        assert "crashed" in q_row["error"]
