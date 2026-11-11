import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.database import create_schema, db, encode, new_id, now_iso, seed_data, set_app_setting
from app.services import (
    connection_service,
    content_service,
    conversation_service,
    distillation_service,
    project_service,
)
from app.transport import DistillResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def test_env(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_decision_replacement.db")
        monkeypatch.setenv("CONTEXTCORE_DB", db_path)
        create_schema(Path(db_path))
        seed_data(Path(db_path))
        set_app_setting("compute_host", "omp")
        set_app_setting("compute_authorized", True)
        connection_service.patch_connection("omp", enabled=True, confirmed=True)

        yield {
            "db_path": db_path,
            "tmpdir": tmpdir,
        }


# ============================================================================
# 1. Explicit Confirmed Supersession
# ============================================================================


@pytest.mark.anyio
async def test_explicit_confirmed_supersession_retires_old_and_creates_provenance(test_env):
    """When a user explicitly supersedes a decision, old decision is retired to historical,

    and new decision is created active with atomic provenance linking.
    """
    project = project_service.create_project(name="Decision Test Project")
    project_id = project["id"]

    # 1. Existing active decision
    d1 = content_service.create_content(
        title="Primary Database",
        content="Use PostgreSQL for all storage.",
        project_id=project_id,
        category="decision",
        agent="omp",
    )
    assert d1["status"] == "active"
    assert d1["version"] == 1

    # 2. Conversation with explicit user instruction
    user_quote = "Let's change our primary DB from PostgreSQL to SQLite. Use SQLite for all storage."
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="DB Architecture Discussion",
        external_id="conv-db-001",
        project_id=project_id,
        messages=[
            {"role": "assistant", "content": "What database should we use?"},
            {"role": "user", "content": user_quote},
        ],
    )

    # 3. Model distillation result with explicit confirmed supersedes
    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Primary Database",
                "content": "Use SQLite for all storage.",
                "tags": ["database", "storage"],
                "quote": user_quote,
                "user_message_index": 1,
                "supersedes": {
                    "id": d1["id"],
                    "expected_version": 1,
                    "evidence_quote": user_quote,
                    "confidence": "explicit_confirmed",
                    "reason": "User explicitly switched primary database to SQLite",
                },
            }
        ],
        usage={"tokens": 100},
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        count = await distillation_service.process_pending_distillations()
        assert count == 1

    with db() as conn:
        # Verify D1 retired to historical
        old_row = conn.execute("SELECT * FROM contents WHERE id = ?", (d1["id"],)).fetchone()
        assert old_row["status"] == "historical"

        # Verify new decision D2 created active
        new_row = conn.execute(
            "SELECT * FROM contents WHERE project_id = ? AND category = 'decision' AND status = 'active'",
            (project_id,),
        ).fetchone()
        assert new_row is not None
        assert new_row["id"] != d1["id"]
        assert new_row["title"] == "Primary Database"
        assert new_row["content"] == "Use SQLite for all storage."
        assert new_row["version"] == 1
        assert d1["id"] in new_row["related_ids"]

        # Verify version record
        ver_row = conn.execute("SELECT * FROM content_versions WHERE content_id = ?", (new_row["id"],)).fetchone()
        assert ver_row is not None
        assert ver_row["version"] == 1

        # Verify provenance source
        src_row = conn.execute("SELECT * FROM content_sources WHERE content_id = ?", (new_row["id"],)).fetchone()
        assert src_row is not None
        assert src_row["source_id"] == conv["id"]
        assert src_row["quote"] == user_quote
        assert src_row["deleted"] == 0

        # Verify audit logs recorded
        supersede_audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'content.supersede' AND target = ?",
            (d1["id"],),
        ).fetchone()
        assert supersede_audit is not None

        create_audit = conn.execute(
            "SELECT * FROM audit_logs WHERE action = 'content.create_distill' AND target = ?",
            (new_row["id"],),
        ).fetchone()
        assert create_audit is not None

        # Verify zero pending conflicts
        pending_confs = conn.execute("SELECT * FROM conflicts WHERE status = 'pending'").fetchall()
        assert len(pending_confs) == 0


# ============================================================================
# 2. Ambiguous Supersession & Conflict Routing
# ============================================================================


@pytest.mark.anyio
async def test_ambiguous_confidence_routes_to_conflict(test_env):
    """When model reports confidence 'ambiguous', decision is not retired and routes to conflict."""
    project = project_service.create_project(name="Ambiguous Confidence Project")
    project_id = project["id"]

    d1 = content_service.create_content(
        title="Cache Tier",
        content="Use in-memory cache.",
        project_id=project_id,
        category="decision",
        agent="omp",
    )

    user_quote = "Maybe we could use Redis for caching later."
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Cache Discussion",
        external_id="conv-cache-001",
        project_id=project_id,
        messages=[
            {"role": "user", "content": user_quote},
        ],
    )

    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Cache Tier",
                "content": "Use Redis for caching.",
                "quote": user_quote,
                "user_message_index": 0,
                "supersedes": {
                    "id": d1["id"],
                    "expected_version": 1,
                    "evidence_quote": user_quote,
                    "confidence": "ambiguous",
                    "reason": "User said maybe later; replacement relationship unclear",
                },
            }
        ],
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        await distillation_service.process_pending_distillations()

    with db() as conn:
        # D1 must remain active
        old_row = conn.execute("SELECT * FROM contents WHERE id = ?", (d1["id"],)).fetchone()
        assert old_row["status"] == "active"

        # No new active decision created
        active_decisions = conn.execute(
            "SELECT * FROM contents WHERE project_id = ? AND category = 'decision' AND status = 'active'",
            (project_id,),
        ).fetchall()
        assert len(active_decisions) == 1

        # A pending conflict must be created
        conf = conn.execute("SELECT * FROM conflicts WHERE existing_id = ? AND status = 'pending'", (d1["id"],)).fetchone()
        assert conf is not None
        assert "ambiguous" in conf["reason"].lower()


@pytest.mark.anyio
async def test_assistant_suggestion_without_user_confirmation_cannot_supersede(test_env):
    """An agent/assistant suggestion cannot masquerade as user authority to supersede a decision."""
    project = project_service.create_project(name="Assistant Suggestion Project")
    project_id = project["id"]

    d1 = content_service.create_content(
        title="Auth Method",
        content="Use JWT tokens.",
        project_id=project_id,
        category="decision",
        agent="omp",
    )

    # Message 0: user, Message 1: assistant
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Auth Discussion",
        external_id="conv-auth-001",
        project_id=project_id,
        messages=[
            {"role": "user", "content": "How should we handle authentication?"},
            {"role": "assistant", "content": "I recommend we switch from JWT to session cookies."},
        ],
    )

    # Distillation references message 1 (assistant message)
    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Auth Method",
                "content": "Use session cookies.",
                "quote": "I recommend we switch from JWT to session cookies.",
                "user_message_index": 1,  # Points to assistant!
                "supersedes": {
                    "id": d1["id"],
                    "expected_version": 1,
                    "evidence_quote": "I recommend we switch from JWT to session cookies.",
                    "confidence": "explicit_confirmed",
                },
            }
        ],
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        await distillation_service.process_pending_distillations()

    with db() as conn:
        # D1 must NOT be retired
        old_row = conn.execute("SELECT * FROM contents WHERE id = ?", (d1["id"],)).fetchone()
        assert old_row["status"] == "active"

        # Must route to conflict due to lack of user confirmation
        conf = conn.execute("SELECT * FROM conflicts WHERE existing_id = ? AND status = 'pending'", (d1["id"],)).fetchone()
        assert conf is not None
        assert "user evidence" in conf["reason"].lower() or "user confirmation" in conf["reason"].lower()


# ============================================================================
# 3. Stale Target Version & Cross-Project Guards
# ============================================================================


@pytest.mark.anyio
async def test_stale_target_version_creates_conflict(test_env):
    """If target decision was updated to version 2, distillation targeting version 1 must conflict."""
    project = project_service.create_project(name="Stale Version Project")
    project_id = project["id"]

    d1 = content_service.create_content(
        title="API Protocol",
        content="Use REST over HTTP/1.1.",
        project_id=project_id,
        category="decision",
        agent="omp",
    )
    # Update to version 2
    d1_v2 = content_service.update_content(
        d1["id"],
        content="Use REST over HTTP/2.",
        expected_version=1,
    )
    assert d1_v2["version"] == 2

    user_quote = "We are migrating all APIs to gRPC. Use gRPC."
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Protocol Discussion",
        external_id="conv-proto-001",
        project_id=project_id,
        messages=[
            {"role": "user", "content": user_quote},
        ],
    )

    # Distillation targets stale version 1
    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "API Protocol",
                "content": "Use gRPC.",
                "quote": user_quote,
                "user_message_index": 0,
                "supersedes": {
                    "id": d1["id"],
                    "expected_version": 1,  # Stale! Database is at 2
                    "evidence_quote": user_quote,
                    "confidence": "explicit_confirmed",
                },
            }
        ],
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        await distillation_service.process_pending_distillations()

    with db() as conn:
        # D1 must remain active at version 2
        current_row = conn.execute("SELECT * FROM contents WHERE id = ?", (d1["id"],)).fetchone()
        assert current_row["status"] == "active"
        assert current_row["version"] == 2

        # A conflict must be recorded for stale version
        conf = conn.execute("SELECT * FROM conflicts WHERE existing_id = ? AND status = 'pending'", (d1["id"],)).fetchone()
        assert conf is not None
        assert "stale" in conf["reason"].lower() or "version" in conf["reason"].lower()


@pytest.mark.anyio
async def test_non_integer_expected_version_rejected_as_conflict(test_env):
    """expected_version must be integer; boolean or string types are rejected to conflict."""
    project = project_service.create_project(name="Invalid Version Type Project")
    project_id = project["id"]

    d1 = content_service.create_content(
        title="Logging Framework",
        content="Use loguru.",
        project_id=project_id,
        category="decision",
        agent="omp",
    )

    user_quote = "Switch logging to structlog. Use structlog."
    conversation_service.upsert_conversation(
        agent="omp",
        title="Logging Discussion",
        external_id="conv-log-001",
        project_id=project_id,
        messages=[{"role": "user", "content": user_quote}],
    )

    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Logging Framework",
                "content": "Use structlog.",
                "quote": user_quote,
                "user_message_index": 0,
                "supersedes": {
                    "id": d1["id"],
                    "expected_version": True,  # Bool is not valid integer!
                    "evidence_quote": user_quote,
                    "confidence": "explicit_confirmed",
                },
            }
        ],
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        await distillation_service.process_pending_distillations()

    with db() as conn:
        d1_row = conn.execute("SELECT * FROM contents WHERE id = ?", (d1["id"],)).fetchone()
        assert d1_row["status"] == "active"
        conf = conn.execute("SELECT * FROM conflicts WHERE existing_id = ? AND status = 'pending'", (d1["id"],)).fetchone()
        assert conf is not None


@pytest.mark.anyio
async def test_cross_project_supersession_rejected_as_conflict(test_env):
    """A conversation in Project B cannot supersede an active decision belonging to Project A."""
    proj_a = project_service.create_project(name="Project Alpha")
    proj_b = project_service.create_project(name="Project Beta")

    d_a = content_service.create_content(
        title="Shared Rule",
        content="Alpha rule.",
        project_id=proj_a["id"],
        category="decision",
        agent="omp",
    )

    user_quote = "Beta rule override."
    conversation_service.upsert_conversation(
        agent="omp",
        title="Beta Discussion",
        external_id="conv-beta-001",
        project_id=proj_b["id"],
        messages=[{"role": "user", "content": user_quote}],
    )

    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Shared Rule",
                "content": "Beta rule override.",
                "quote": user_quote,
                "user_message_index": 0,
                "supersedes": {
                    "id": d_a["id"],
                    "expected_version": 1,
                    "evidence_quote": user_quote,
                    "confidence": "explicit_confirmed",
                },
            }
        ],
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        await distillation_service.process_pending_distillations()

    with db() as conn:
        # Decision in Project A remains active
        d_a_row = conn.execute("SELECT * FROM contents WHERE id = ?", (d_a["id"],)).fetchone()
        assert d_a_row["status"] == "active"

        # Invalid cross-project supersedes with no same-title active memory in Project B
        # drops the invalid supersedes reference instead of creating a NULL existing_id conflict
        null_conf = conn.execute("SELECT * FROM conflicts WHERE project_id = ? AND existing_id IS NULL", (proj_b["id"],)).fetchone()
        assert null_conf is None
        b_mem = conn.execute("SELECT * FROM contents WHERE project_id = ? AND title = 'Shared Rule' AND status = 'active'", (proj_b["id"],)).fetchone()
        assert b_mem is not None
        assert b_mem["related_ids"] == "[]"


# ============================================================================
# 4. Same-Title Disambiguation & Ordinary User Decisions
# ============================================================================


@pytest.mark.anyio
async def test_same_title_without_supersedes_creates_conflict_not_silent_duplicate(test_env):
    """PRD M01-02: Same title with conflicting content without explicit supersedes object

    must ask/conflict, never create silent duplicates or silently overwrite.
    """
    project = project_service.create_project(name="Same Title Project")
    project_id = project["id"]

    d1 = content_service.create_content(
        title="Deployment Target",
        content="Deploy to AWS ECS.",
        project_id=project_id,
        category="decision",
        agent="omp",
    )

    user_quote = "Deploy to Cloudflare Workers instead."
    conversation_service.upsert_conversation(
        agent="omp",
        title="Deployment Discussion",
        external_id="conv-deploy-001",
        project_id=project_id,
        messages=[{"role": "user", "content": user_quote}],
    )

    # User confirmed quote, but supersedes object is absent
    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Deployment Target",
                "content": "Deploy to Cloudflare Workers.",
                "quote": user_quote,
                "user_message_index": 0,
                "supersedes": None,
            }
        ],
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        await distillation_service.process_pending_distillations()

    with db() as conn:
        # D1 remains active
        d1_row = conn.execute("SELECT * FROM contents WHERE id = ?", (d1["id"],)).fetchone()
        assert d1_row["status"] == "active"

        # No duplicate active item with same title
        active_items = conn.execute(
            "SELECT * FROM contents WHERE project_id = ? AND title = 'Deployment Target' AND status = 'active'",
            (project_id,),
        ).fetchall()
        assert len(active_items) == 1

        # Conflict recorded
        conf = conn.execute("SELECT * FROM conflicts WHERE existing_id = ? AND status = 'pending'", (d1["id"],)).fetchone()
        assert conf is not None
        assert "not specified" in conf["reason"].lower() or "matches" in conf["reason"].lower()


@pytest.mark.anyio
async def test_ordinary_confirmed_user_decision_without_supersedes_creates_active_memory(test_env):
    """Ordinary user decision with confirmed user quote and no prior conflicting item creates active memory."""
    project = project_service.create_project(name="New Decision Project")
    project_id = project["id"]

    user_quote = "Always enforce Conventional Commits for commit messages."
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Git Guidelines",
        external_id="conv-git-001",
        project_id=project_id,
        messages=[{"role": "user", "content": user_quote}],
    )

    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Commit Message Standard",
                "content": "Enforce Conventional Commits.",
                "tags": ["git", "standards"],
                "quote": user_quote,
                "user_message_index": 0,
                "supersedes": None,
            }
        ],
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        count = await distillation_service.process_pending_distillations()
        assert count == 1

    with db() as conn:
        mem = conn.execute("SELECT * FROM contents WHERE project_id = ? AND title = 'Commit Message Standard'", (project_id,)).fetchone()
        assert mem is not None
        assert mem["status"] == "active"
        assert mem["category"] == "decision"
        assert mem["version"] == 1

        src = conn.execute("SELECT * FROM content_sources WHERE content_id = ?", (mem["id"],)).fetchone()
        assert src is not None
        assert src["source_id"] == conv["id"]
        assert src["quote"] == user_quote


# ============================================================================
# 5. Rollback Behavior: Atomicity Rollback & Version Rollback
# ============================================================================


@pytest.mark.anyio
async def test_supersession_transaction_atomicity_rollback(test_env, monkeypatch):
    """If an error occurs mid-application of supersession, the entire transaction rolls back

    leaving the old decision active and preventing orphaned records.
    """
    project = project_service.create_project(name="Atomicity Rollback Project")
    project_id = project["id"]

    d1 = content_service.create_content(
        title="State Management",
        content="Use Redux.",
        project_id=project_id,
        category="decision",
        agent="omp",
    )

    user_quote = "Switch state management from Redux to Zustand. Use Zustand."
    conversation_service.upsert_conversation(
        agent="omp",
        title="State Discussion",
        external_id="conv-state-001",
        project_id=project_id,
        messages=[{"role": "user", "content": user_quote}],
    )

    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "State Management",
                "content": "Use Zustand.",
                "quote": user_quote,
                "user_message_index": 0,
                "supersedes": {
                    "id": d1["id"],
                    "expected_version": 1,
                    "evidence_quote": user_quote,
                    "confidence": "explicit_confirmed",
                },
            }
        ],
    )

    # Monkeypatch log_audit to fail on content.create_distill to simulate crash after retirement
    from app.services import distillation_service as dist_mod
    real_apply = dist_mod.apply_distilled_memories

    def interrupted_apply(conn, conv, memories, actor, now):
        # Call real apply but raise halfway
        real_apply(conn, conv, memories, actor, now)
        raise RuntimeError("Simulated crash before transaction commit")

    monkeypatch.setattr(dist_mod, "apply_distilled_memories", interrupted_apply)

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        with pytest.raises(RuntimeError):
            await distillation_service.process_pending_distillations()

    with db() as conn:
        # D1 must remain ACTIVE because transaction rolled back!
        d1_row = conn.execute("SELECT * FROM contents WHERE id = ?", (d1["id"],)).fetchone()
        assert d1_row["status"] == "active"

        # No Zustand memory was committed
        zustand_row = conn.execute("SELECT * FROM contents WHERE content LIKE '%Zustand%'").fetchone()
        assert zustand_row is None


def test_version_rollback_via_content_service(test_env):
    """PRD D015: Support rolling back a modified decision to an earlier version."""
    project = project_service.create_project(name="Version Rollback Project")
    project_id = project["id"]

    # 1. Create decision at v1
    d = content_service.create_content(
        title="Code Review Policy",
        content="Require 1 reviewer approval.",
        project_id=project_id,
        category="decision",
    )
    assert d["version"] == 1

    # 2. Update to v2
    d_v2 = content_service.update_content(
        d["id"],
        content="Require 2 reviewer approvals.",
        expected_version=1,
    )
    assert d_v2["version"] == 2
    assert d_v2["content"] == "Require 2 reviewer approvals."

    # 3. Rollback to v1 via restore_version
    restored = content_service.restore_version(d["id"], version=1, expected_version=2)
    assert restored["version"] == 3
    assert restored["content"] == "Require 1 reviewer approval."

    # 4. Check version audit and history
    versions = content_service.get_content(d["id"])["versions"]
    assert len(versions) == 3
    assert versions[0]["version"] == 1
    assert versions[1]["version"] == 2
    assert versions[2]["version"] == 3
    assert versions[2]["actor"] == "restore_v1"


# ============================================================================
# 6. Tombstone Nonresurrection & Source Invalidation Guards
# ============================================================================


@pytest.mark.anyio
async def test_tombstoned_deleted_decision_cannot_be_reingested_via_distillation(test_env):
    """Critical check: A permanently deleted decision (tombstoned) must never be resurrected by distillation."""
    project = project_service.create_project(name="Tombstone Project")
    project_id = project["id"]

    # Create and delete decision
    d = content_service.create_content(
        title="Secret Rule",
        content="Deleted confidential requirement.",
        project_id=project_id,
        category="decision",
    )
    content_service.delete_content(d["id"])

    # Conversation proposes identical content
    user_quote = "Let's record the secret rule: Deleted confidential requirement."
    conversation_service.upsert_conversation(
        agent="omp",
        title="Leaked Discussion",
        external_id="conv-leak-001",
        project_id=project_id,
        messages=[{"role": "user", "content": user_quote}],
    )

    mock_result = DistillResult(
        status="success",
        memories=[
            {
                "category": "decision",
                "title": "Secret Rule",
                "content": "Deleted confidential requirement.",
                "quote": user_quote,
                "user_message_index": 0,
            }
        ],
    )

    with patch("app.transport.run_distill", new=AsyncMock(return_value=mock_result)):
        await distillation_service.process_pending_distillations()

    with db() as conn:
        resurrected = conn.execute("SELECT * FROM contents WHERE title = 'Secret Rule'").fetchone()
        assert resurrected is None


@pytest.mark.anyio
async def test_tombstoned_source_conversation_rejects_distillation(test_env):
    """When a conversation source is tombstoned, distillation processing is aborted."""
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Tombstoned Conv",
        external_id="conv-tomb-001",
        messages=[{"role": "user", "content": "Hello"}],
    )

    # Tombstone the conversation source
    source_key = conversation_service.compute_source_identity_key(
        conv["agent"], conv.get("source_path"), conv["title"], conv.get("external_id")
    )
    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO tombstones (id, entity_type, dedupe_key, scope_project_id, deleted_at, deleted_by, reason)
            VALUES ('tomb-c1', 'conversation', ?, NULL, ?, 'user', 'manual_delete')
            """,
            (source_key, now),
        )

    # Queue distillation
    queue_item = distillation_service.queue_distillation(conv["id"])

    # Processing must fail due to source tombstone
    res = await distillation_service.process_distillation_queue_item(queue_item["id"])
    assert res is not None
    assert res["ok"] is False
    assert "tombstoned" in res["error"].lower()

    # Direct submission must raise 410
    with pytest.raises(HTTPException) as exc:
        distillation_service.submit_structured_distillation(
            conversation_id=conv["id"],
            memories=[{"title": "T", "content": "C", "category": "knowledge"}],
        )
    assert exc.value.status_code == 410


@pytest.mark.anyio
async def test_revoked_or_paused_connection_rejects_distillation(test_env):
    """When host connection is paused or disabled, distillation fails immediately."""
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Connection Revoke Test",
        external_id="conv-rev-001",
        messages=[{"role": "user", "content": "Hello"}],
    )

    # Pause connection
    connection_service.patch_connection("omp", paused=True, confirmed=True)

    queue_item = distillation_service.queue_distillation(conv["id"])
    res = await distillation_service.process_distillation_queue_item(queue_item["id"])
    assert res is not None
    assert res["ok"] is False
    assert "paused" in res["error"].lower()

    # submit_structured_distillation must raise 403
    with pytest.raises(HTTPException) as exc:
        distillation_service.submit_structured_distillation(
            conversation_id=conv["id"],
            memories=[{"title": "T", "content": "C", "category": "knowledge"}],
        )
    assert exc.value.status_code == 403
