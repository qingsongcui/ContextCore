import pytest
from fastapi import HTTPException

from app.database import create_schema, db, seed_data
from app.services import (
    conflict_service,
    connection_service,
    content_service,
    conversation_service,
    distillation_service,
    handoff_service,
    host_registry,
    project_service,
)


@pytest.fixture
def governance_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "contextcore.db"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_path))
    monkeypatch.setenv("CONTEXTCORE_DATA_DIR", str(data_dir))
    create_schema()
    seed_data()
    return {"data_dir": data_dir, "db_path": db_path}


def test_fresh_installation_does_not_inherit_bundled_live_verified(governance_env):
    """Clean installation must NOT promote bundled reference live passes to local verified=true."""
    hosts = host_registry.discover_hosts()
    for host in hosts:
        for surface in host["discovery"]["surfaces"]:
            assert surface["verified"] is False, f"{host['id']} surface {surface['id']} claims verified without observation"
            for cap_name, cap_info in surface["capabilities"].items():
                assert cap_info["verified"] is False, f"{host['id']}.{cap_name} claims verified on clean install"

    codex_conn = connection_service.get_connection("codex")
    # Observations are empty on fresh installation
    assert codex_conn["discovery"]["observations"] == []
    assert codex_conn["discovery"]["local_observations_present"] is False
    # Reference baseline is distinctly marked
    ref = codex_conn["discovery"]["reference_acceptance"]
    assert ref is not None
    assert ref["is_reference_baseline"] is True
    assert ref["local_installation_verified"] is False

    host_registry.record_observation("codex", "memory_read", "cli", {"tool_name": "assemble_context"})
    observed = connection_service.get_connection("codex")["discovery"]
    assert observed["local_observations_present"] is True
    assert any(event["capability"] == "memory_read" for event in observed["observations"])
    assert "local_installation_verified" not in observed
    assert observed["reference_acceptance"]["local_installation_verified"] is False


def test_deleted_memory_tombstone_blocks_resurrection_via_distillation_and_superseding(governance_env):
    """Permanently deleted memory cannot be re-created, superseded, or resurrected by distillation."""
    project = project_service.create_project(name="Tombstone Governance")
    mem = content_service.create_content(
        title="Protected Architecture",
        content="Architecture design v1",
        project_id=project["id"],
        category="decision",
    )
    mem_id = mem["id"]

    # Delete the memory
    content_service.delete_content(mem_id, expected_version=1)

    # 1. Direct re-creation of identical body is blocked by 410 tombstone
    with pytest.raises(HTTPException) as exc:
        content_service.create_content(
            title="Protected Architecture",
            content="Architecture design v1",
            project_id=project["id"],
            category="decision",
        )
    assert exc.value.status_code == 410

    # 2. Attempting to supersede the deleted entity fails with 404
    with pytest.raises(HTTPException) as exc:
        content_service.create_content(
            title="New Architecture",
            content="Architecture design v2",
            project_id=project["id"],
            category="decision",
            supersedes_id=mem_id,
        )
    assert exc.value.status_code == 404

    # 3. Distillation proposing the deleted content hash is discarded
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Distill Stale Conversation",
        project_id=project["id"],
        messages=[{"role": "user", "content": "Let us discuss Protected Architecture: Architecture design v1"}],
    )
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        created = distillation_service.apply_distilled_memories(
            conn=conn,
            conv=dict(conv),
            memories=[{
                "category": "decision",
                "title": "Protected Architecture",
                "content": "Architecture design v1",
                "quote": "Architecture design v1",
            }],
            actor="omp",
            now="2026-10-25T12:00:00Z",
        )
    assert created == []
    with db() as conn:
        assert conn.execute("SELECT id FROM contents WHERE id = ?", (mem_id,)).fetchone() is None


def test_modified_memory_version_conflict_routes_to_conflict(governance_env):
    """Editing memory v1 to v2 prevents stale distillation targeting v1 from overwriting; routes to conflicts."""
    project = project_service.create_project(name="Conflict Governance")
    mem = content_service.create_content(
        title="DB Pool Size",
        content="Initial pool size 10",
        project_id=project["id"],
        category="decision",
    )
    # Update memory to version 2
    content_service.update_content(
        mem["id"],
        content="Updated pool size 20",
        expected_version=1,
    )
    updated = content_service.get_content(mem["id"])
    assert updated["version"] == 2
    assert updated["content"] == "Updated pool size 20"

    # Simulate distillation targeting version 1 with explicit supersedes
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Pool Tuning Session",
        project_id=project["id"],
        messages=[{"role": "user", "content": "Set pool size to 15 instead"}],
    )
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        created = distillation_service.apply_distilled_memories(
            conn=conn,
            conv=dict(conv),
            memories=[{
                "category": "decision",
                "title": "DB Pool Size",
                "content": "Set pool size to 15 instead",
                "quote": "Set pool size to 15 instead",
                "supersedes": {
                    "id": mem["id"],
                    "expected_version": 1,
                    "confidence": "explicit_confirmed",
                    "evidence_quote": "Set pool size to 15 instead",
                },
            }],
            actor="omp",
            now="2026-10-25T12:05:00Z",
        )

    # Invariant: did not supersede or overwrite; active memory remains v2
    assert created == []
    current = content_service.get_content(mem["id"])
    assert current["version"] == 2
    assert current["content"] == "Updated pool size 20"

    # Consumer-visible invariant: pending conflict was created for the existing memory
    conflicts = conflict_service.list_conflicts(project["id"], status="pending")
    assert len(conflicts) == 1
    assert conflicts[0]["existing_id"] == mem["id"]
    assert conflicts[0]["status"] == "pending"
    assert conflicts[0]["proposed"]["title"] == "DB Pool Size"
    assert conflicts[0]["proposed"]["content"] == "Set pool size to 15 instead"


def test_cross_project_supersession_is_rejected(governance_env):
    """Supersession across different project domains (including global vs project) must be rejected with 400."""
    p1 = project_service.create_project(name="Project Alpha")
    p2 = project_service.create_project(name="Project Beta")

    mem1 = content_service.create_content(
        title="Cache Policy",
        content="Cache in Redis",
        project_id=p1["id"],
        category="decision",
    )

    # Attempting to supersede from another project fails with 400
    with pytest.raises(HTTPException) as exc:
        content_service.create_content(
            title="Cache Policy",
            content="Cache in Memcached",
            project_id=p2["id"],
            category="decision",
            supersedes_id=mem1["id"],
        )
    assert exc.value.status_code == 400

    # Attempting to supersede project-scoped memory from global domain (project_id=None) fails with 400
    with pytest.raises(HTTPException) as exc:
        content_service.create_content(
            title="Cache Policy",
            content="Global cache policy",
            project_id=None,
            category="decision",
            supersedes_id=mem1["id"],
        )
    assert exc.value.status_code == 400


def test_independent_deletion_semantics_for_conversations_and_memories(governance_env):
    """Deleting conversation retains independent memories (marking provenance deleted); handoff cards cancelled."""
    project = project_service.create_project(name="Independent Deletion")
    conv = conversation_service.upsert_conversation(
        agent="codex",
        title="Design Conversation",
        project_id=project["id"],
        messages=[{"role": "user", "content": "We decided on SQLite WAL mode."}],
    )
    mem = content_service.create_content(
        title="SQLite Journal Mode",
        content="Use WAL mode for concurrency.",
        project_id=project["id"],
        category="decision",
        source_id=conv["id"],
        source_kind="conversation",
    )
    handoff = handoff_service.create_handoff(
        project_id=project["id"],
        target_agent="cursor",
        source_agent="codex",
    )
    assert handoff["status"] == "awaiting_confirmation"

    # Delete conversation
    conversation_service.delete_conversation(conv["id"])

    # Memory remains fully active and readable
    retained_mem = content_service.get_content(mem["id"])
    assert retained_mem["status"] == "active"
    assert retained_mem["title"] == "SQLite Journal Mode"
    # Source provenance reflects deletion
    sources = retained_mem["sources"]
    assert len(sources) == 1
    assert sources[0]["deleted"] is True

    # Handoff referencing deleted conversation is cancelled
    cancelled_handoff = handoff_service.get_handoff(handoff["id"])
    assert cancelled_handoff["status"] == "cancelled"
    assert "已永久删除" in cancelled_handoff["card"]["decisions"]

    # Deleting the memory does not affect other project data
    content_service.delete_content(mem["id"], expected_version=1)
    with pytest.raises(HTTPException) as exc:
        content_service.get_content(mem["id"])
    assert exc.value.status_code == 404
