"""Named P1-024 regressions: budget, languages, sessions, deletion, and tool pairs.

Synthetic data only. These cases name the scenarios that were previously
implemented but not called out together.
"""
import json

import pytest

from app.database import create_schema
from app.services import content_service, conversation_service, handoff_service, project_service, retrieval_service


def _project(name: str):
    return project_service.create_project(name=name, directories=[f"/synthetic/{name}"])


def _task(project_id: str):
    return content_service.create_content(
        title="Synthetic task", content="Continue the selected task", category="task",
        project_id=project_id, agent="codex", tags=["status:in_progress"],
    )


def _relay(project_id: str, task_id: str, mode: str, budget: int, language: str):
    return handoff_service.relay_project(
        project_id=project_id, consumer="omp", task_id=task_id, context_mode=mode,
        max_tokens=budget, sync_first=False, language=language,
    )


def test_exact_serialized_budget_and_unknown_host_room():
    payload = {"status": "ready", "text": "中文 tool result 😀" * 5}
    fitted = retrieval_service.bound_context_response(payload, 32000, language="zh")
    budget = fitted["estimated_tokens"]
    for _ in range(5):
        response = retrieval_service.bound_context_response(payload, budget, language="en")
        budget = response["required_tokens"] if response["status"] == "insufficient_budget" else response["estimated_tokens"]
    exact = retrieval_service.bound_context_response(payload, budget, language="en")
    short = retrieval_service.bound_context_response(payload, budget - 1, language="zh")
    assert exact["status"] == "ready"
    assert exact["estimated_tokens"] == len(json.dumps(exact, ensure_ascii=False).encode()) == budget
    assert exact["host_context_usage"] == "unknown"
    assert short["status"] == "insufficient_budget"
    assert short["must_not_execute"] is True
    assert short["host_context_usage"] == "unknown"
    assert "text" not in short
    assert "中文 tool result" not in json.dumps(short, ensure_ascii=False)
    assert "剩余" not in short["message"]
    assert "remaining" not in exact["message"] if "message" in exact else True


@pytest.mark.parametrize("mode", ["full", "compressed"])
@pytest.mark.parametrize("language", ["zh", "en"])
def test_large_document_refuses_at_256_and_at_the_maximum(mode, language):
    create_schema()
    project = _project(f"doc198-{mode}-{language}")
    task = _task(project["id"])
    prefix = "DOC198_MARKER_"
    body = prefix + ("A" * (198 * 1024 - len(prefix.encode())))
    assert len(body.encode()) == 198 * 1024
    content_service.create_content(
        title="Large constraint", content=body, category="decision", project_id=project["id"], agent="codex",
    )
    for budget in (256, 32000):
        result = _relay(project["id"], task["id"], mode, budget, language)
        encoded = json.dumps(result, ensure_ascii=False)
        assert result["status"] == "insufficient_budget"
        assert result["must_not_execute"] is True
        assert result["host_context_usage"] == "unknown"
        assert result["continuation"]["resume_from"] is None
        assert result["continuation"]["must_not_execute"] is True
        assert "DOC198_MARKER_" not in encoded
        assert not result.get("context_text")


def test_chinese_and_english_long_text_stay_intact_in_both_modes():
    create_schema()
    project = _project("long-text")
    task = _task(project["id"])
    chinese = "ZH_LONG_" + ("必须保留这段中文约束。" * 22)
    english = "EN_LONG_" + ("Do not translate this sentence. " * 7)
    assert len(chinese) > 200 and len(english) > 200
    content_service.create_content(
        title="Chinese constraint", content=chinese, category="decision", project_id=project["id"], agent="codex",
    )
    conversation_service.upsert_conversation(
        agent="codex", title="English goal", external_id="synthetic:long-text",
        project_id=project["id"], cwd="/synthetic/long-text",
        messages=[{"role": "user", "content": english}, {"role": "assistant", "content": "received"}],
        updated_at="2026-11-01T00:00:01+00:00",
    )
    for mode in ("compressed", "full"):
        for language in ("zh", "en"):
            result = _relay(project["id"], task["id"], mode, 32000, language)
            assert result["status"] == "relayed"
            assert result["host_context_usage"] == "unknown"
            text = result["context_text"]
            assert chinese in text
            assert english in text


def test_multiple_sessions_deleted_source_and_tool_pairs_agree_across_modes():
    create_schema()
    project = _project("multi-session")
    task = _task(project["id"])
    deleted = content_service.create_content(
        title="Deleted source", content="DELETED_SOURCE_MARKER", category="decision",
        project_id=project["id"], agent="codex",
    )
    content_service.delete_content(deleted["id"])
    live = "LIVE_CONSTRAINT_MARKER"
    content_service.create_content(
        title="Live constraint", content=live, category="decision", project_id=project["id"], agent="codex",
    )
    argument = '{"path":"' + ("目录" * 40) + '"}'
    result_text = "RESULT_FULL_" + ("字节" * 40)
    hidden = "HIDDEN_SESSION_MARKER"
    conversation_service.upsert_conversation(
        agent="codex", title="Oldest session", external_id="synthetic:hidden",
        project_id=project["id"], cwd="/synthetic/multi-session",
        messages=[{"role": "user", "content": hidden}],
        updated_at="2026-11-01T00:00:00+00:00",
    )
    for index in range(1, 5):
        conversation_service.upsert_conversation(
            agent="codex", title=f"Kept session {index}", external_id=f"synthetic:keep-{index}",
            project_id=project["id"], cwd="/synthetic/multi-session",
            messages=[{"role": "user", "content": f"KEEP_SESSION_{index}"}],
            updated_at=f"2026-11-01T00:00:0{index}+00:00",
        )
    conversation_service.upsert_conversation(
        agent="codex", title="Newest tool session", external_id="synthetic:keep-5",
        project_id=project["id"], cwd="/synthetic/multi-session",
        messages=[
            {"role": "user", "content": "KEEP_SESSION_5"},
            {"role": "assistant", "content": "calling", "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": argument}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": result_text},
            {"role": "tool", "content": "ORPHAN_NOT_A_FINISHED_RESULT"},
        ],
        updated_at="2026-11-01T00:00:05+00:00",
    )

    compressed = _relay(project["id"], task["id"], "compressed", 32000, "zh")
    full = _relay(project["id"], task["id"], "full", 32000, "zh")
    assert compressed["status"] == full["status"] == "relayed"
    assert compressed["host_context_usage"] == full["host_context_usage"] == "unknown"
    assert compressed["omitted_conversations"] == 1
    assert full["omitted_conversations"] == 0
    compact = compressed["context_text"]
    complete = full["context_text"]
    for marker in ("KEEP_SESSION_1", "KEEP_SESSION_2", "KEEP_SESSION_3", "KEEP_SESSION_4", "KEEP_SESSION_5"):
        assert marker in compact and marker in complete
    assert hidden not in compact
    assert "另有 1 段会话没有写入这段摘要" in compact
    assert "工作现场未知" in compact
    assert hidden in complete
    assert argument in compact and argument in complete
    assert result_text in compact and result_text in complete
    assert "未配对" in compact and "未配对" in complete
    assert "-> ORPHAN_NOT_A_FINISHED_RESULT" not in compact
    assert "-> ORPHAN_NOT_A_FINISHED_RESULT" not in complete
    assert live in compact and live in complete
    assert "DELETED_SOURCE_MARKER" not in compact
    assert "DELETED_SOURCE_MARKER" not in complete
    assert "DELETED_SOURCE_MARKER" not in json.dumps(compressed, ensure_ascii=False)
    assert "DELETED_SOURCE_MARKER" not in json.dumps(full, ensure_ascii=False)
