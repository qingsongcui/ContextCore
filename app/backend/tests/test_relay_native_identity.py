"""Native session selection stays fresh, scoped, and non-executing."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.database import create_schema, db, encode
from app.routers.conversations import router as conversation_router
from app.routers.handoffs import router as handoff_router
from app.services import connection_service, conversation_service, handoff_service, project_service, working_memory_service

UUID = "123e4567-e89b-42d3-a456-426614174000"


@pytest.fixture(autouse=True)
def schema():
    create_schema()


def make_conversation(**kwargs):
    return conversation_service.upsert_conversation(
        **{"agent": "antigravity", "title": "Repeated opening", "external_id": f"antigravity:{UUID}",
           "messages": [{"role": "user", "content": "Inspect the existing implementation"},
                        {"role": "assistant", "content": "Latest recorded progress"}], **kwargs}
    )


@pytest.mark.parametrize("changed", [False, True])
def test_path_only_import_acquires_native_identity_even_without_content_change(tmp_path, changed):
    source = str(tmp_path / "session.json")
    original = make_conversation(external_id=None, source_path=source)
    updated = make_conversation(source_path=source, title="New title" if changed else original["title"])
    assert updated["id"] == original["id"]
    assert updated["external_id"] == f"antigravity:{UUID}"
    assert make_conversation(source_path=source)["id"] == original["id"]
    conversation_service.delete_conversation(original["id"])
    with pytest.raises(HTTPException) as exc:
        make_conversation(source_path=source)
    assert exc.value.status_code == 410


def test_native_identity_search_and_rest_response_without_uuid_in_messages():
    conv = make_conversation()
    assert UUID not in encode(conv["messages"])
    app = FastAPI()
    app.include_router(conversation_router)
    with TestClient(app) as client:
        listing = client.get("/api/conversations", params={"query": UUID}).json()
        assert [c["id"] for c in listing] == [conv["id"]]
        assert listing[0]["external_id"] == conv["external_id"]
        assert client.get(f"/api/conversations/{conv['id']}").json()["external_id"] == conv["external_id"]
        assert client.get("/api/conversations/index", params={"query": UUID}).json()["total"] == 1


def test_selected_uuid_refreshes_only_source_and_ignores_unrelated_work(tmp_path, monkeypatch):
    project = project_service.create_project(name="Selected project")
    for title in ("Other active work A", "Other active work B"):
        working_memory_service.upsert_working_task(title=title, project_id=project["id"], execution_mode="discussion_only")
    source = str(tmp_path / "session.json")
    conv = make_conversation(project_id=project["id"], source_path=source)
    calls = []

    def refresh(identifier, *, agent=None, source_path=None):
        calls.append((identifier, agent, source_path))
        make_conversation(project_id=project["id"], source_path=source, messages=[
            {"role": "user", "content": "Inspect the existing implementation"},
            {"role": "assistant", "content": "Fresh final response from selected native session"},
        ])
        return [{"agent_id": agent, "status": "ok"}]

    def forbidden(*args, **kwargs):
        raise AssertionError("Selected source must not discover every host/project, choose project tasks, or create a card")

    monkeypatch.setattr(connection_service, "sync_conversation_for_relay", refresh)
    for name in ("discover_relay_conversations", "sync_recent_agent_chats_for_relay", "list_project_tasks", "create_handoff", "confirm_handoff"):
        monkeypatch.setattr(handoff_service, name, forbidden)
    result = handoff_service.relay_conversation(UUID, consumer="omp", query=f"选择 {UUID}", max_tokens=32000)
    assert calls == [(conv["external_id"], "antigravity", source)]
    assert result["status"] == "loaded"
    assert result["must_not_execute"] is True and result["confirmed"] is False
    assert result["continuation"]["selected_task_id"] is None
    assert result["handoff_id"] is None
    assert result["selected_conversation"]["external_id"] == conv["external_id"]
    assert "Fresh final response" in result["context_text"]
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='handoff.confirm'").fetchone()[0] == 0


def test_previously_unknown_native_uuid_is_target_refreshed_before_resolution(monkeypatch):
    def refresh(identifier, **kwargs):
        assert identifier == UUID and kwargs == {"agent": None, "source_path": None}
        make_conversation()
        return [{"agent_id": "antigravity", "status": "ok"}]
    monkeypatch.setattr(connection_service, "sync_conversation_for_relay", refresh)
    result = handoff_service.relay_conversation(UUID, consumer="omp", max_tokens=32000)
    assert result["status"] == "loaded"
    assert result["selected_conversation"]["external_id"] == f"antigravity:{UUID}"


@pytest.mark.parametrize("selector", [UUID, "Repeated opening"])
def test_ambiguous_native_uuid_or_title_never_picks_latest(selector, monkeypatch):
    first = make_conversation()
    second = make_conversation(agent="cursor", external_id=f"cursor:{UUID}")
    def forbidden(*args, **kwargs):
        raise AssertionError("Ambiguous selection must not refresh or execute")
    monkeypatch.setattr(connection_service, "sync_conversation_for_relay", forbidden)
    with pytest.raises(HTTPException) as exc:
        handoff_service.relay_conversation(selector, consumer="omp", max_tokens=32000)
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "ambiguous_conversation"
    assert {c["conversation_id"] for c in exc.value.detail["candidates"]} == {first["id"], second["id"]}


def test_discovery_keeps_same_title_sessions_and_unknown_project_but_not_foreign_project(tmp_path):
    project = project_service.create_project(name="Current", directories=[str(tmp_path)])
    foreign = project_service.create_project(name="Foreign")
    first = make_conversation(project_id=project["id"])
    second = make_conversation(project_id=project["id"], external_id="antigravity:second")
    unknown = make_conversation(external_id="antigravity:unknown")
    other = make_conversation(project_id=foreign["id"], cwd=str(tmp_path / "nested"), external_id="antigravity:foreign")
    result = handoff_service.discover_relay_conversations(project_id=project["id"], cwd=str(tmp_path), sync_first=False, link_unlinked=False)
    candidates = {c["id"]: c for c in result["candidates"]}
    assert set(candidates) == {first["id"], second["id"], unknown["id"]}
    assert other["id"] not in candidates
    assert candidates[unknown["id"]]["scope_match"] == "unassigned"
    assert candidates[first["id"]]["external_id"] == first["external_id"]
    assert "未知项目" in result["question_prompt"]


def test_discovery_reports_real_counts_beyond_old_cap_and_exposes_http_pagination():
    with db() as conn:
        conn.executemany(
            "INSERT INTO conversations(id,external_id,agent,title,started_at,updated_at,messages,process_summary,processing_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(f"conv_page_{i:03}", f"antigravity:page-{i}", "antigravity", "Same opening", "2026-11-12", "2026-11-12",
              encode([{"role": "user", "content": "Inspect the existing implementation"}]), "", "pending", "2026-11-12") for i in range(205)],
        )
    app = FastAPI()
    app.include_router(handoff_router)
    with TestClient(app) as client:
        page = client.get("/api/handoffs/relay/conversations", params={"consumer": "omp", "limit": 5, "offset": 200, "sync_first": False}).json()
        assert page["total_candidates"] == 205
        assert page["total"] == 5 and page["omitted_count"] == 200
        assert page["next_offset"] is None and page["has_more"] is False
        assert [c["index"] for c in page["candidates"]] == [201, 202, 203, 204, 205]
        loaded = client.get("/api/handoffs/relay/conversation", params={
            "conversation_id": "201", "consumer": "omp", "offset": 200, "limit": 5, "sync_first": False, "max_tokens": 32000,
        }).json()
        assert loaded["conversation_id"] == page["candidates"][0]["conversation_id"]
        assert loaded["must_not_execute"] is True


def test_small_budget_never_emits_executable_or_unbounded_selected_context():
    conv = make_conversation(messages=[{"role": "assistant", "content": "Large latest content " * 1000}])
    result = handoff_service.relay_conversation(conv["id"], consumer="omp", sync_first=False, max_tokens=256)
    assert result["status"] == "insufficient_budget"
    assert result["must_not_execute"] is True
    assert "context_text" not in result and "selected_conversation" not in result
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 0


def test_refresh_failure_is_visible_without_claiming_freshness(monkeypatch):
    conv = make_conversation()
    monkeypatch.setattr(connection_service, "sync_conversation_for_relay", lambda *a, **kw: [{"agent_id": "antigravity", "status": "error"}])
    result = handoff_service.relay_conversation(conv["id"], consumer="omp", max_tokens=32000)
    assert result["status"] == "loaded" and result["sync_failed"] is True
    assert result["must_not_execute"] is True


@pytest.mark.parametrize("status,reason", [("partial", None), ("waiting", None), ("skipped", "sync_in_progress")])
def test_incomplete_refresh_stays_cached_and_cannot_confirm_task(monkeypatch, status, reason):
    conv = make_conversation()
    monkeypatch.setattr(connection_service, "sync_conversation_for_relay", lambda *a, **kw: [
        {"agent": "antigravity", "status": status, "reason": reason},
    ])
    result = handoff_service.relay_conversation(conv["id"], consumer="omp", query="Implement the next fix", max_tokens=32000)
    assert result["history_fresh"] is False and result["sync_failed"] is True
    assert result["must_not_execute"] is True and result["confirmed"] is False
    assert "未确认包含宿主最新消息" in result["context_text"]


def test_linked_discussion_constraints_survive_read_without_other_project_tasks(monkeypatch):
    project = project_service.create_project(name="Linked discussion")
    conv = make_conversation(project_id=project["id"])
    working_memory_service.upsert_working_task(
        title="Discuss native capture", project_id=project["id"], source_conversation_id=conv["id"],
        execution_mode="discussion_only", temp_constraints=["Do not rewrite native host settings"],
    )
    result = handoff_service.relay_conversation(conv["id"], consumer="omp", sync_first=False, max_tokens=32000)
    assert result["status"] == "loaded" and result["must_not_execute"] is True
    assert result["execution_mode"] == "discussion_only"
    assert "Do not rewrite native host settings" in result["card"]["decisions"]


def test_unknown_explicit_project_does_not_fall_back_to_global_candidates():
    make_conversation()
    with pytest.raises(HTTPException) as exc:
        handoff_service.discover_relay_conversations(project_id="missing-project", sync_first=False)
    assert exc.value.status_code == 404


def test_discovery_reads_only_metadata_and_bounded_derived_index(monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    indexed = make_conversation()
    with db() as conn:
        conn.execute(
            """INSERT INTO conversations
               (id,agent,title,started_at,updated_at,messages,process_summary,processing_status,created_at)
               VALUES ('legacy-only','cursor','Legacy source','2026-11-11','2026-11-11',?,?,'pending','2026-11-11')""",
            ("not-json-" * 10000, "RAW_SUMMARY_MUST_NOT_BE_READ" * 10000),
        )

    @contextmanager
    def metadata_only_db():
        with db() as conn:
            def authorize(action, table, column, *_):
                if action == sqlite3.SQLITE_READ and table == "conversations" and column in ("messages", "process_summary"):
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            yield conn

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not summarize transcripts on demand")

    monkeypatch.setattr(handoff_service, "db", metadata_only_db)
    monkeypatch.setattr(handoff_service, "_summarize_conversation_for_relay", forbidden)
    result = handoff_service.discover_relay_conversations(consumer="antigravity", limit=1, sync_first=False, link_unlinked=False)
    assert result["total_candidates"] == 2 and result["omitted_count"] == 1
    assert result["candidates"][0]["id"] == indexed["id"]
    legacy = handoff_service.discover_relay_conversations(limit=1, offset=1, sync_first=False, link_unlinked=False)
    assert legacy["candidates"][0]["id"] == "legacy-only"
    assert legacy["candidates"][0]["summary_status"] == "metadata_only"
    assert legacy["candidates"][0]["message_count"] is None
    assert "RAW_SUMMARY" not in str(legacy)


def test_recent_unassigned_session_precedes_hundreds_of_older_project_records(tmp_path):
    project = project_service.create_project(name="Current", directories=[str(tmp_path)])
    with db() as conn:
        conn.executemany(
            """INSERT INTO conversations
               (id,agent,title,project_id,cwd,started_at,updated_at,messages,processing_status,created_at)
               VALUES (?, 'cursor','Older project session',?,?,'2026-11-11','2026-11-11','[]','pending','2026-11-11')""",
            [(f"old-{i:03}", project["id"], str(tmp_path)) for i in range(247)],
        )
    recent = make_conversation(updated_at="2026-11-12T12:00:00Z")
    result = handoff_service.discover_relay_conversations(
        project_id=project["id"], cwd=str(tmp_path), limit=5, sync_first=False, link_unlinked=False,
    )
    assert result["total_candidates"] == 248
    assert result["candidates"][0]["id"] == recent["id"]
    assert result["candidates"][0]["scope_match"] == "unassigned"
    assert "不代表属于当前项目" in result["question_prompt"]


def test_derived_index_updates_and_deletes_with_source():
    conv = make_conversation()
    conversation_service.update_conversation(conv["id"], title="Renamed native session")
    result = handoff_service.discover_relay_conversations(sync_first=False, link_unlinked=False)
    assert result["candidates"][0]["title"] == "Renamed native session"
    with db() as conn:
        conn.execute("UPDATE conversations SET title='Unindexed revision',content_hash='external-change' WHERE id=?", (conv["id"],))
    result = handoff_service.discover_relay_conversations(sync_first=False, link_unlinked=False)
    assert result["candidates"][0]["title"] == "Unindexed revision"
    assert result["candidates"][0]["summary_status"] == "metadata_only"
    conversation_service.delete_conversation(conv["id"])
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM conversation_relay_index").fetchone()[0] == 0


@pytest.mark.parametrize("reason", ["background_refresh_requested", "background_refresh_unavailable"])
def test_empty_index_during_background_refresh_is_pending_not_empty_history(monkeypatch, reason):
    monkeypatch.setattr(connection_service, "sync_recent_connections_for_relay", lambda: [
        {"agent": "antigravity", "status": "pending", "reason": reason},
    ])
    result = handoff_service.discover_relay_conversations(sync_first=True)
    assert result["status"] == "sync_pending" and result["sync_pending"] is True
    assert result["must_not_execute"] is True and result["candidates"] == []
    assert result["sync_unavailable"] is (reason == "background_refresh_unavailable")
    assert "不能据此判断宿主没有历史记录" in result["question_prompt"]


def test_default_budget_keeps_latest_request_and_exact_chinese_reply_tail():
    import json

    old = [{"role": "user" if i % 2 == 0 else "assistant", "content": "全面了解项目" if i == 0 else f"旧记录 {i}"} for i in range(304)]
    request = "为什么聊天记录没了？请查看刚结束的对话。"
    answer = "较早的长答复正文。" * 10000 + "最终进展：已定位原生会话，等待确认。"
    conv = make_conversation(messages=[*old, {"role": "user", "content": request},
        {"role": "assistant", "content": answer, "timestamp": "2026-11-12T10:35:42Z"}])
    result = handoff_service.relay_conversation(conv["id"], consumer="omp", sync_first=False)
    assert result["status"] == "loaded" and result["must_not_execute"] is True
    messages = result["selected_conversation"]["messages"]
    assert messages == result["context"]["selected_conversation"]["messages"]
    assert messages[0]["content"] == request
    tail = messages[-1]["content"]
    assert tail != answer and answer.endswith(tail)
    assert tail.endswith("最终进展：已定位原生会话，等待确认。")
    assert messages[-1]["timestamp"] == "2026-11-12T10:35:42Z"
    assert request in result["context_text"] and tail in result["context_text"]
    coverage = result["coverage"]
    assert coverage["scope"] == "partial"
    assert coverage["total_messages"] == 306 and coverage["omitted_older_messages"] == 304
    assert coverage["clipped_message"]["source_index"] == 305
    assert coverage["clipped_message"]["included_bytes"] == len(tail.encode("utf-8"))
    assert coverage["clipped_message"]["omitted_prefix_bytes"] + len(tail.encode("utf-8")) == len(answer.encode("utf-8"))
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 12000


def test_simple_unlinked_selection_fits_project_relay_default_budget():
    import json

    conv = make_conversation(messages=[
        {"role": "user", "content": "写一个清理过期会话的 SQL 脚本。"},
        {"role": "assistant", "content": "已生成 DELETE FROM sessions WHERE expires_at < NOW() 脚本。"},
    ])
    result = handoff_service.relay_project(conversation_id=conv["id"], consumer="omp", sync_first=False)
    assert result["status"] == "loaded" and result["must_not_execute"] is True
    assert "清理过期会话" in result["context_text"]
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 4000


def test_protected_rules_are_not_clipped_to_manufacture_success():
    from app.services import content_service

    project = project_service.create_project(name="Protected rules")
    content_service.create_content(
        project_id=project["id"], category="preference", title="Protected instruction",
        content="必须完整保留的用户约束。" * 1000, agent="omp",
    )
    conv = make_conversation(project_id=project["id"])
    result = handoff_service.relay_conversation(conv["id"], consumer="omp", sync_first=False)
    assert result["status"] == "insufficient_budget" and result["must_not_execute"] is True


def test_budget_failure_never_confirms_concrete_task_handoff():
    project = project_service.create_project(name="Small protected budget")
    conv = make_conversation(project_id=project["id"])
    result = handoff_service.relay_conversation(
        conv["id"], consumer="omp", query="Implement the next explicit fix",
        sync_first=False, max_tokens=256,
    )
    assert result["status"] == "insufficient_budget" and result["must_not_execute"] is True
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='handoff.confirm'").fetchone()[0] == 0


def test_numbered_selection_keeps_the_listed_conversation_when_a_newer_chat_lands():
    project = project_service.create_project(name="Frozen numbering")
    listed = conversation_service.upsert_conversation(
        agent="antigravity", title="Listed conversation", external_id="antigravity:listed",
        project_id=project["id"], updated_at="2026-11-12T08:00:00Z",
        messages=[{"role": "user", "content": "First listed session body"}],
    )
    listing = handoff_service.discover_relay_conversations(
        consumer="omp", project_id=project["id"], sync_first=False, link_unlinked=False,
    )
    assert [(c["index"], c["conversation_id"]) for c in listing["candidates"]] == [(1, listed["id"])]

    # Background sync ingests a newer conversation after the list was printed.
    newer = conversation_service.upsert_conversation(
        agent="antigravity", title="Arrived later", external_id="antigravity:arrived-later",
        project_id=project["id"], updated_at="2026-11-12T12:00:00Z",
        messages=[{"role": "user", "content": "Unrelated newer session body"}],
    )
    assert newer["id"] != listed["id"]

    relayed = handoff_service.relay_conversation(
        "1", consumer="omp", project_id=project["id"], sync_first=False,
    )
    assert relayed["conversation_id"] == listed["id"]
    assert "First listed session body" in relayed["context_text"]
    assert "Unrelated newer session body" not in relayed["context_text"]

    refreshed = handoff_service.discover_relay_conversations(
        consumer="omp", project_id=project["id"], sync_first=False, link_unlinked=False,
    )
    assert refreshed["candidates"][0]["index"] == 1
    assert refreshed["candidates"][0]["conversation_id"] == newer["id"]


def test_numbered_selection_from_a_later_offset_page_needs_no_repeated_offset():
    created = [
        conversation_service.upsert_conversation(
            agent="cursor", title=f"Paged session {i:02d}", external_id=f"cursor:paged-{i:02d}",
            updated_at=f"2026-11-12T00:{i + 1:02d}:00Z",
            messages=[{"role": "user", "content": f"Paged session body {i:02d}"}],
        )
        for i in range(20)
    ]
    page = handoff_service.discover_relay_conversations(
        consumer="omp", limit=15, offset=15, sync_first=False, link_unlinked=False,
    )
    assert [c["index"] for c in page["candidates"]] == [16, 17, 18, 19, 20]
    newest_first = list(reversed(created))
    assert [c["conversation_id"] for c in page["candidates"]] == [c["id"] for c in newest_first[15:20]]

    # Replying with #16 without repeating offset=15 must still load that row.
    relayed = handoff_service.relay_conversation("16", consumer="omp", sync_first=False)
    assert relayed["conversation_id"] == created[4]["id"]
    assert "Paged session body 04" in relayed["context_text"]
