"""Promotion evidence gate: a task lesson must be backed by real source-conversation evidence.

Covers the regression where an approved task with no source conversation, no
completed steps, and an invented `evidence_quote` could still mint a
`verified_outcome` memory.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.database import create_schema, db, encode
from app.services import content_service, conversation_service, project_service, working_memory_service


@pytest.fixture
def evidence_env(tmp_path, monkeypatch):
    db_path = tmp_path / "task_lesson_evidence.sqlite"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_path))
    create_schema(db_path)
    repo_dir = tmp_path / "evidence-repo"
    repo_dir.mkdir()
    project = project_service.create_project("Evidence Project", "test", [str(repo_dir)])
    other = project_service.create_project("Other Project", "test", [str(tmp_path / "other-repo")])
    return {"db_path": db_path, "project": project, "other": other, "tmp_path": tmp_path}


def _conversation(project_id, content: str, *, external_id: str = "conv-evidence-1", role: str = "user"):
    return conversation_service.upsert_conversation(
        agent="codex",
        project_id=project_id,
        external_id=external_id,
        title="Task evidence conversation",
        messages=[{"role": role, "content": content, "timestamp": "2026-11-12T00:00:00Z"}],
    )


def _task(project_id, *, conversation_id=None, execution_mode: str = "approved", **fields):
    return working_memory_service.upsert_working_task(
        project_id=project_id,
        title=fields.pop("title", "修正限流窗口实现"),
        goal=fields.pop("goal", "让限流窗口行为稳定"),
        execution_mode=execution_mode,
        source_conversation_id=conversation_id,
        agent="codex",
        **fields,
    )


def _memory_ids(project_id) -> set[str]:
    return {
        item["id"]
        for item in content_service.list_content(project_id=project_id, status="all", limit=200)
    }


def _promote_kwargs(**overrides):
    kwargs = {
        "title": "限流窗口参数经验",
        "content": "限流窗口固定为 60 秒可以避免边界抖动。",
        "category": "lesson",
        "applicability": {"when": "调整限流窗口参数时", "env": "local-desktop"},
    }
    kwargs.update(overrides)
    return kwargs


def _assert_422_no_writes(project_id, task_id, before_ids, before_task, **kwargs):
    with pytest.raises(HTTPException) as exc:
        working_memory_service.promote_task_conclusion(task_id, **kwargs)
    assert exc.value.status_code in (409, 422), exc.value.detail
    assert _memory_ids(project_id) == before_ids
    after_task = working_memory_service.get_working_task(task_id)
    assert after_task["status"] == before_task["status"]
    assert after_task["version"] == before_task["version"]
    assert after_task["completed_at"] == before_task["completed_at"]
    assert all(event["event_type"] != "promoted" for event in after_task["events"])
    return exc.value


def test_agent_promotion_requires_real_source_evidence_and_is_source_backed(evidence_env):
    from app.services import governance_guardrails

    assert "source_backed" in governance_guardrails.VALID_ORIGIN_AUTHORITIES
    assert "source_backed" not in governance_guardrails.AUTHORITATIVE_ORIGINS

    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "请确认把限流窗口改成 60 秒，测试抖动消失了。")
    task = _task(pid, conversation_id=conv["id"], status="in_progress")
    before_ids = _memory_ids(pid)

    promoted = working_memory_service.promote_task_conclusion(
        task["id"],
        **_promote_kwargs(
            title="限流窗口参数经验",
            content="限流窗口固定为 60 秒可以避免边界抖动。",
            evidence_quote="把限流窗口改成 60 秒",
        ),
        actor="codex",
        complete_task=True,
    )

    assert promoted["promoted"] is True
    assert promoted["action"] == "ADD"
    assert promoted["conflict"] is None
    memory = promoted["memory"]
    assert memory["origin_authority"] == "source_backed"
    assert memory["origin_authority"] not in ("verified_outcome", "user_explicit")
    assert memory["promoted_from_task_id"] == task["id"]
    assert len(memory["sources"]) == 1
    assert memory["sources"][0]["kind"] == "conversation"
    assert memory["sources"][0]["id"] == conv["id"]
    assert memory["sources"][0]["quote"] == "把限流窗口改成 60 秒"
    assert _memory_ids(pid) == before_ids | {memory["id"]}
    assert promoted["task"]["status"] == "completed"
    assert promoted["task"]["completed_at"] is not None
    assert any(event["event_type"] == "promoted" for event in promoted["task"]["events"])


def test_manual_promotion_still_needs_source_evidence_and_stays_verified_outcome(evidence_env):
    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "请把限流窗口统一为 60 秒。", external_id="conv-manual-evidence")
    task = _task(pid, conversation_id=conv["id"])

    promoted = working_memory_service.promote_task_conclusion(
        task["id"],
        **_promote_kwargs(
            title="限流窗口人工确认经验",
            content="限流窗口统一为 60 秒，作为团队默认配置。",
            evidence_quote="把限流窗口统一为 60 秒",
        ),
        actor="user",
    )
    assert promoted["promoted"] is True
    assert promoted["memory"]["origin_authority"] == "verified_outcome"

    # A manual actor gets no free pass: removing the source conversation blocks promotion.
    source_less = _task(pid, title="无来源人工任务")
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(source_less["id"])
    exc = _assert_422_no_writes(
        pid,
        source_less["id"],
        before_ids,
        before_task,
        **_promote_kwargs(
            title="人工编造经验",
            content="限流窗口固定为 30 秒即可。",
            evidence_quote="把限流窗口统一为 30 秒",
        ),
        actor="user",
    )
    assert "source conversation" in exc.detail


def test_promotion_without_source_conversation_writes_nothing(evidence_env):
    pid = evidence_env["project"]["id"]
    task = _task(pid, status="in_progress")
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    exc = _assert_422_no_writes(
        pid,
        task["id"],
        before_ids,
        before_task,
        **_promote_kwargs(
            title="任意编造的限流经验",
            content="限流窗口固定为 15 秒即可。",
            evidence_quote="我明确要求把窗口改成 15 秒",
        ),
        actor="codex",
        complete_task=True,
    )
    assert "source conversation" in exc.detail


def test_promotion_requires_nonempty_quote(evidence_env):
    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "请把限流窗口改成 60 秒。")
    task = _task(pid, conversation_id=conv["id"])
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    for quote in (None, "", "   "):
        exc = _assert_422_no_writes(
            pid,
            task["id"],
            before_ids,
            before_task,
            **_promote_kwargs(evidence_quote=quote),
            actor="codex",
        )
        assert "evidence_quote" in exc.detail


def test_quote_not_present_in_message_content_is_rejected(evidence_env):
    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "请把限流窗口改成 60 秒。")
    task = _task(pid, conversation_id=conv["id"])
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    exc = _assert_422_no_writes(
        pid,
        task["id"],
        before_ids,
        before_task,
        **_promote_kwargs(evidence_quote="把部署脚本迁移到 Docker"),
        actor="codex",
    )
    assert "not found" in exc.detail


def test_quote_in_metadata_ids_or_task_slots_cannot_certify_a_lesson(evidence_env):
    pid = evidence_env["project"]["id"]
    metadata_quote = "部署脚本迁移到 Docker"
    conv = conversation_service.upsert_conversation(
        agent="codex",
        project_id=pid,
        external_id="conv-metadata-evidence",
        title="Metadata-only quote conversation",
        messages=[
            {
                "id": metadata_quote,
                "external_id": metadata_quote,
                "role": "user",
                "content": "请把限流窗口改成 60 秒。",
                "timestamp": "2026-11-12T00:00:00Z",
            }
        ],
    )
    task = _task(
        pid,
        conversation_id=conv["id"],
        goal=metadata_quote,
        current_state=metadata_quote,
        completed_steps=[{"step": metadata_quote, "evidence": metadata_quote}],
    )
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    exc = _assert_422_no_writes(
        pid,
        task["id"],
        before_ids,
        before_task,
        **_promote_kwargs(evidence_quote=metadata_quote),
        actor="codex",
    )
    assert "not found in the linked source conversation messages" in exc.detail


def test_source_conversation_from_another_project_is_rejected(evidence_env):
    pid = evidence_env["project"]["id"]
    other_pid = evidence_env["other"]["id"]
    foreign_conv = _conversation(other_pid, "请把限流窗口改成 60 秒。", external_id="conv-foreign-evidence")
    task = _task(pid, conversation_id=foreign_conv["id"])
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    exc = _assert_422_no_writes(
        pid,
        task["id"],
        before_ids,
        before_task,
        **_promote_kwargs(evidence_quote="把限流窗口改成 60 秒"),
        actor="codex",
    )
    assert "different project" in exc.detail


def test_deleted_source_conversation_is_rejected(evidence_env):
    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "请把限流窗口改成 60 秒。")
    task = _task(pid, conversation_id=conv["id"])
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])
    with db() as conn:
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv["id"],))

    exc = _assert_422_no_writes(
        pid,
        task["id"],
        before_ids,
        before_task,
        **_promote_kwargs(evidence_quote="把限流窗口改成 60 秒"),
        actor="codex",
    )
    assert "no longer exists" in exc.detail


def test_unrelated_genuine_quote_cannot_certify_arbitrary_lesson(evidence_env):
    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "把部署脚本迁移到 Docker 镜像里，记得保留回滚脚本。")
    task = _task(pid, conversation_id=conv["id"])
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    exc = _assert_422_no_writes(
        pid,
        task["id"],
        before_ids,
        before_task,
        **_promote_kwargs(
            title="限流窗口参数经验",
            content="限流窗口固定为 60 秒可以避免边界抖动。",
            evidence_quote="把部署脚本迁移到 Docker",
        ),
        actor="codex",
    )
    assert "unrelated" in exc.detail


def test_discussion_only_task_cannot_promote_with_agent_actor(evidence_env):
    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "请把限流窗口改成 60 秒。")
    task = _task(pid, conversation_id=conv["id"], execution_mode="discussion_only")
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    exc = _assert_422_no_writes(
        pid,
        task["id"],
        before_ids,
        before_task,
        **_promote_kwargs(evidence_quote="把限流窗口改成 60 秒"),
        actor="codex",
    )
    assert "Discussion-only" in exc.detail


def test_conflicting_promotion_preserves_conflict_without_completing_task(evidence_env):
    pid = evidence_env["project"]["id"]
    existing = content_service.create_content(
        title="限流窗口规则",
        content="限流窗口固定为 30 秒。",
        project_id=pid,
        category="decision",
        origin_authority="user_explicit",
    )
    conv = _conversation(pid, "请把限流窗口从 30 秒改成 60 秒，测试抖动消失了。")
    task = _task(pid, conversation_id=conv["id"], status="in_progress")
    before_memory_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    result = working_memory_service.promote_task_conclusion(
        task["id"],
        **_promote_kwargs(
            title="限流窗口规则",
            content="限流窗口应当改成 60 秒并保留抖动缓冲。",
            evidence_quote="把限流窗口从 30 秒改成 60 秒",
        ),
        actor="codex",
        complete_task=True,
    )

    assert result["action"] == "OPEN_CONFLICT"
    assert result["promoted"] is False
    assert result["memory"] is None
    conflict = result["conflict"]
    assert conflict["id"]
    assert conflict["existing_id"] == existing["id"]

    after_task = working_memory_service.get_working_task(task["id"])
    assert after_task["status"] == before_task["status"] == "in_progress"
    assert after_task["version"] == before_task["version"]
    assert after_task["completed_at"] is None
    assert all(event["event_type"] != "promoted" for event in after_task["events"])
    assert _memory_ids(pid) == before_memory_ids
    with db() as conn:
        pending = conn.execute(
            "SELECT id, status FROM conflicts WHERE id = ?", (conflict["id"],)
        ).fetchone()
    assert pending is not None and pending["status"] == "pending"


def test_task_change_during_reconcile_rolls_back_memory_and_refuses_success(evidence_env, monkeypatch):
    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "请把限流窗口改成 60 秒。")
    task = _task(pid, conversation_id=conv["id"], status="in_progress")
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    original = content_service.reconcile_long_term_memory

    def racing_reconcile(**kwargs):
        result = original(**kwargs)
        kwargs["_connection"].execute(
            "UPDATE working_tasks SET version = version + 1 WHERE id = ?", (task["id"],)
        )
        return result

    monkeypatch.setattr(content_service, "reconcile_long_term_memory", racing_reconcile)
    with pytest.raises(HTTPException) as exc:
        working_memory_service.promote_task_conclusion(
            task["id"],
            **_promote_kwargs(evidence_quote="把限流窗口改成 60 秒"),
            actor="codex",
            complete_task=True,
        )
    assert exc.value.status_code == 409
    assert "changed during promotion" in exc.value.detail
    assert _memory_ids(pid) == before_ids
    after_task = working_memory_service.get_working_task(task["id"])
    assert after_task["status"] == before_task["status"]
    assert after_task["version"] == before_task["version"]
    assert all(event["event_type"] != "promoted" for event in after_task["events"])


def test_source_change_during_reconcile_rolls_back_memory_and_refuses_success(evidence_env, monkeypatch):
    pid = evidence_env["project"]["id"]
    conv = _conversation(pid, "请把限流窗口改成 60 秒。")
    task = _task(pid, conversation_id=conv["id"])
    before_ids = _memory_ids(pid)
    before_task = working_memory_service.get_working_task(task["id"])

    original = content_service.reconcile_long_term_memory

    def racing_reconcile(**kwargs):
        result = original(**kwargs)
        kwargs["_connection"].execute(
            "UPDATE conversations SET messages = ? WHERE id = ?", (encode([]), conv["id"])
        )
        return result

    monkeypatch.setattr(content_service, "reconcile_long_term_memory", racing_reconcile)
    with pytest.raises(HTTPException) as exc:
        working_memory_service.promote_task_conclusion(
            task["id"],
            **_promote_kwargs(evidence_quote="把限流窗口改成 60 秒"),
            actor="codex",
            complete_task=True,
        )
    assert exc.value.status_code == 409
    assert "Source conversation changed" in exc.value.detail
    assert _memory_ids(pid) == before_ids
    after_task = working_memory_service.get_working_task(task["id"])
    assert after_task["status"] == before_task["status"]
    assert after_task["version"] == before_task["version"]
    with db() as conn:
        restored = conn.execute("SELECT messages FROM conversations WHERE id = ?", (conv["id"],)).fetchone()
    assert "把限流窗口改成 60 秒" in restored["messages"]