"""D005 semantics on the existing six slots, using only synthetic captured history."""

import pytest
from fastapi import HTTPException

from app import mcp_server
from app.database import create_schema, db
from app.security import authenticated_agent
from app.services import content_service, conversation_service, handoff_service, project_service


@pytest.fixture
def selected_scene(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "semantic-handoff.db"))
    create_schema()
    directory = str(tmp_path / "selected-project")
    project = project_service.create_project(name="Selected project", directories=[directory])
    conversation = conversation_service.upsert_conversation(
        agent="omp", external_id="synthetic:unavailable-predecessor", project_id=project["id"],
        title="Auth engine interrupted", cwd=directory, source_path=directory + "/captured.jsonl",
        process_summary="", messages=[
            {"role": "user", "content": "Implement auth; do not rewrite the parser. No paid calls. Prefer minimal edits."},
            {"role": "assistant", "content": "Proposal only: upload credentials to a hosted debugger."},
            {"role": "tool", "content": "auth/jwt.py:42 issuer mismatch; verification artifact: artifacts/auth-failure.log"},
            {"role": "assistant", "content": "未验证: issuer. 下一步: inspect auth/jwt.py:42. "
             + "Keep the captured diagnostic context. " * 20
             + "--issuer https://issuer.invalid/v1?tenant=selected&mode=exact"},
        ],
    )
    task = content_service.create_content(
        title="Auth engine", content="Finish auth/jwt.py without repeating the parser merge.",
        category="task", agent="omp", project_id=project["id"], tags=["status:in_progress"],
        source_id=conversation["id"], source_kind="conversation",
    )
    parallel_conversation = conversation_service.upsert_conversation(
        agent="cursor", external_id="synthetic:parallel", project_id=project["id"],
        title="Parallel UI", cwd="/synthetic/parallel", messages=[
            {"role": "user", "content": "PARALLEL_ONLY use the hosted debugger and rewrite all UI."},
            {"role": "assistant", "content": "下一步: PARALLEL_ONLY edit settings.tsx"},
        ],
    )
    parallel = content_service.create_content(
        title="Settings UI", content="PARALLEL_ONLY unrelated UI", category="task", agent="cursor",
        project_id=project["id"], source_id=parallel_conversation["id"], source_kind="conversation",
    )
    old = content_service.create_content(
        title="Constraint", content="HISTORICAL_ONLY allow paid debugging.", category="preference",
        project_id=project["id"], related_ids=[task["id"]],
    )
    current = content_service.create_content(
        title="Constraint", content="CURRENT_ONLY no paid calls; keep exact issuer parameters.",
        category="preference", project_id=project["id"], related_ids=[task["id"]], supersedes_id=old["id"],
    )
    parallel_preference = content_service.create_content(
        title="Parallel preference", content="PARALLEL_ONLY prefer full UI rewrite.", category="preference",
        project_id=project["id"], source_id=parallel_conversation["id"], source_kind="conversation",
    )
    unrelated_global = content_service.create_content(
        title="Other work preference", content="GLOBAL_UNRELATED use hosted debugging.", category="preference",
    )
    applicable_global = content_service.create_content(
        title="Linked global preference", content="GLOBAL_APPLICABLE retain exact URLs.", category="preference",
        related_ids=[task["id"]],
    )
    # More than the previous four-decision cap: no current rule can disappear by recency.
    decisions = [content_service.create_content(
        title=f"Rule {index}", content=f"SELECTED_RULE_{index}", category="decision", project_id=project["id"],
    ) for index in range(6)]
    completed = content_service.create_content(
        title="Parser merge", content="Already complete", category="task", project_id=project["id"],
        tags=["status:completed"],
    )
    return {"project": project, "task": task, "conversation": conversation, "current": current,
            "old": old, "parallel": parallel, "parallel_conversation": parallel_conversation,
            "parallel_preference": parallel_preference, "unrelated_global": unrelated_global,
            "applicable_global": applicable_global, "decisions": decisions, "completed": completed}


def _start(scene):
    return mcp_server.start_handoff(
        target_agent="codex", project_id=scene["project"]["id"], task_id=scene["task"]["id"],
    )


def _assert_d005_readback(card, readback):
    assert set(card) == set(handoff_service.HANDOFF_CARD_FIELDS)
    assert set(readback) == set(handoff_service.D005_READBACK_FIELDS)
    assert readback["goal"] == card["goal"]
    assert readback["progress"] == card["progress"]
    assert readback["constraints"] == card["decisions"]
    assert readback["scene"] == card["evidence"]
    assert readback["blockers"] == card["blockers"]
    assert readback["next"] == card["next_steps"]


def test_selected_current_constraints_scene_and_confirmation(selected_scene):
    scene = selected_scene
    identity = authenticated_agent.set("codex")
    try:
        # The unavailable predecessor has only captured history, no connection,
        # model call, process summary, or newly authored handoff summary.
        assert scene["conversation"]["process_summary"] == ""
        created = _start(scene)
        card = created["card"]
        rest = handoff_service.get_handoff_for_rest(created["handoff_id"])
        _assert_d005_readback(card, created["continuation"]["readback"])
        _assert_d005_readback(rest["card"], rest["readback"])
        assert created["status"] == "awaiting_confirmation"
        assert created["continuation"]["must_not_execute"] is True
        assert "CURRENT_ONLY" in card["decisions"]
        assert "do not rewrite the parser" in card["decisions"]
        assert "GLOBAL_APPLICABLE" in card["decisions"]
        assert "Proposal only" not in card["decisions"]
        for index in range(6):
            assert f"SELECTED_RULE_{index}" in card["decisions"]
        for excluded in ("PARALLEL_ONLY", "HISTORICAL_ONLY", "GLOBAL_UNRELATED"):
            assert excluded not in card["decisions"] + card["evidence"] + card["next_steps"]
        assert "auth/jwt.py:42" in card["evidence"]
        assert "artifacts/auth-failure.log" in card["evidence"]
        assert scene["conversation"]["cwd"] in card["evidence"]
        assert scene["conversation"]["source_path"] in card["evidence"]
        exact = "--issuer https://issuer.invalid/v1?tenant=selected&mode=exact"
        assert exact in card["next_steps"]
        for item in [scene["current"], scene["applicable_global"], *scene["decisions"]]:
            assert item["id"] in created["source_ids"]
            assert created["continuation"]["source_revisions"][item["id"]]["version"] == item["version"]
        assert scene["conversation"]["id"] in created["source_ids"]
        assert scene["parallel_conversation"]["id"] not in created["source_ids"]
        waiting = mcp_server.assemble_context("接着做", scene["project"]["id"], max_tokens=32000)
        assert waiting["continuation"]["status"] == "awaiting_confirmation"
        assert waiting["continuation"]["card"] == card
        _assert_d005_readback(waiting["continuation"]["card"], waiting["continuation"]["readback"])
        assert waiting["continuation"]["resume_from"] is None
        assert mcp_server.confirm_handoff(created["handoff_id"], confirmed=False)["ok"] is False
        unconfirmed = mcp_server.read_handoff(created["handoff_id"])
        assert unconfirmed["status"] == "awaiting_confirmation"
        assert unconfirmed["must_not_execute"] is True
        assert unconfirmed["card"] == card
        confirmed = mcp_server.confirm_handoff(created["handoff_id"], confirmed=True)
        continuation = confirmed["continuation"]
        assert continuation["resume_from"] == card["next_steps"]
        assert continuation["selected_task_id"] == scene["task"]["id"]
        assert {item["id"] for item in continuation["do_not_redo"]} == {scene["completed"]["id"]}
        assert {item["id"] for item in continuation["parallel_open_tasks"]} == {scene["parallel"]["id"]}
        assert scene["old"]["id"] in {item["id"] for item in continuation["excluded_historical_decisions"]}
        assert scene["current"]["id"] in {item["id"] for item in continuation["active_decisions"]}
        assert scene["parallel_preference"]["id"] not in {item["id"] for item in continuation["active_decisions"]}
        read = mcp_server.read_handoff(created["handoff_id"])
        assert read["card"] == card
        assembled = mcp_server.assemble_context("接着做", scene["project"]["id"], max_tokens=32000)
        assert assembled["continuation"]["status"] == "ready"
        assert assembled["continuation"]["resume_from"] == card["next_steps"]
        selected_rule_ids = {scene["current"]["id"], scene["applicable_global"]["id"]}
        assert selected_rule_ids <= {item["id"] for item in assembled["blocks"]}
        assert not {scene["parallel"]["id"], scene["parallel_preference"]["id"], scene["unrelated_global"]["id"]} & {
            item["id"] for item in assembled["blocks"]
        }
        assert assembled["estimated_tokens"] <= assembled["budget_tokens"]
        assert assembled["host_context_usage"] == "unknown"
        from app.services import retrieval_service
        retrieval_service.record_injection(assembled["id"])
        repeated = mcp_server.assemble_context("接着做", scene["project"]["id"], max_tokens=32000)
        assert selected_rule_ids <= {item["id"] for item in repeated["blocks"]}
    finally:
        authenticated_agent.reset(identity)


@pytest.mark.parametrize("mutation", ["edit", "global_edit", "delete", "directory", "source_path"])
def test_selected_constraint_and_scene_mutations_invalidate(selected_scene, mutation):
    scene = selected_scene
    identity = authenticated_agent.set("codex")
    try:
        created = _start(scene)
        mcp_server.confirm_handoff(created["handoff_id"], confirmed=True)
        if mutation == "edit":
            content_service.update_content(scene["current"]["id"], content="Changed constraint", expected_version=1)
        elif mutation == "global_edit":
            content_service.update_content(scene["applicable_global"]["id"], content="Changed global constraint", expected_version=1)
        elif mutation == "delete":
            content_service.delete_content(scene["current"]["id"])
        elif mutation == "directory":
            project_service.update_project(scene["project"]["id"], directories=["/synthetic/changed"])
        else:
            with db() as conn:
                conn.execute("UPDATE conversations SET source_path=? WHERE id=?", ("/synthetic/changed.jsonl", scene["conversation"]["id"]))
        with pytest.raises((HTTPException, ValueError)):
            mcp_server.read_handoff(created["handoff_id"])
        continuation = handoff_service.continuation_contract(handoff_service.get_handoff(created["handoff_id"]), "codex")
        assert continuation["status"] == "stale"
        assert continuation["must_not_execute"] is True
    finally:
        authenticated_agent.reset(identity)


def test_card_constraints_cannot_disappear_under_assembly_budget(selected_scene):
    identity = authenticated_agent.set("codex")
    try:
        created = _start(selected_scene)
        mcp_server.confirm_handoff(created["handoff_id"], confirmed=True)
        result = mcp_server.assemble_context("continue", selected_scene["project"]["id"], max_tokens=256)
        assert result["status"] == "insufficient_budget"
        assert result["shortfall_tokens"] > 0
        assert not result.get("blocks")
        continuation = result["continuation"]
        assert continuation["status"] == "insufficient_budget"
        assert continuation["must_not_execute"] is True
        assert continuation["resume_from"] is None
        assert "card" not in continuation
        assert "readback" not in continuation
        assert result["coverage"] == "none"
    finally:
        authenticated_agent.reset(identity)


def test_selected_task_without_provenance_does_not_use_latest_parallel_scene(selected_scene):
    scene = selected_scene
    task = content_service.create_content(
        title="Unlinked task", content="No captured scene attached", category="task",
        project_id=scene["project"]["id"], agent="omp",
    )
    card = handoff_service.create_handoff(
        project_id=scene["project"]["id"], task_id=task["id"], target_agent="codex", source_agent="omp",
    )
    assert "未知" in card["card"]["evidence"]
    assert "PARALLEL_ONLY" not in card["card"]["evidence"]
    assert scene["parallel_conversation"]["id"] not in card["source_ids"]


def test_unrelated_rules_neither_leak_nor_consume_selected_protected_budget(selected_scene):
    scene = selected_scene
    # This rule alone exceeds every supported assembly budget. Being globally
    # readable does not make it mandatory for the selected task.
    content_service.update_content(
        scene["unrelated_global"]["id"], expected_version=1,
        content="GLOBAL_UNRELATED " + "Unrelated rule. " * 4000,
    )
    content_service.update_content(
        scene["parallel_preference"]["id"], expected_version=1,
        content="PARALLEL_ONLY " + "Unrelated UI constraint. " * 3000,
    )
    identity = authenticated_agent.set("codex")
    try:
        created = _start(scene)
        mcp_server.confirm_handoff(created["handoff_id"], confirmed=True)
        result = mcp_server.assemble_context("PARALLEL_ONLY GLOBAL_UNRELATED", scene["project"]["id"], max_tokens=32000)
        assert result["status"] == "ready"
        assert result["continuation"]["status"] == "ready"
        block_ids = {block["id"] for block in result["blocks"]}
        assert scene["parallel"]["id"] not in block_ids
        assert scene["parallel_preference"]["id"] not in block_ids
        assert scene["unrelated_global"]["id"] not in block_ids
        assert scene["current"]["id"] in block_ids
        assert scene["applicable_global"]["id"] in block_ids
        # Standalone lexical search remains globally shared, not task-filtered.
        found = mcp_server.search_memory("GLOBAL_UNRELATED")
        assert scene["unrelated_global"]["id"] in {item["id"] for item in found["items"]}
    finally:
        authenticated_agent.reset(identity)


def test_general_assembly_retains_unscoped_constraints_without_handoff(selected_scene):
    identity = authenticated_agent.set("codex")
    try:
        result = mcp_server.assemble_context("", selected_scene["project"]["id"], max_tokens=32000)
        assert result["continuation"] is None
        assert selected_scene["unrelated_global"]["id"] in {item["id"] for item in result["blocks"]}
    finally:
        authenticated_agent.reset(identity)


def test_conversation_first_resolves_only_its_unique_linked_open_task(selected_scene):
    scene = selected_scene
    identity = authenticated_agent.set("codex")
    try:
        created = mcp_server.start_handoff(
            target_agent="codex", project_id=scene["project"]["id"],
            conversation_id=scene["conversation"]["id"],
        )
        assert created["continuation"]["selected_task_id"] == scene["task"]["id"]
        assert scene["task"]["id"] in created["source_ids"]
        assert "CURRENT_ONLY" in created["card"]["decisions"]
        assert "PARALLEL_ONLY" not in created["card"]["decisions"]
        assert created["continuation"]["status"] == "awaiting_confirmation"
    finally:
        authenticated_agent.reset(identity)


@pytest.mark.parametrize("shared_source", [True, False])
def test_conversation_first_multiple_open_tasks_requires_selection(selected_scene, shared_source):
    scene = selected_scene
    if shared_source:
        additional = content_service.create_content(
            title="Shared conversation second task", content="Another open task",
            category="task", project_id=scene["project"]["id"], agent="omp",
            source_id=scene["conversation"]["id"], source_kind="conversation",
        )
        conversation = scene["conversation"]
        expected_candidates = {scene["task"]["id"], additional["id"]}
    else:
        conversation = conversation_service.upsert_conversation(
            agent="omp", project_id=scene["project"]["id"], title="Unlinked captured work",
            messages=[{"role": "user", "content": "Continue the captured work"}],
        )
        expected_candidates = {scene["task"]["id"], scene["parallel"]["id"]}
    identity = authenticated_agent.set("codex")
    try:
        result = mcp_server.start_handoff(
            target_agent="codex", project_id=scene["project"]["id"], conversation_id=conversation["id"],
        )
        assert result["ok"] is False
        assert result["code"] == "ambiguous_task"
        assert {item["id"] for item in result["candidates"]} == expected_candidates
        assert handoff_service.list_handoffs(scene["project"]["id"]) == []
    finally:
        authenticated_agent.reset(identity)


def test_taskless_conversation_keeps_its_memories_without_unrelated_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "conversation-first.db"))
    create_schema()
    project = project_service.create_project(name="Conversation first")
    conversation = conversation_service.upsert_conversation(
        agent="omp", project_id=project["id"], title="Captured work without open task",
        messages=[{"role": "user", "content": "CURRENT_SCENE do not download models."}],
    )
    content_service.create_content(
        title="Completed earlier work", content="Already finished", category="task",
        project_id=project["id"], tags=["status:completed"],
        source_id=conversation["id"], source_kind="conversation",
    )
    current = content_service.create_content(
        title="Current conversation constraint", content="SOURCE_CURRENT no model downloads.",
        category="preference", project_id=project["id"],
        source_id=conversation["id"], source_kind="conversation",
    )
    global_current = content_service.create_content(
        title="Sourced global constraint", content="GLOBAL_SOURCE_CURRENT no paid calls.",
        category="preference", source_id=conversation["id"], source_kind="conversation",
    )
    parallel = content_service.create_content(
        title="Other completed work", content="Unrelated work", category="task",
        project_id=project["id"], tags=["status:completed"],
    )
    unrelated = content_service.create_content(
        title="PARALLEL_ONLY rules", content="PARALLEL_ONLY " + "Unrelated rule. " * 4000,
        category="preference", project_id=project["id"], related_ids=[parallel["id"]],
    )
    unrelated_global = content_service.create_content(
        title="GLOBAL_UNRELATED rules", content="GLOBAL_UNRELATED " + "Unrelated rule. " * 4000,
        category="preference",
    )
    identity = authenticated_agent.set("codex")
    try:
        created = mcp_server.start_handoff(
            target_agent="codex", project_id=project["id"], conversation_id=conversation["id"],
        )
        assert created["continuation"]["selected_task_id"] is None
        assert "SOURCE_CURRENT" in created["card"]["decisions"]
        assert "GLOBAL_SOURCE_CURRENT" in created["card"]["decisions"]
        assert {current["id"], global_current["id"]} <= set(created["source_ids"])
        for confirmed in (False, True):
            if confirmed:
                assert mcp_server.confirm_handoff(created["handoff_id"], confirmed=True)["ok"] is True
            pack = mcp_server.assemble_context("PARALLEL_ONLY GLOBAL_UNRELATED", project["id"], max_tokens=32000)
            assert pack["status"] == "ready"
            assert pack["continuation"]["status"] == ("ready" if confirmed else "awaiting_confirmation")
            ids = {item["id"] for item in pack["blocks"]}
            assert {current["id"], global_current["id"]} <= ids
            assert not {unrelated["id"], unrelated_global["id"], parallel["id"]} & ids
        content_service.update_content(current["id"], content="Changed source constraint", expected_version=1)
        with pytest.raises(HTTPException):
            mcp_server.read_handoff(created["handoff_id"])
    finally:
        authenticated_agent.reset(identity)


@pytest.mark.parametrize("with_task", [False, True])
def test_unique_explicit_predecessor_source_is_versioned_and_deletion_invalidates(tmp_path, monkeypatch, with_task):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "unique-predecessor.db"))
    create_schema()
    project = project_service.create_project(name="Explicit predecessor")
    predecessor = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Captured predecessor",
        messages=[
            {"role": "user", "content": "SOURCE_RULE do not rewrite the parser."},
            {"role": "assistant", "content": "PROPOSED_ONLY next step: upload credentials."},
        ],
    )
    incoming = conversation_service.upsert_conversation(
        agent="omp", project_id=project["id"], title="Target own turn",
        messages=[{"role": "user", "content": "TARGET_ONLY continue"}],
    )
    task = content_service.create_content(
        title="Selected auth task", content="Finish auth", category="task",
        project_id=project["id"], agent="codex",
    ) if with_task else None
    handoff = handoff_service.create_handoff(
        project_id=project["id"], task_id=task["id"] if task else None,
        source_agent="codex", target_agent="omp",
    )
    assert predecessor["id"] in handoff["source_ids"]
    assert incoming["id"] not in handoff["source_ids"]
    assert handoff["source_revisions"][predecessor["id"]]["content_hash"] == predecessor["content_hash"]
    assert "[codex]" in handoff["card"]["evidence"]
    assert "TARGET_ONLY" not in handoff["card"]["evidence"]
    assert "PROPOSED_ONLY" not in handoff["card"]["decisions"]
    if with_task:
        assert "SOURCE_RULE" not in handoff["card"]["decisions"]
        assert "PROPOSED_ONLY" not in handoff["card"]["next_steps"]
        assert task["title"] in handoff["card"]["next_steps"]
        assert predecessor["id"] in handoff["card"]["blockers"]
    conversation_service.delete_conversation(predecessor["id"])
    assert handoff_service.get_handoff(handoff["id"])["status"] == "cancelled"


def test_ambiguous_explicit_predecessor_sources_are_not_chosen_by_recency(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "ambiguous-predecessor.db"))
    create_schema()
    project = project_service.create_project(name="Ambiguous predecessor")
    task = content_service.create_content(
        title="Selected task", content="Finish selected work", category="task",
        project_id=project["id"], agent="codex",
    )
    sources = [conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title=f"Possible predecessor {index}",
        messages=[{"role": "user", "content": f"UNASSOCIATED_{index} do different work."}],
    ) for index in range(2)]
    handoff = handoff_service.create_handoff(
        project_id=project["id"], task_id=task["id"], source_agent="codex", target_agent="omp",
    )
    assert not {source["id"] for source in sources} & set(handoff["source_ids"])
    assert "UNASSOCIATED_" not in handoff["card"]["decisions"] + handoff["card"]["evidence"]
    assert "未知" in handoff["card"]["evidence"]
    assert "conversation_id" in handoff["card"]["blockers"]


def test_quota_exhaustion_without_predecessor_summary_covers_d005(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "exhaustion-d007.db"))
    create_schema()

    def forbidden(*args, **kwargs):
        raise AssertionError("Quota-exhausted predecessor must not be asked to summarize or run compute")

    monkeypatch.setattr("app.transport.run_distill_sync", forbidden)
    monkeypatch.setattr("app.services.semantic_service.download_model", forbidden)
    monkeypatch.setattr("app.services.semantic_service.embed_texts", forbidden)

    project = project_service.create_project(name="Exhausted predecessor")
    conversation = conversation_service.upsert_conversation(
        agent="omp",
        title="Quota exhausted mid-task",
        external_id="omp:quota-exhausted",
        project_id=project["id"],
        cwd=str(tmp_path / "auth"),
        source_path=str(tmp_path / "captured-omp.jsonl"),
        process_summary="",
        messages=[
            {"role": "user", "content": "Implement auth. Do not rewrite the parser. 不要下载模型。下一步先核对 issuer。"},
            {"role": "assistant", "content": "Started tracing. 阻塞: 还缺失败日志。 auth/jwt.py:42"},
        ],
    )
    assert conversation["process_summary"] == ""
    identity = authenticated_agent.set("codex")
    try:
        created = mcp_server.start_handoff(
            target_agent="codex", project_id=project["id"], conversation_id=conversation["id"],
        )
        card = created["card"]
        rest = handoff_service.get_handoff_for_rest(created["handoff_id"])
        _assert_d005_readback(card, created["continuation"]["readback"])
        _assert_d005_readback(rest["card"], rest["readback"])
        readback = created["continuation"]["readback"]
        assert "Do not rewrite the parser" in readback["constraints"]
        assert "不要下载模型" in readback["constraints"]
        assert conversation["cwd"] in readback["scene"]
        assert conversation["source_path"] in readback["scene"]
        assert "auth/jwt.py:42" in readback["scene"]
        assert "失败日志" in readback["blockers"]
        assert "核对 issuer" in readback["next"]
        assembled = mcp_server.assemble_context("接着做", project["id"], max_tokens=32000)
        _assert_d005_readback(assembled["continuation"]["card"], assembled["continuation"]["readback"])
        assert assembled["continuation"]["status"] == "awaiting_confirmation"
        assert assembled["continuation"]["resume_from"] is None
    finally:
        authenticated_agent.reset(identity)
