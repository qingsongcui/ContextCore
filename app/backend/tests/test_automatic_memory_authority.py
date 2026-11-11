"""Automatic ordinary memories share decision governance with model distillation."""
import pytest

from app.database import create_schema, db
from app.services import content_service, conversation_service, distillation_service, project_service


@pytest.fixture
def project():
    create_schema()
    return project_service.create_project(name="Synthetic authority")


def capture(project, messages, memories=None):
    conv = conversation_service.upsert_conversation(agent="codex", project_id=project["id"], title="Authority source",
                                                    messages=messages)
    result = distillation_service.auto_summarize_memory(project_id=project["id"], conversation_id=conv["id"],
                                                       memories=memories, actor="codex")
    return conv, result


@pytest.mark.parametrize("role,text", [
    ("assistant", "建议：我们决定采用 PostgreSQL；如果用户同意再做，目前尚未确认。"),
    ("assistant", "We decided to use PostgreSQL."),
    ("user", "我尚未决定采用 PostgreSQL。"),
    ("user", "引用外部文字：最终决定采用 PostgreSQL。"),
    ("user", "If we decided to adopt PostgreSQL, migration would be needed."),
    ("unknown", "最终决定采用 PostgreSQL。"),
])
def test_suggestion_negation_quote_and_unknown_role_never_become_decision(project, role, text):
    capture(project, [{"role": role, "content": text}], [
        {"category": "decision", "title": "Database", "content": "Use PostgreSQL", "quote": text, "user_message_index": 0}
    ])
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM contents WHERE category='decision' AND status='active'").fetchone()[0] == 0


def test_explicit_change_retires_only_named_version_with_source_evidence(project):
    old = content_service.create_content(title="Database", content="Use SQLite", category="decision", agent="codex", project_id=project["id"])
    text = "最终决定改用 PostgreSQL 替代 SQLite。"
    conv, result = capture(project, [{"role": "user", "content": text}], [
        {"category": "decision", "title": "Database", "content": "改用 PostgreSQL 替代 SQLite", "quote": text,
         "user_message_index": 0, "supersedes": {"id": old["id"], "expected_version": old["version"],
         "confidence": "explicit_confirmed", "evidence_quote": text}}
    ])
    assert content_service.get_content(old["id"])["status"] == "historical"
    replacement = next(m for m in result["saved_memories"] if m["title"] == "Database")
    saved = content_service.get_content(replacement["id"])
    assert saved["status"] == "active"
    assert "role:user" in saved["tags"]
    assert "message_index:0" in saved["tags"]
    source_revision = conversation_service.get_conversation(conv["id"])["content_hash"]
    assert f"source_version:{source_revision}" in saved["tags"]
    with db() as conn:
        source = conn.execute("SELECT * FROM content_sources WHERE content_id=?", (saved["id"],)).fetchone()
        assert source["source_id"] == conv["id"] and source["quote"] == text


def test_two_shared_characters_do_not_count_as_user_support():
    assert distillation_service._claim_supported_by_quote("改成 PostgreSQL", "请使用新的配置方案") is False
    assert distillation_service._claim_supported_by_quote("使用 PostgreSQL", "确定使用 PostgreSQL。") is True


def test_same_title_without_explicit_supersedes_stays_pending(project):
    old = content_service.create_content(title="Database", content="Use SQLite", category="decision", agent="codex", project_id=project["id"])
    text = "确定使用 PostgreSQL。"
    _, result = capture(project, [{"role": "user", "content": text}, {"role": "user", "content": "项目背景：这是本机桌面工具。"}], [
        {"category": "decision", "title": "Database", "content": "使用 PostgreSQL", "quote": text, "user_message_index": 0}
    ])
    assert content_service.get_content(old["id"])["status"] == "active"
    with db() as conn:
        conflict = conn.execute(
            "SELECT reason FROM conflicts WHERE existing_id=? AND status='pending'", (old["id"],),
        ).fetchone()
    assert conflict and "matches" in conflict["reason"].lower()
    assert any(m["category"] == "background" for m in result["saved_memories"])


def test_consolidate_skips_a_tombstoned_merged_body(project):
    from app.database import new_id, now_iso
    shared = "Keep the local service on the loopback port."
    extra = "Restart after an upgrade."
    content_service.create_content(
        title="Relay note", content=shared, category="knowledge", agent="codex", project_id=project["id"],
    )
    newer = content_service.create_content(
        title="Relay note", content=f"{shared}\n{extra}", category="knowledge", agent="codex", project_id=project["id"],
    )
    digest = content_service.compute_content_hash(project["id"], "knowledge", "Relay note", f"{shared}\n{extra}")
    with db() as conn:
        conn.execute(
            """INSERT INTO tombstones
               (id, entity_type, dedupe_key, scope_project_id, deleted_at, deleted_by, reason)
               VALUES (?, 'content', ?, ?, ?, 'user', 'deleted body')""",
            (new_id("tomb"), digest, project["id"], now_iso()),
        )
    result = content_service.consolidate_memories(project["id"])
    assert result["merged_groups"] == 0
    assert content_service.get_content(newer["id"])["status"] == "active"
    with db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM contents WHERE title='Relay note' AND status='active'",
        ).fetchone()[0]
    assert active == 2
