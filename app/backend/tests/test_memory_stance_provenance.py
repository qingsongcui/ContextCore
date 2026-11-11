"""Each memory stance keeps its source role, turn, quote, and version."""
from unittest.mock import patch

import pytest

from app.database import create_schema, db, seed_data, set_app_setting
from app.services import connection_service, conversation_service, distillation_service, processing_jobs, project_service
from app.transport import DistillResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


CASES = [
    (0, "fact", "事实记录", "本机只保存合成数据", "事实是本机只保存合成数据。"),
    (1, "preference", "偏好记录", "使用隔离数据库", "我习惯使用隔离数据库。"),
    (2, "decision", "决定记录", "采用隔离库", "最终决定采用隔离库。"),
    (3, "suggestion", "建议记录", "先不要改真实库", "建议先不要改真实库。"),
    (4, "assumption", "假设记录", "端口仍然可用", "假设端口仍然可用。"),
    (5, "negation", "否定记录", "不再采用旧库", "我不再采用旧库。"),
    (6, "quote", "引用记录", "外部文字不是本机决定", "引用外部文字：有人说要删库。"),
]


def test_each_stance_keeps_role_turn_quote_and_version():
    create_schema()
    project = project_service.create_project(name="Synthetic stances")
    messages = [{"role": "user", "content": text} for _, _, _, _, text in CASES]
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Stance source", messages=messages,
    )
    memories = [
        {
            "stance": stance,
            "category": "decision",
            "title": title,
            "content": content,
            "quote": text,
            "user_message_index": index,
            "source_message_index": index,
        }
        for index, stance, title, content, text in CASES
    ]
    result = distillation_service.auto_summarize_memory(
        project_id=project["id"], conversation_id=conversation["id"], memories=memories,
        actor="codex", auto_extract_from_messages=False,
    )
    saved = {item["title"]: item for item in result["saved_memories"]}
    assert set(saved) == {title for _, _, title, _, _ in CASES}
    with db() as conn:
        decisions = conn.execute(
            "SELECT title FROM contents WHERE category='decision' AND status='active'"
        ).fetchall()
        assert [row["title"] for row in decisions] == ["决定记录"]
        for index, stance, title, content, text in CASES:
            item = conn.execute("SELECT * FROM contents WHERE id=?", (saved[title]["id"],)).fetchone()
            tags = item["tags"]
            assert item["version"] == 1
            assert item["content"] == content
            assert f"stance:{stance}" in tags
            assert "role:user" in tags
            assert f"message_index:{index}" in tags
            assert f"source_version:{conversation['content_hash']}" in tags
            if stance == "decision":
                assert item["category"] == "decision"
            elif stance == "preference":
                assert item["category"] == "preference"
            else:
                assert item["category"] == "knowledge"
            source = conn.execute(
                "SELECT * FROM content_sources WHERE content_id=?", (item["id"],)
            ).fetchone()
            assert source["source_id"] == conversation["id"]
            assert source["quote"] == text
            version = conn.execute(
                "SELECT version, content FROM content_versions WHERE content_id=?", (item["id"],)
            ).fetchone()
            assert version["version"] == 1 and version["content"] == content


@pytest.mark.anyio
async def test_cancel_during_processing_is_not_replayed_by_restart(tmp_path, monkeypatch):
    path = tmp_path / "cancel-restart.db"
    monkeypatch.setenv("CONTEXTCORE_DB", str(path))
    create_schema(path)
    seed_data(path)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)
    text = "最终决定采用隔离库。"
    conversation = conversation_service.upsert_conversation(
        agent="omp", title="Cancel source", external_id="cancel-restart",
        messages=[{"id": "u1", "role": "user", "content": text}],
    )
    queued = distillation_service.queue_distillation(conversation["id"])

    async def cancel_before_commit(*_args, **_kwargs):
        with db() as conn:
            processing_jobs.cancel_conversation_jobs(conn, conversation["id"])
        return DistillResult(
            status="success",
            memories=[{
                "category": "decision", "title": "决定记录", "content": "采用隔离库",
                "quote": text, "user_message_index": 0,
            }],
        )

    with patch("app.transport.run_distill", side_effect=cancel_before_commit):
        result = await distillation_service.process_distillation_queue_item(queued["id"])
    assert result["ok"] is False
    assert processing_jobs.recover_processing_jobs() == {"pending": 0, "unknown": 0}
    assert processing_jobs.list_processing_jobs()[0]["state"] == "cancelled"
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM contents WHERE category='decision'").fetchone()[0] == 0
    resumed = distillation_service.auto_summarize_memory(
        project_id=conversation["project_id"], conversation_id=conversation["id"], actor="omp",
        auto_extract_from_messages=False,
        memories=[{
            "stance": "decision", "category": "decision", "title": "决定记录", "content": "采用隔离库",
            "quote": text, "user_message_index": 0, "source_message_index": 0,
        }],
    )
    assert resumed["saved_memories"][0]["category"] == "decision"
    with db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM contents WHERE category='decision' AND status='active'"
        ).fetchone()[0] == 1
