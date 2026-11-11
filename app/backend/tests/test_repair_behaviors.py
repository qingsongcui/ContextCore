import json
import threading

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import security
from app.database import create_schema, db, new_id, now_iso
from app.services import connection_service as service
from app.services import conversation_service, handoff_service
from app.services.sync_budget import SyncBudget


def _client():
    security._SESSIONS.clear()
    app = FastAPI()
    app.add_middleware(security.SecurityMiddleware)
    from app.routers.session import router
    app.include_router(router)
    return TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))


def test_repeat_session_keeps_one_csrf_cookie():
    with _client() as client:
        first = client.get("/api/session")
        second = client.get("/api/session")
        assert first.status_code == 200
        assert second.json()["csrf_token"] == first.json()["csrf_token"]
        assert len(security._SESSIONS) == 1


def test_path_only_row_adopts_later_external_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "identity.sqlite"))
    create_schema()
    source = str(tmp_path / "session.jsonl")
    first = conversation_service.upsert_conversation(
        agent="omp", source_path=source, title="Draft",
        messages=[{"role": "user", "content": "original"}],
    )
    second = conversation_service.upsert_conversation(
        agent="omp", external_id="omp:session-1", source_path=source, title="Draft",
        messages=[{"role": "user", "content": "original"}],
    )
    assert second["id"] == first["id"]
    assert len(conversation_service.list_conversations(agent="omp")) == 1


def test_deleted_external_id_blocks_later_path_only_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "identity.sqlite"))
    create_schema()
    source = str(tmp_path / "session.jsonl")
    created = conversation_service.upsert_conversation(
        agent="omp", external_id="omp:session-1", source_path=source, title="Draft",
        messages=[{"role": "user", "content": "gone"}],
    )
    conversation_service.delete_conversation(created["id"])
    with pytest.raises(HTTPException) as exc:
        conversation_service.upsert_conversation(
            agent="omp", source_path=source, title="Draft",
            messages=[{"role": "user", "content": "gone"}],
        )
    assert exc.value.status_code == 410


def test_incremental_merge_does_not_reuse_existing_message_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "identity.sqlite"))
    create_schema()
    created = conversation_service.upsert_conversation(
        agent="omp", external_id="omp:turns", title="Turns",
        messages=[{"id": "m3", "role": "user", "content": "kept"}],
    )
    updated = conversation_service.upsert_conversation(
        agent="omp", external_id="omp:turns", title="Turns", incremental=True,
        messages=[{"role": "assistant", "content": "one"}, {"role": "assistant", "content": "two"}],
    )
    assert updated["id"] == created["id"]
    ids = [message["id"] for message in updated["messages"]]
    assert len(ids) == len(set(ids))
    assert "m3" in ids


def test_confirmed_handoff_cannot_be_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "handoff.sqlite"))
    create_schema()
    hid = new_id("hand")
    now = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO handoffs
            (id, project_id, source_agent, target_agent, status, card, source_ids, source_revisions, created_at, confirmed_at)
            VALUES (?, NULL, 'codex', 'cursor', 'awaiting_confirmation', '{}', '[]', '{}', ?, ?)""",
            (hid, now, now),
        )
    handoff_service.confirm_handoff(hid, confirmed=True, actor='cursor')
    with pytest.raises(HTTPException) as exc:
        handoff_service.cancel_handoff(hid)
    assert exc.value.status_code == 409
    with db() as conn:
        status = conn.execute("SELECT status FROM handoffs WHERE id=?", (hid,)).fetchone()[0]
    assert status == "confirmed"


def test_summary_conversation_list_omits_transcripts(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "summary.sqlite"))
    create_schema()
    conversation_service.upsert_conversation(
        agent="cursor", external_id="cursor:one", title="Visible title",
        messages=[{"role": "user", "content": "x" * 4000}],
    )
    rows = conversation_service.list_conversations(summary=True)
    assert rows[0]["title"] == "Visible title"
    assert rows[0]["messages"] == []


def test_terminal_format_error_is_not_reread(tmp_path, monkeypatch):
    create_schema()
    from app.database import seed_data
    seed_data()
    root = tmp_path / "history"
    root.mkdir()
    bad = root / "bad.jsonl"
    bad.write_text("{not-json}\n")
    service.patch_connection("claude-code", paths=[str(root)], enabled=True, confirmed=True)
    calls = {"n": 0}

    def fail_read(*args, **kwargs):
        calls["n"] += 1
        from app.adapters import AgentAdapterError
        raise AgentAdapterError(host="claude-code", code="unsupported_format", path=str(bad.resolve()), detail="bad json")

    monkeypatch.setattr("app.adapters.read_sessions", fail_read)
    service._sync_locks.clear()
    service.sync_connection("claude-code", force=False, budget=SyncBudget(threading.Event(), duty_cycle=1))
    service._sync_locks.clear()
    second = service.sync_connection("claude-code", force=False, budget=SyncBudget(threading.Event(), duty_cycle=1))
    assert calls["n"] == 1
    assert second.get("reason") == "source_unchanged"
    assert str(second.get("connection", {}).get("error") or "").startswith("skipped_damaged:")
    bad.write_text('{"type":"user"}\n')
    service._sync_locks.clear()
    service.sync_connection("claude-code", force=False, budget=SyncBudget(threading.Event(), duty_cycle=1))
    assert calls["n"] == 2


def test_codex_index_change_stays_inside_source_budget(tmp_path, monkeypatch):
    create_schema()
    from app.database import seed_data
    seed_data()
    root = tmp_path / "codex"
    root.mkdir()
    for index in range(5):
        (root / f"rollout-{index}.jsonl").write_text("{}\n")
    (root / "session_index.jsonl").write_text("{}\n")
    service.patch_connection("codex", paths=[str(root)], enabled=True, confirmed=True)
    seen = []

    def record_read(connection_id, roots, source_paths=None, allow_execution=False, session_titles=None):
        seen.append(list(source_paths or []))
        assert session_titles is None or len(session_titles) <= 2
        return []

    monkeypatch.setattr("app.adapters.read_sessions", record_read)
    budget = SyncBudget(threading.Event(), duty_cycle=1)
    budget.max_sources = 2
    service._sync_locks.clear()
    result = service.sync_connection("codex", force=False, budget=budget)
    assert result["pending_sources"] > 0
    rollouts = [path for path in seen[0] if not path.endswith("session_index.jsonl")]
    assert len(rollouts) <= 2
