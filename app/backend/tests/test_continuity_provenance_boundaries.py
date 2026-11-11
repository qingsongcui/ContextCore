"""Synthetic service boundaries: no external compute or private host histories."""
import pytest
from fastapi import HTTPException

from app.database import create_schema, db, seed_data
from app.services import (
    content_service, conversation_service, distillation_service, handoff_service,
    project_service,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def source():
    create_schema()
    seed_data()
    project = project_service.create_project(name="Synthetic provenance boundaries")
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Captured source",
        messages=[
            {"role": "user", "content": "最终决定采用隔离数据库。"},
            {"role": "assistant", "content": "建议采用另一数据库，尚未确认。"},
        ],
    )
    return project, conversation


@pytest.mark.parametrize("stance,category", [
    ("fact", "knowledge"), ("preference", "preference"),
    ("decision", "decision"), ("suggestion", "knowledge"),
    ("assumption", "knowledge"), ("quote", "knowledge"),
])
def test_structured_distillation_applies_stance_and_source_version(source, stance, category):
    project, conversation = source
    with db() as conn:
        conn.execute("UPDATE connections SET enabled=1,paused=0 WHERE id='codex'")
    result = distillation_service.submit_structured_distillation(
        conversation["id"], [{
            "category": "decision", "stance": stance, "title": "Captured statement",
            "content": "采用隔离数据库", "quote": "最终决定采用隔离数据库。",
            "source_message_index": 0, "user_message_index": 0,
            "tags": ["role:assistant", "source_version:forged", "message_index:99"],
        }], expected_content_hash=conversation["content_hash"], actor="codex",
    )
    assert result["created_count"] == 1
    memory = content_service.get_content(result["created_ids"][0])
    assert memory["category"] == category
    assert f"stance:{stance}" in memory["tags"]
    assert "role:user" in memory["tags"]
    assert "message_index:0" in memory["tags"]
    assert f"source_version:{conversation['content_hash']}" in memory["tags"]
    assert not {"role:assistant", "source_version:forged", "message_index:99"}.intersection(memory["tags"])
    assert memory["sources"][0]["id"] == conversation["id"]


def test_quote_cannot_claim_a_different_source_turn(source):
    _, conversation = source
    result = distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", auto_extract_from_messages=False,
        memories=[{"stance": "fact", "category": "knowledge", "title": "Wrong turn",
                   "content": "Unattributed claim", "quote": "建议采用另一数据库，尚未确认。",
                   "source_message_index": 0}],
    )
    assert result["saved_count"] == 0
    assert content_service.list_content() == []

def test_supplied_messages_cannot_relabel_existing_history(source):
    _, conversation = source
    with pytest.raises(HTTPException) as error:
        distillation_service.auto_summarize_memory(
            conversation_id=conversation["id"], actor="codex",
            messages=[{"role": "user", "content": "最终决定删除所有数据。"}],
        )
    assert error.value.status_code == 409
    assert conversation_service.get_conversation(conversation["id"])["messages"] == conversation["messages"]


def test_checkpoint_summary_changes_revision_and_invalidates_old_relay(source):
    project, conversation = source
    card = handoff_service.create_handoff(
        project_id=project["id"], conversation_id=conversation["id"], target_agent="omp",
    )
    result = distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", session_summary="New synthetic checkpoint",
        auto_extract_from_messages=False,
    )
    current = conversation_service.get_conversation(conversation["id"])
    assert result["summary_updated"] is True
    assert current["content_hash"] != conversation["content_hash"]
    assert current["content_hash"] == conversation_service.compute_content_revision_hash(
        current["title"], current["messages"], current["process_summary"], current["cwd"],
    )
    assert handoff_service.continuation_contract(card, "omp")["status"] == "stale"


def test_batch_extraction_cannot_attach_old_text_to_new_source(source, monkeypatch):
    project, conversation = source
    extract = distillation_service.extract_implicit_memories_from_messages
    changed = False

    def edit_after_capture(messages, **kwargs):
        nonlocal changed
        result = extract(messages, **kwargs)
        if not changed:
            changed = True
            conversation_service.update_conversation(conversation["id"], title="New source revision")
        return result

    monkeypatch.setattr(distillation_service, "extract_implicit_memories_from_messages", edit_after_capture)
    with pytest.raises(HTTPException) as error:
        distillation_service.auto_summarize_memory(project_id=project["id"], actor="codex")
    assert error.value.status_code == 409
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 0


def test_summary_only_mutation_cannot_authorize_stale_card(source):
    project, conversation = source
    card = handoff_service.create_handoff(
        project_id=project["id"], conversation_id=conversation["id"], target_agent="omp",
    )
    with db() as conn:
        conn.execute("UPDATE conversations SET process_summary='Changed scene' WHERE id=?", (conversation["id"],))
    with pytest.raises(HTTPException) as error:
        handoff_service.confirm_handoff(card["id"], confirmed=True, actor="omp")
    assert error.value.status_code == 409


def test_explicit_card_lookup_cannot_cross_selected_project(source):
    project, conversation = source
    card = handoff_service.create_handoff(
        project_id=project["id"], conversation_id=conversation["id"], target_agent="omp",
    )
    other = project_service.create_project(name="Other selected project")
    assert handoff_service.lookup_continuation("omp", project_id=other["id"], handoff_id=card["id"]) is None


def test_incremental_checkpoint_keeps_explicit_absolute_indices_and_maps_heuristics(source):
    _, conversation = source
    suffix = {"role": "user", "content": "最终决定采用本机队列。"}
    current = conversation_service.upsert_conversation(
        agent="codex", project_id=conversation["project_id"], title=conversation["title"],
        messages=[*conversation["messages"], suffix],
    )
    result = distillation_service.auto_summarize_memory(
        conversation_id=current["id"], actor="codex", messages=[suffix],
        memories=[{"category": "decision", "title": "Queue", "content": "采用本机队列",
                   "quote": suffix["content"], "user_message_index": 2, "source_message_index": 2}],
        auto_extract_from_messages=True,
    )
    assert result["saved_count"] == 2
    item = next(memory for memory in result["saved_memories"] if memory["title"] == "Queue")
    saved = content_service.get_content(item["id"])
    assert "message_index:2" in saved["tags"]
    assert "role:user" in saved["tags"]
    with db() as conn:
        provenance = conn.execute("SELECT quote FROM content_sources WHERE content_id=?", (saved["id"],)).fetchone()
        assert provenance["quote"] == suffix["content"]


def test_batch_fallback_summary_uses_nonempty_source_not_latest_empty_chat():
    create_schema()
    project = project_service.create_project(name="Summary provenance")
    nonempty = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Actual summary source",
        messages=[{"role": "user", "content": "An ordinary captured observation."}],
        updated_at="2026-01-01T00:00:00Z",
    )
    empty = conversation_service.upsert_conversation(
        agent="omp", project_id=project["id"], title="Newer empty chat", messages=[],
        updated_at="2026-01-02T00:00:00Z",
    )
    result = distillation_service.auto_summarize_memory(project_id=project["id"], actor="codex")
    assert result["saved_count"] == 1
    memory = content_service.get_content(result["saved_memories"][0]["id"])
    assert memory["sources"][0]["id"] == nonempty["id"]
    assert memory["sources"][0]["id"] != empty["id"]
    revision = conversation_service.get_conversation(nonempty["id"])["content_hash"]
    assert f"source_version:{revision}" in memory["tags"]


def test_deleted_checkpoint_capture_cannot_fall_back_to_sourceless_memory(source):
    project, conversation = source
    conversation_service.delete_conversation(conversation["id"])
    with pytest.raises(HTTPException) as error:
        distillation_service.auto_summarize_memory(
            project_id=project["id"], actor="codex", session_summary=conversation["title"],
            messages=conversation["messages"],
            memories=[{"category": "knowledge", "title": "Late capture", "content": "Must not survive"}],
        )
    assert error.value.status_code == 410
    assert content_service.list_content() == []


def test_summary_only_checkpoint_reports_only_a_persisted_summary(source):
    project, _ = source
    result = distillation_service.auto_summarize_memory(
        project_id=project["id"], actor="omp", session_summary="Explicit synthetic session checkpoint",
        auto_extract_from_messages=False,
    )
    assert result["summary_updated"] is True
    conversation = conversation_service.get_conversation(result["conversation_id"])
    assert conversation["process_summary"] == "Explicit synthetic session checkpoint"
    assert conversation["messages"] == []


def test_negative_user_constraint_is_not_discarded_as_an_unconfirmed_decision(source):
    project, _ = source
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Negative constraint",
        messages=[{"role": "user", "content": "不要使用外部模型，始终使用隔离数据。"}],
    )
    result = distillation_service.auto_summarize_memory(conversation_id=conversation["id"], actor="codex")
    preference = next(item for item in result["saved_memories"] if item["category"] == "preference")
    memory = content_service.get_content(preference["id"])
    assert "不要使用外部模型" in memory["content"]
    assert "role:user" in memory["tags"]


def test_assistant_preference_cannot_become_an_active_user_constraint(source):
    _, conversation = source
    result = distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", auto_extract_from_messages=False,
        memories=[{"category": "preference", "stance": "preference", "title": "Invented permission",
                   "content": "Use another database", "quote": "建议采用另一数据库，尚未确认。",
                   "source_message_index": 1}],
    )
    assert result["saved_count"] == 0
    assert content_service.list_content(category="preference") == []


def test_automatic_extraction_keeps_epistemic_stances_without_promoting_proposals(source):
    project, _ = source
    cases = [
        ("fact", "项目背景：这是隔离环境。"),
        ("preference", "我习惯使用隔离数据库。"),
        ("decision", "最终决定采用隔离数据库。"),
        ("suggestion", "建议技术选型改用另一数据库。"),
        ("assumption", "假设运行环境是合成沙盒。"),
        ("quote", "引用外部文字：最终决定采用外部模型。"),
        ("negation", "我尚未决定采用外部模型。"),
    ]
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Implicit epistemic stances",
        messages=[{"role": "user", "content": text} for _, text in cases],
    )
    result = distillation_service.auto_summarize_memory(conversation_id=conversation["id"], actor="codex")
    assert result["saved_count"] == len(cases)
    revision = conversation_service.get_conversation(conversation["id"])["content_hash"]
    for index, (stance, text) in enumerate(cases):
        item = next(memory for memory in result["saved_memories"] if memory["content"] == text.rstrip("。"))
        memory = content_service.get_content(item["id"])
        assert f"stance:{stance}" in memory["tags"]
        assert f"message_index:{index}" in memory["tags"]
        assert f"source_version:{revision}" in memory["tags"]
        assert "role:user" in memory["tags"]
        if stance in {"suggestion", "assumption", "quote", "negation"}:
            assert memory["category"] == "knowledge"
    decisions = content_service.list_content(project_id=project["id"], category="decision", status="active")
    assert [memory["content"] for memory in decisions] == ["最终决定采用隔离数据库"]


def test_incomplete_legacy_conversation_revision_requires_regeneration(source):
    from app.database import decode, encode
    project, conversation = source
    handoff = handoff_service.create_handoff(
        project_id=project["id"], conversation_id=conversation["id"], target_agent="omp",
    )
    with db() as conn:
        revisions = decode(conn.execute(
            "SELECT source_revisions FROM handoffs WHERE id=?", (handoff["id"],),
        ).fetchone()["source_revisions"], {})
        revisions[conversation["id"]].pop("process_summary")
        conn.execute("UPDATE handoffs SET source_revisions=? WHERE id=?", (encode(revisions), handoff["id"]))
    with pytest.raises(HTTPException) as error:
        handoff_service.confirm_handoff(handoff["id"], confirmed=True, actor="omp")
    assert error.value.status_code == 409


@pytest.mark.anyio
async def test_queued_model_output_uses_the_same_stance_and_provenance_governance(source, monkeypatch):
    from app.database import set_app_setting
    from app.services import connection_service
    from app.transport import DistillResult

    _, conversation = source
    connection_service.patch_connection("codex", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)
    queue = distillation_service.queue_distillation(conversation["id"])
    stances = ("fact", "preference", "decision", "suggestion", "assumption", "quote")

    async def synthetic_transport(*_args, **_kwargs):
        return DistillResult(status="success", memories=[{
            "category": "decision", "stance": stance, "title": f"Queue stance {stance}",
            "content": "采用隔离数据库", "quote": conversation["messages"][0]["content"],
            "source_message_index": 0, "user_message_index": 0,
            "source_role": "assistant", "source_version": "forged",
            "tags": ["role:assistant", "source_version:forged", " Role:assistant", " SOURCE_VERSION:forged"],
        } for stance in stances] + [{
            "category": "preference", "title": "Unattributed model preference",
            "content": "The model omitted all supporting user evidence",
        }])

    monkeypatch.setattr("app.transport.run_distill", synthetic_transport)
    result = await distillation_service.process_distillation_queue_item(queue["id"])
    assert result["ok"] is True and result["created_count"] == len(stances)
    for memory_id in result["created_ids"]:
        memory = content_service.get_content(memory_id)
        assert "role:user" in memory["tags"] and "role:assistant" not in memory["tags"]
        assert "source_version:forged" not in memory["tags"]
        assert not {" Role:assistant", " SOURCE_VERSION:forged"}.intersection(memory["tags"])
        assert f"source_version:{conversation['content_hash']}" in memory["tags"]
        stance = memory["title"].removeprefix("Queue stance ")
        assert f"stance:{stance}" in memory["tags"]
        assert memory["category"] == (
            stance if stance in {"decision", "preference"} else "knowledge"
        )


@pytest.mark.anyio
async def test_checkpoint_refreshes_pending_source_before_model_invocation(source, monkeypatch):
    from app.database import set_app_setting
    from app.services import connection_service
    from app.transport import DistillResult

    _, conversation = source
    connection_service.patch_connection("codex", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)
    queued = distillation_service.queue_distillation(conversation["id"])
    distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", session_summary="Current checkpoint",
        auto_extract_from_messages=False,
    )
    calls = []

    async def synthetic_transport(_host, payload, **_kwargs):
        calls.append(payload["process_summary"])
        return DistillResult(status="success", memories=[{
            "category": "knowledge", "stance": "fact", "title": "Current source fact",
            "content": "A source-backed synthetic fact", "quote": conversation["messages"][0]["content"],
            "source_message_index": 0,
        }])

    monkeypatch.setattr("app.transport.run_distill", synthetic_transport)
    result = await distillation_service.process_distillation_queue_item(queued["id"])
    assert result["ok"] is True
    assert calls == ["Current checkpoint"]
    memory = content_service.get_content(result["created_ids"][0])
    current = conversation_service.get_conversation(conversation["id"])
    assert f"source_version:{current['content_hash']}" in memory["tags"]


@pytest.mark.parametrize("entrypoint", ["automatic", "structured"])
@pytest.mark.parametrize("stance", [None, "preference"])
def test_source_bound_preference_cannot_omit_supporting_user_evidence(source, entrypoint, stance):
    _, conversation = source
    memory = {
        "category": "preference", "title": "Unsupported source preference",
        "content": "Use the assistant's preferred database",
    }
    if stance is not None:
        memory["stance"] = stance
    if entrypoint == "automatic":
        result = distillation_service.auto_summarize_memory(
            conversation_id=conversation["id"], actor="codex",
            memories=[memory], auto_extract_from_messages=False,
        )
        # Chatting-agent paraphrase is stored. Background structured distillation stays gated.
        assert result["saved_count"] == 1
        assert len(content_service.list_content(category="preference", status="active")) == 1
    else:
        from app.services import connection_service
        connection_service.patch_connection("codex", enabled=True, confirmed=True)
        result = distillation_service.submit_structured_distillation(
            conversation["id"], [memory], actor="codex",
        )
        assert result["created_count"] == 0
        assert content_service.list_content(category="preference", status="active") == []


@pytest.mark.parametrize("category", ["decision", "preference"])
@pytest.mark.parametrize("indices", [
    {"source_message_index": 1, "user_message_index": 0},
    {"source_message_index": 1},
    {},
])
def test_repeated_quote_cannot_launder_assistant_provenance_into_user_authority(source, category, indices):
    project, _ = source
    quote = "确定使用隔离数据库。"
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Repeated user and assistant quote",
        messages=[{"role": "user", "content": quote}, {"role": "assistant", "content": quote}],
    )
    old = content_service.create_content(
        project_id=project["id"], title="Existing database", content="Keep the old database",
        category="decision", agent="codex",
    )
    memory = {
        "category": category, "stance": category, "title": "New database", "content": "使用隔离数据库",
        "quote": quote, **indices,
    }
    if category == "decision":
        memory["supersedes"] = {
            "id": old["id"], "expected_version": 1, "confidence": "explicit_confirmed",
            "evidence_quote": quote,
        }
    result = distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", memories=[memory],
        auto_extract_from_messages=False,
    )
    assert result["saved_count"] == 0
    assert content_service.get_content(old["id"])["status"] == "active"
    active = content_service.list_content(project_id=project["id"], category=category, status="active")
    assert {item["id"] for item in active} == ({old["id"]} if category == "decision" else set())


def test_explicit_user_turn_resolves_repeated_quote_provenance(source):
    project, _ = source
    quote = "我习惯使用隔离数据库。"
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Explicit user provenance",
        messages=[{"role": "user", "content": quote}, {"role": "assistant", "content": quote}],
    )
    result = distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", auto_extract_from_messages=False,
        memories=[{
            "category": "preference", "stance": "preference", "title": "Approved preference",
            "content": "使用隔离数据库", "quote": quote, "source_message_index": 0,
        }],
    )
    assert result["saved_count"] == 1
    memory = content_service.get_content(result["saved_memories"][0]["id"])
    assert "role:user" in memory["tags"] and "message_index:0" in memory["tags"]
    assert "role:assistant" not in memory["tags"]


def test_explicit_sourceless_preference_remains_an_independent_write(source):
    project, _ = source
    result = distillation_service.auto_summarize_memory(
        project_id=project["id"], actor="codex", auto_extract_from_messages=False,
        memories=[{"category": "preference", "title": "Direct independent preference",
                   "content": "Use isolated data"}],
    )
    assert result["saved_count"] == 1
    memory = content_service.get_content(result["saved_memories"][0]["id"])
    assert memory["category"] == "preference"
    assert memory["sources"] == []


@pytest.mark.parametrize("acknowledgement", ["好的", "ok", "继续", "。"])
@pytest.mark.parametrize("category", ["decision", "preference"])
def test_acknowledgement_cannot_authorize_an_unrelated_claim_or_supersession(source, acknowledgement, category):
    project, _ = source
    old = content_service.create_content(
        project_id=project["id"], category="decision", title="Retained local-data rule",
        content="Keep local data", agent="codex",
    )
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Acknowledgement is not a decision",
        messages=[
            {"role": "user", "content": acknowledgement},
            {"role": "assistant", "content": "建议采用外部模型并删除本地数据。"},
        ],
    )
    memory = {
        "category": category, "stance": category, "title": "Unrelated destructive claim",
        "content": "采用外部模型并删除本地数据", "quote": acknowledgement,
        "source_message_index": 0, "user_message_index": 0,
    }
    if category == "decision":
        memory["supersedes"] = {
            "id": old["id"], "expected_version": 1, "confidence": "explicit_confirmed",
            "evidence_quote": acknowledgement,
        }
    result = distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", auto_extract_from_messages=False,
        memories=[memory],
    )
    assert result["saved_count"] == 0
    assert content_service.get_content(old["id"])["status"] == "active"
    assert not any(item["title"] == memory["title"] for item in content_service.list_content(status="active"))


@pytest.mark.parametrize("quote", [None, "", "   ", 42])
def test_indexed_knowledge_requires_a_nonempty_matching_quote(source, quote):
    _, conversation = source
    result = distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", auto_extract_from_messages=False,
        memories=[{
            "category": "knowledge", "stance": "fact", "title": "Unbound indexed claim",
            "content": "A model claim is not source evidence", "source_message_index": 0, "quote": quote,
        }],
    )
    assert result["saved_count"] == 0
    assert content_service.list_content() == []


def test_ambiguous_unindexed_knowledge_quote_cannot_claim_source_provenance(source):
    project, _ = source
    quote = "Shared words in more than one turn"
    conversation = conversation_service.upsert_conversation(
        agent="codex", project_id=project["id"], title="Ambiguous knowledge quote",
        messages=[{"role": "user", "content": quote}, {"role": "assistant", "content": quote}],
    )
    result = distillation_service.auto_summarize_memory(
        conversation_id=conversation["id"], actor="codex", auto_extract_from_messages=False,
        memories=[{"category": "knowledge", "stance": "quote", "title": "Ambiguous source",
                   "content": quote, "quote": quote}],
    )
    assert result["saved_count"] == 0
    assert content_service.list_content() == []


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", [TypeError, AssertionError, RuntimeError])
async def test_programmer_errors_propagate_after_processing_safety_bookkeeping(source, monkeypatch, error_type):
    from app.database import set_app_setting
    from app.services import connection_service, processing_jobs

    _, conversation = source
    connection_service.patch_connection("codex", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)
    queued = distillation_service.queue_distillation(conversation["id"])
    calls = []

    async def broken_implementation(*_args, **_kwargs):
        calls.append(True)
        raise error_type("Synthetic programmer defect must propagate")

    monkeypatch.setattr("app.transport.run_distill", broken_implementation)
    with pytest.raises(error_type, match="programmer defect"):
        await distillation_service.process_distillation_queue_item(queued["id"])
    jobs = processing_jobs.list_processing_jobs()
    job = next(item for item in jobs if item["id"] == queued["id"])
    assert job["state"] == "failed" and job["invoked"] == 0
    assert job["error_code"] == "programmer_error"
    assert await distillation_service.process_distillation_queue_item(queued["id"]) is None
    assert calls == [True]
    assert content_service.list_content() == []


@pytest.mark.anyio
async def test_legacy_missing_role_is_not_presented_to_compute_as_user(source, monkeypatch):
    from app.database import encode, set_app_setting
    from app.services import connection_service
    from app.transport import DistillResult

    _, conversation = source
    messages = [{"id": "legacy-turn", "content": "A legacy turn without an attributed role"}]
    with db() as conn:
        revision = conversation_service.compute_content_revision_hash(
            conversation["title"], messages, conversation["process_summary"], conversation["cwd"],
        )
        conn.execute("UPDATE conversations SET messages=?,content_hash=? WHERE id=?", (
            encode(messages), revision, conversation["id"],
        ))
    connection_service.patch_connection("codex", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)
    queued = distillation_service.queue_distillation(conversation["id"])
    roles = []

    async def synthetic_transport(_host, payload, **_kwargs):
        roles.extend(message["role"] for message in payload["messages"])
        return DistillResult(status="success", memories=[])

    monkeypatch.setattr("app.transport.run_distill", synthetic_transport)
    result = await distillation_service.process_distillation_queue_item(queued["id"])
    assert result["ok"] is True
    assert roles == ["unknown"]


@pytest.mark.anyio
@pytest.mark.parametrize("send_started,result_received", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("kind", ["transport", "implementation"])
async def test_typed_worker_failure_records_the_actual_send_and_result_boundary(
    source, monkeypatch, send_started, result_received, kind,
):
    from app.database import set_app_setting
    from app.services import connection_service, processing_jobs
    from app.transport import DistillExecutionError

    _, conversation = source
    connection_service.patch_connection("codex", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)
    queued = distillation_service.queue_distillation(conversation["id"])
    calls = []

    async def synthetic_worker_failure(*_args, **_kwargs):
        calls.append(True)
        raise DistillExecutionError(
            "Synthetic safe worker failure", send_started=send_started,
            result_received=result_received, kind=kind,
        )

    monkeypatch.setattr("app.transport.run_distill", synthetic_worker_failure)
    unknown = send_started and not result_received
    if kind == "implementation":
        with pytest.raises(DistillExecutionError, match="Synthetic safe worker failure"):
            await distillation_service.process_distillation_queue_item(queued["id"])
    else:
        result = await distillation_service.process_distillation_queue_item(queued["id"])
        assert result["ok"] is False
        assert result["status"] == ("unknown" if unknown else "error")
    job = next(item for item in processing_jobs.list_processing_jobs() if item["id"] == queued["id"])
    assert job["state"] == ("unknown" if unknown else "failed")
    assert job["invoked"] == int(send_started)
    if kind == "implementation" and not unknown:
        assert job["error_code"] == "programmer_error"
    assert await distillation_service.process_distillation_queue_item(queued["id"]) is None
    assert await distillation_service.process_pending_distillations() == 0
    assert calls == [True]
    assert content_service.list_content() == []


@pytest.mark.anyio
@pytest.mark.parametrize("send_started,result_received", [(False, False), (True, False), (True, True)])
async def test_cancellation_propagates_with_its_actual_worker_boundary(
    source, monkeypatch, send_started, result_received,
):
    import asyncio
    from app.database import set_app_setting
    from app.services import connection_service, processing_jobs

    _, conversation = source
    connection_service.patch_connection("codex", enabled=True, confirmed=True)
    connection_service.patch_connection("omp", enabled=True, confirmed=True)
    set_app_setting("compute_host", "omp")
    set_app_setting("compute_authorized", True)
    queued = distillation_service.queue_distillation(conversation["id"])

    async def cancelled_worker(*_args, **_kwargs):
        cancelled = asyncio.CancelledError()
        cancelled.send_started = send_started
        cancelled.result_received = result_received
        raise cancelled

    monkeypatch.setattr("app.transport.run_distill", cancelled_worker)
    with pytest.raises(asyncio.CancelledError):
        await distillation_service.process_distillation_queue_item(queued["id"])
    job = next(item for item in processing_jobs.list_processing_jobs() if item["id"] == queued["id"])
    assert job["state"] == ("unknown" if send_started and not result_received else "failed")
    assert job["invoked"] == int(send_started)
    assert await distillation_service.process_distillation_queue_item(queued["id"]) is None
    assert content_service.list_content() == []
