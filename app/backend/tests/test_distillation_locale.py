from app.database import create_schema, db
from app.services import content_service, conversation_service, distillation_service, project_service


def test_implicit_capture_uses_substantive_user_language_and_preserves_original_text():
    preference = 'Always use "原文标题" as the release heading.'
    original_progress = "这一轮已经验证。"
    messages = [
        {"role": "user", "content": preference},
        {"role": "assistant", "content": original_progress},
        {"role": "user", "content": "收到"},
    ]

    result = distillation_service.extract_implicit_memories_from_messages(messages)
    memory = result["memories"][0]
    assert memory["title"].startswith("User preference: ")
    assert memory["content"] == memory["quote"] == preference
    assert result["process_summary"].startswith("User goal or question: " + preference)
    assert "Latest progress: " + original_progress in result["process_summary"]

    translated_labels = distillation_service.extract_implicit_memories_from_messages(messages, language="zh")
    assert translated_labels["memories"][0]["title"].startswith("用户偏好与规范：")
    assert translated_labels["memories"][0]["quote"] == preference
    assert "最近进展：" + original_progress in translated_labels["process_summary"]


def test_checkpoint_locale_preserves_existing_identity_and_tombstone():
    create_schema()
    project = project_service.create_project(name="Documentation")
    conv = conversation_service.upsert_conversation(
        agent="codex",
        project_id=project["id"],
        title="Release workflow",
        messages=[{"role": "user", "content": "Always use the release checklist for deployment."}],
    )
    first = distillation_service.auto_summarize_memory(conversation_id=conv["id"], actor="codex", language="zh")
    memory = first["saved_memories"][0]
    assert memory["title"].startswith("用户偏好与规范：")
    assert first["summary_note"] == "记忆检查点包含 1 条记忆。"

    localized_receipt = distillation_service.auto_summarize_memory(conversation_id=conv["id"], actor="codex", language="en")
    assert localized_receipt["summary_note"] == "Memory checkpoint includes 1 item."
    assert localized_receipt["created_count"] == 0
    assert localized_receipt["unchanged_count"] == 1
    assert localized_receipt["saved_memories"][0]["id"] == memory["id"]
    assert localized_receipt["saved_memories"][0]["title"] == memory["title"]

    content_service.delete_content(memory["id"], expected_version=memory["version"])
    replay = distillation_service.auto_summarize_memory(conversation_id=conv["id"], actor="codex", language="en")
    assert replay["saved_count"] == 0
    assert replay["skipped_memories"][0]["action"] == "skipped_tombstoned"
    with db() as conn:
        assert not conn.execute("SELECT id FROM contents").fetchall()
