"""Gold-standard memory quality: apply-time rules without calling a model."""
from app.database import create_schema, db, now_iso
from app.services import content_service, conversation_service, distillation_service, project_service, retrieval_service


def _apply(conv, memories, actor="omp"):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return distillation_service.apply_distilled_memories(conn, conv, memories, actor, now_iso())


def test_assistant_idea_is_not_an_active_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "memory-gold.db"))
    create_schema()
    project = project_service.create_project(name="Idea vs Decision")
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Cache chat",
        external_id="omp:idea",
        project_id=project["id"],
        messages=[
            {"role": "user", "content": "How should we cache this?"},
            {"role": "assistant", "content": "Maybe we should use Redis later."},
        ],
    )
    created = _apply(
        conv,
        [{
            "category": "decision",
            "title": "Cache",
            "content": "Use Redis.",
            "quote": "Maybe we should use Redis later.",
            "user_message_index": 1,
        }],
    )
    assert created == []
    active = content_service.list_content(project_id=project["id"], category="decision", status="active")
    assert active == []


def test_new_user_decision_replaces_old_and_retrieval_drops_historical(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "memory-replace.db"))
    create_schema()
    project = project_service.create_project(name="Replace Decision")
    old = content_service.create_content(
        title="Primary Database",
        content="Use PostgreSQL for all storage.",
        project_id=project["id"],
        category="decision",
        agent="omp",
    )
    quote = "Let's change our primary DB from PostgreSQL to SQLite. Use SQLite for all storage."
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="DB change",
        external_id="omp:db-change",
        project_id=project["id"],
        messages=[{"role": "user", "content": quote}],
    )
    created = _apply(
        conv,
        [{
            "category": "decision",
            "title": "Primary Database",
            "content": "Use SQLite for all storage.",
            "quote": quote,
            "user_message_index": 0,
            "supersedes": {
                "id": old["id"],
                "expected_version": 1,
                "evidence_quote": quote,
                "confidence": "explicit_confirmed",
                "reason": "User switched primary database",
            },
        }],
    )
    assert len(created) == 1
    pack = retrieval_service.assemble_context("which database", project_id=project["id"], consumer_id="codex")
    texts = " ".join(entry["text"] for entry in pack["entries"])
    assert "SQLite" in texts
    assert "PostgreSQL" not in texts
    assert content_service.get_content(old["id"])["status"] == "historical"


def test_projects_do_not_cross_supersede_or_pin_foreign_decisions(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "memory-isolate.db"))
    create_schema()
    project_a = project_service.create_project(name="Bridge A")
    project_b = project_service.create_project(name="Bridge B")
    a_decision = content_service.create_content(
        title="Material",
        content="Use paper.",
        project_id=project_a["id"],
        category="decision",
        agent="cursor",
    )
    content_service.create_content(
        title="Material",
        content="Use wood.",
        project_id=project_b["id"],
        category="decision",
        agent="cursor",
    )
    quote = "For this project use steel."
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="B tries to overwrite A",
        external_id="omp:cross",
        project_id=project_b["id"],
        messages=[{"role": "user", "content": quote}],
    )
    _apply(
        conv,
        [{
            "category": "decision",
            "title": "Material",
            "content": "Use steel.",
            "quote": quote,
            "user_message_index": 0,
            "supersedes": {
                "id": a_decision["id"],
                "expected_version": 1,
                "evidence_quote": quote,
                "confidence": "explicit_confirmed",
                "reason": "Illegal cross-project replace",
            },
        }],
    )
    assert content_service.get_content(a_decision["id"])["status"] == "active"
    pack_a = retrieval_service.assemble_context("material", project_id=project_a["id"], consumer_id="codex")
    pinned = [entry for entry in pack_a["entries"] if entry.get("tier") == "protected"]
    assert any("paper" in entry["text"] for entry in pinned)
    assert all("wood" not in entry["text"] and "steel" not in entry["text"] for entry in pinned)


def test_duplicate_capture_does_not_create_a_second_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "memory-dupe.db"))
    create_schema()
    project = project_service.create_project(name="Dedupe")
    quote = "Store sessions in SQLite."
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Store sessions",
        external_id="omp:dupe",
        project_id=project["id"],
        messages=[{"role": "user", "content": quote}],
    )
    memory = {
        "category": "decision",
        "title": "Session store",
        "content": "Store sessions in SQLite.",
        "quote": quote,
        "user_message_index": 0,
    }
    first = _apply(conv, [memory])
    second = _apply(conv, [memory])
    assert len(first) == 1
    assert second == []
    active = content_service.list_content(project_id=project["id"], category="decision", status="active")
    assert len(active) == 1


def test_user_edit_and_delete_are_visible_to_later_retrieval(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "memory-edit.db"))
    create_schema()
    project = project_service.create_project(name="Edit Delete")
    item = content_service.create_content(
        title="Port",
        content="Listen on 8420.",
        project_id=project["id"],
        category="decision",
        agent="user",
    )
    edited = content_service.update_content(item["id"], content="Listen on 18420.", expected_version=1)
    pack = retrieval_service.assemble_context("port", project_id=project["id"], consumer_id="codex")
    texts = " ".join(entry["text"] for entry in pack["entries"])
    assert "18420" in texts
    assert "Listen on 8420." not in texts
    content_service.delete_content(edited["id"], expected_version=2)
    after_delete = retrieval_service.assemble_context("port", project_id=project["id"], consumer_id="codex")
    deleted_texts = " ".join(entry["text"] for entry in after_delete["entries"])
    assert "18420" not in deleted_texts
    found = content_service.list_content(project_id=project["id"], query="port", status="active")
    assert found == []
