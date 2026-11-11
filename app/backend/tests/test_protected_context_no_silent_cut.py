"""Protected goals, constraints, todos and tool pairs are not silently shortened."""

from app.database import create_schema
from app.services import content_service, conversation_service, handoff_service, retrieval_service


def test_tool_pairs_keep_both_sides_and_do_not_invent_a_result():
    long_argument = '{"path":"' + ("目录" * 80) + '"}'
    long_result = "RESULT_ONE_" + ("字节" * 80)
    extracted = handoff_service.assemble_tool_pairs([
        {"role": "assistant", "content": "calling", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": long_argument}},
            {"id": "call-2", "type": "function", "function": {"name": "write", "arguments": '{"path":"b"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call-1", "content": long_result},
        {"role": "tool", "tool_call_id": "call-2", "content": "RESULT_TWO_FULL"},
        {"role": "tool", "content": "ORPHAN_RESULT_SHOULD_NOT_LOOK_FINISHED"},
    ])
    by_id = {item["id"]: item for item in extracted["pairs"]}
    assert by_id["call-1"]["arguments"] == long_argument
    assert by_id["call-1"]["result"] == long_result
    assert by_id["call-2"]["result"] == "RESULT_TWO_FULL"
    assert "ORPHAN_RESULT_SHOULD_NOT_LOOK_FINISHED" not in repr(extracted["pairs"])
    assert {item["reason"] for item in extracted["omitted"]} == {"unpaired_tool_result"}


def test_stored_conversation_and_handoff_keep_full_pairs(tmp_path):
    create_schema()
    directory = str(tmp_path / "scene")
    project = project_service_create(directory)
    long_argument = '{"path":"' + ("目录" * 80) + '"}'
    long_result = "RESULT_ONE_" + ("字节" * 80)
    long_summary = "过程全文_" + ("未截断" * 80)
    conversation = conversation_service.upsert_conversation(
        agent="codex",
        title="Tool pair scene",
        external_id="synthetic:tool-pairs",
        project_id=project["id"],
        cwd=directory,
        messages=[
            {"role": "user", "content": "保留完整工具结果"},
            {"role": "assistant", "content": "calling", "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": long_argument}},
                {"id": "call-2", "type": "function", "function": {"name": "write", "arguments": '{"path":"b"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": long_result},
            {"role": "tool", "tool_call_id": "call-2", "content": "RESULT_TWO_FULL"},
            {"role": "tool", "content": "ORPHAN_RESULT_SHOULD_NOT_LOOK_FINISHED"},
        ],
        process_summary=[{"command": "read", "outcome": long_summary}],
    )
    assert long_summary in conversation["process_summary"]
    assert "…" not in conversation["process_summary"]
    stored_calls = [message for message in conversation["messages"] if message.get("tool_calls")]
    assert stored_calls[0]["tool_calls"][0]["function"]["arguments"] == long_argument
    stored_results = {message.get("tool_call_id"): message["content"] for message in conversation["messages"] if message.get("role") == "tool"}
    assert stored_results["call-1"] == long_result
    assert stored_results["call-2"] == "RESULT_TWO_FULL"

    card = handoff_service.create_handoff(
        project_id=project["id"], target_agent="omp", conversation_id=conversation["id"],
        source_agent="codex", actor="codex",
    )["card"]
    scene = card["evidence"] + card["progress"]
    assert long_argument in scene
    assert long_result in scene
    assert "RESULT_TWO_FULL" in scene
    assert long_summary in scene
    assert "ORPHAN_RESULT_SHOULD_NOT_LOOK_FINISHED" not in card["progress"]
    assert "未配对" in scene
    assert "done" not in card["progress"]


def test_compressed_relay_does_not_slice_constraints_or_goals():
    create_schema()
    project = project_service_create("/synthetic/relay-constraints")
    constraint = "CONSTRAINT_MARK " + ("必须保留的授权约束。" * 22)
    goal = "GOAL_MARK " + ("这是不能被摘要截断的用户目标。" * 14)
    assert len(constraint) > 200 and len(goal) > 200
    content_service.create_content(
        title="Keep the constraint", content=constraint, category="decision", project_id=project["id"],
    )
    task = content_service.create_content(
        title="Selected work", content="继续这项工作", category="task", project_id=project["id"],
        agent="codex", tags=["status:in_progress"],
    )
    conversation_service.upsert_conversation(
        agent="codex", title="Constraint scene", external_id="synthetic:constraint-scene",
        project_id=project["id"], cwd="/synthetic/relay-constraints",
        messages=[{"role": "user", "content": goal}, {"role": "assistant", "content": "收到"}],
    )
    relayed = handoff_service.relay_project(
        project_id=project["id"], consumer="omp", task_id=task["id"],
        context_mode="compressed", max_tokens=32000, sync_first=False,
    )
    text = relayed["context_text"]
    assert constraint in text
    assert goal in text
    assert relayed["context"]["active_decisions_and_constraints"][0]["content"] == constraint
    huge = "HUGE_CONSTRAINT " + ("不能靠截断交出去。" * 4000)
    content_service.create_content(
        title="Too large", content=huge, category="decision", project_id=project["id"],
    )
    blocked = handoff_service.relay_project(
        project_id=project["id"], consumer="omp", task_id=task["id"],
        context_mode="compressed", max_tokens=32000, sync_first=False,
    )
    assert blocked["status"] == "insufficient_budget"
    assert blocked["must_not_execute"] is True
    assert "HUGE_CONSTRAINT" not in repr(blocked)


def test_open_task_is_not_replaced_by_a_passage_snippet():
    create_schema()
    project = project_service_create("/synthetic/todo-budget")
    marker = "TASK_BODY_MARKER_" + ("待办正文" * 400)
    content_service.create_content(
        title="Long todo", content=marker, category="task", project_id=project["id"],
        agent="codex", tags=["status:in_progress"],
    )
    pack = retrieval_service.assemble_context("TASK_BODY_MARKER_", project_id=project["id"], budget_tokens=2200)
    assert pack["status"] == "ready"
    task_entries = [entry for entry in pack["entries"] if entry["category"] == "task"]
    assert task_entries
    assert task_entries[0]["tier"] == "reference"
    assert "待办正文未放入本段" in pack["text"]
    assert "TASK_BODY_MARKER_" not in pack["text"]


def test_process_summary_list_does_not_invent_or_slice_an_outcome():
    create_schema()
    project = project_service_create("/synthetic/summary")
    outcome = "OUTCOME_MARK_" + ("结果" * 80)
    stored = conversation_service.upsert_conversation(
        agent="codex", title="Summary", external_id="synthetic:summary",
        project_id=project["id"], messages=[{"role": "user", "content": "记录"}],
        process_summary=[{"command": "bash"}, {"command": "read", "outcome": outcome}],
    )
    assert outcome in stored["process_summary"]
    assert "结果未记录" in stored["process_summary"]
    assert "done" not in stored["process_summary"]


def project_service_create(directory: str):
    from app.services import project_service
    return project_service.create_project(name="Synthetic protected context", directories=[directory])
