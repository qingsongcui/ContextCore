"""Protected bootstrap budget: safety rules and temp_constraints are never cut.

Synthetic data only. The confirmable bug was an approved task with 10
`temp_constraints` (the last one a real restriction) returning only three of
them with `status=ready` / `must_not_execute=false` at `max_tokens=3200`, and a
rule's last sentence disappearing into an ellipsis.

Every assertion reads the actual returned serialization and the real
`saved -> provided` receipts, not an internal estimate.
"""
import json

import pytest

from app import mcp_server
from app.database import create_schema, db
from app.services import (
    conflict_service,
    content_service,
    project_service,
    retrieval_service,
    telemetry_service,
    working_memory_service,
)


@pytest.fixture
def boot_env(tmp_path, monkeypatch):
    db_path = tmp_path / "bootstrap-protected.sqlite"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_path))
    create_schema(db_path)
    repo = tmp_path / "protected-repo"
    repo.mkdir()
    project = project_service.create_project(
        name="Protected Budget Repo",
        description="bootstrap protected budget contract",
        directories=[str(repo)],
    )
    return {"project": project, "repo": repo, "db_path": db_path}


def _encoded(payload):
    return json.dumps(payload, ensure_ascii=False)


def _provided_count(content_id):
    return telemetry_service.get_usage_summaries([content_id])[content_id]["provided_count"]


def _provided_event_count():
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM memory_usage_events WHERE event_type = 'provided'"
        ).fetchone()[0]


def test_long_user_restriction_as_last_constraint_is_delivered_whole(boot_env):
    project_id = boot_env["project"]["id"]
    leading = [f"阶段限制 {index}：只改本阶段文件" for index in range(9)]
    final_rule = (
        "本次任务禁止重写任何原生 Agent 的配置、Hook、技能与模型选择；"
        "只允许在产品仓库内改动，交付前必须逐条回读；Never rewrite native agent settings。"
    )
    task = working_memory_service.upsert_working_task(
        project_id=project_id,
        title="收紧宿主配置写入边界",
        execution_mode="approved",
        goal="只在产品内改代码并保持宿主只读",
        temp_constraints=[*leading, final_rule],
        cwd=str(boot_env["repo"]),
    )

    res = retrieval_service.bootstrap_working_memory(
        project_id=project_id, task_id=task["id"], consumer_id="codex", max_tokens=3200
    )
    encoded = _encoded(res)
    assert res["estimated_tokens"] == len(encoded.encode("utf-8"))
    assert res["status"] == "ready", encoded
    assert res["estimated_tokens"] <= 3200
    assert res["must_not_execute"] is False

    expected = [*leading, final_rule]
    assert res["selected_task"]["temp_constraints"] == expected
    assert res["active_tasks"][0]["temp_constraints"] == expected
    assert final_rule in encoded
    assert final_rule.endswith("Never rewrite native agent settings。")
    assert "..." not in "".join(res["selected_task"]["temp_constraints"])


def test_rule_last_sentence_survives_and_short_budget_refuses_exactly(boot_env):
    project_id = boot_env["project"]["id"]
    body = ("这段是历史背景与决策过程，不属于安全规则的最后一句。" * 40) + "最后一条：Never rewrite native agent settings."
    rule = content_service.create_content(
        title="宿主配置边界",
        content=body,
        category="decision",
        project_id=project_id,
        agent="user",
    )

    ready = retrieval_service.bootstrap_working_memory(
        project_id=project_id, consumer_id="codex", max_tokens=32000
    )
    assert ready["status"] == "ready"
    delivered = next(item for item in ready["applicable_constraints"] if item["id"] == rule["id"])
    assert delivered["content"] == body
    assert delivered["content"].endswith("Never rewrite native agent settings.")
    assert _provided_count(rule["id"]) == 1

    refused = retrieval_service.bootstrap_working_memory(
        project_id=project_id, consumer_id="codex", max_tokens=600
    )
    refused_encoded = _encoded(refused)
    assert refused["status"] == "insufficient_budget"
    assert refused["ok"] is False
    assert refused["must_not_execute"] is True
    assert refused["applicable_constraints"] == []
    assert refused["active_tasks"] == []
    assert refused["selected_task"] is None
    assert refused["budget_tokens"] == 600
    assert refused["required_tokens"] > 600
    assert refused["shortfall_tokens"] == refused["required_tokens"] - 600
    assert refused["estimated_tokens"] == len(refused_encoded.encode("utf-8"))
    assert "Never rewrite" not in refused_encoded
    assert "历史背景与决策过程" not in refused_encoded
    # A refusal emits no source text, so it must not claim a new delivery receipt.
    assert _provided_count(rule["id"]) == 1

    exact = retrieval_service.bootstrap_working_memory(
        project_id=project_id, consumer_id="codex", max_tokens=refused["required_tokens"]
    )
    assert exact["status"] == "ready"
    assert exact["estimated_tokens"] <= refused["required_tokens"]
    assert next(item for item in exact["applicable_constraints"] if item["id"] == rule["id"])["content"] == body


def test_many_conflicts_small_budget_refuses_instead_of_cutting_the_rule(boot_env):
    project_id = boot_env["project"]["id"]
    body = "PROTECTED_RULE_MARKER " + ("规则必须完整交付，不允许省略任何一句。" * 90)
    rule = content_service.create_content(
        title="完整交付规则",
        content=body,
        category="decision",
        project_id=project_id,
        agent="user",
    )
    conflict_ids = []
    for index in range(12):
        conflict = conflict_service.create_conflict(
            project_id=project_id,
            existing_id=rule["id"],
            proposed={"title": f"提案 {index}", "content": f"候选规则 {index}"},
            reason=f"并发提案 {index} 与现有规则冲突",
        )
        conflict_ids.append(conflict["id"])

    refused = retrieval_service.bootstrap_working_memory(
        project_id=project_id, consumer_id="codex", max_tokens=1500
    )
    refused_encoded = _encoded(refused)
    assert refused["status"] == "insufficient_budget"
    assert refused["must_not_execute"] is True
    assert refused["required_tokens"] > 1500
    assert refused["pending_conflicts"] == []
    assert "PROTECTED_RULE_MARKER" not in refused_encoded
    assert _provided_count(rule["id"]) == 0

    roomy = retrieval_service.bootstrap_working_memory(
        project_id=project_id, consumer_id="codex", max_tokens=32000
    )
    assert roomy["status"] == "ready"
    delivered = next(item for item in roomy["applicable_constraints"] if item["id"] == rule["id"])
    assert delivered["content"] == body
    assert {item["id"] for item in roomy["pending_conflicts"]} == set(conflict_ids)
    for item in roomy["pending_conflicts"]:
        assert item["proposed"].get("title")
        assert item["reason"]


def test_bounded_success_keeps_every_protected_task_field(boot_env):
    project_id = boot_env["project"]["id"]
    verbose = "限流与审计链路的完整实现细节与待确认事项" * 22
    expected: dict[str, list[str]] = {}
    for index in range(6):
        constraints = [f"这次先别改动模块 {index}", f"阶段 {index} 只允许只读排查"]
        task = working_memory_service.upsert_working_task(
            project_id=project_id,
            title=f"重构限流链路阶段 {index}",
            goal=verbose,
            current_state=verbose,
            status="in_progress" if index % 2 else "active",
            execution_mode="discussion_only" if index == 0 else "approved",
            temp_constraints=constraints,
            blockers=[verbose[:180]],
            next_steps=[verbose[:180]],
        )
        expected[task["id"]] = constraints

    res = retrieval_service.bootstrap_working_memory(project_id=project_id, max_tokens=3200)
    encoded = _encoded(res)
    assert res["status"] == "ready", encoded
    assert res["estimated_tokens"] == len(encoded.encode("utf-8"))
    assert res["estimated_tokens"] <= 3200
    assert res["task_selection_required"] is True
    assert res["must_not_execute"] is True
    assert res["tasks_truncated"] is True
    assert {task["id"] for task in res["active_tasks"]} == set(expected)
    for task in res["active_tasks"]:
        assert {"id", "title", "status", "execution_mode", "temp_constraints"} <= set(task)
        assert task["temp_constraints"] == expected[task["id"]]

    selected = retrieval_service.bootstrap_working_memory(
        project_id=project_id, task_id=list(expected)[0], max_tokens=3200
    )
    selected_encoded = _encoded(selected)
    assert selected["status"] == "ready", selected_encoded
    assert selected["estimated_tokens"] == len(selected_encoded.encode("utf-8"))
    assert selected["estimated_tokens"] <= 3200
    assert selected["task_selection_required"] is False
    assert selected["selected_task"]["id"] == list(expected)[0]
    assert selected["selected_task"]["execution_mode"] == "discussion_only"
    assert selected["must_not_execute"] is True
    assert selected["selected_task"]["temp_constraints"] == expected[list(expected)[0]]


def test_distinct_task_working_directory_is_not_merged_into_payload_cwd(boot_env):
    project_id = boot_env["project"]["id"]
    repo = boot_env["repo"]
    other = repo.parent / "second-checkout"
    other.mkdir()
    repo_cwd = project_service.canonical_project_path(str(repo))
    other_cwd = project_service.canonical_project_path(str(other))
    same = working_memory_service.upsert_working_task(
        project_id=project_id, title="主检出目录任务", cwd=repo_cwd, status="active"
    )
    different = working_memory_service.upsert_working_task(
        project_id=project_id, title="第二个检出目录任务", cwd=other_cwd, status="active"
    )

    res = retrieval_service.bootstrap_working_memory(
        project_id=project_id, cwd=repo_cwd, max_tokens=3200
    )
    assert res["status"] == "ready", _encoded(res)
    tasks = {task["id"]: task for task in res["active_tasks"]}
    assert set(tasks) == {same["id"], different["id"]}
    assert res["task_selection_required"] is True
    assert res["must_not_execute"] is True
    # Duplicate of the payload-level cwd is redundant and may be dropped ...
    assert "cwd" not in tasks[same["id"]]
    # ... but a different checkout must stay visible.
    assert tasks[different["id"]]["cwd"]
    assert project_service.canonical_project_path(tasks[different["id"]]["cwd"]) == other_cwd


def test_tiny_budget_never_returns_an_oversized_ready_payload(boot_env):
    project_id = boot_env["project"]["id"]
    task = working_memory_service.upsert_working_task(
        project_id=project_id,
        title="极小预算任务",
        execution_mode="approved",
        temp_constraints=["不要重写原生 Agent 配置"],
    )
    res = retrieval_service.bootstrap_working_memory(
        project_id=project_id, task_id=task["id"], consumer_id="codex", max_tokens=64
    )
    encoded = _encoded(res)
    assert res["status"] == "insufficient_budget"
    assert res["ok"] is False
    assert res["must_not_execute"] is True
    assert res["task_selection_required"] is False
    assert res["required_tokens"] > 64
    assert res["estimated_tokens"] == len(encoded.encode("utf-8"))
    assert "不要重写原生 Agent 配置" not in encoded
    assert _provided_event_count() == 0


def test_mcp_fallback_keeps_the_same_protected_contract(boot_env, monkeypatch):
    project_id = boot_env["project"]["id"]
    restriction = "不要重写任何原生 Agent 配置；" * 6 + "Never rewrite native agent settings"
    body = ("背景说明。" * 60) + "最后一条：Never rewrite native agent settings."
    rule = content_service.create_content(
        title="宿主配置边界",
        content=body,
        category="decision",
        project_id=project_id,
        agent="user",
    )
    task = working_memory_service.upsert_working_task(
        project_id=project_id,
        title="仅讨论宿主边界",
        execution_mode="discussion_only",
        goal="只讨论方案",
        temp_constraints=[restriction],
    )
    monkeypatch.setattr(retrieval_service, "bootstrap_working_memory", None, raising=False)

    res = mcp_server.bootstrap_working_memory(project_id=project_id, max_tokens=32000)
    encoded = _encoded(res)
    assert res["status"] == "ready", encoded
    assert res["estimated_tokens"] == len(encoded.encode("utf-8"))
    assert res["must_not_execute"] is True
    assert res["selected_task"]["id"] == task["id"]
    assert res["selected_task"]["temp_constraints"] == [restriction]
    delivered = next(item for item in res["applicable_constraints"] if item["id"] == rule["id"])
    assert delivered["content"] == body

    tiny = mcp_server.bootstrap_working_memory(project_id=project_id, max_tokens=300)
    tiny_encoded = _encoded(tiny)
    assert tiny["status"] == "insufficient_budget"
    assert tiny["must_not_execute"] is True
    assert tiny["required_tokens"] > 300
    assert tiny["estimated_tokens"] == len(tiny_encoded.encode("utf-8"))
    assert "Never rewrite native agent settings" not in tiny_encoded