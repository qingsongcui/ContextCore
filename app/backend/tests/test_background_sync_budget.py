import json
import threading

import pytest

from app.database import create_schema, seed_data, db
from app.services import connection_service as service, conversation_service
from app.services.sync_budget import SyncBudget, SyncCancelled


@pytest.fixture
def history(tmp_path):
    create_schema()
    seed_data()
    root = tmp_path / "synthetic-history"
    root.mkdir()
    return root


def budget(limit=2):
    result = SyncBudget(threading.Event(), duty_cycle=1)
    result.max_sources = limit
    return result


def test_background_batches_checkpoint_only_processed_sources(history):
    for index in range(5):
        (history / f"{index}.jsonl").write_text(json.dumps({
            "type": "user", "uuid": str(index), "sessionId": str(index),
            "message": {"role": "user", "content": "Synthetic batch fixture"}
        }) + "\n")
    service.patch_connection("claude-code", paths=[str(history)], enabled=True, confirmed=True)
    for expected, pending in ((2, 3), (4, 1), (5, 0)):
        result = service.sync_connection("claude-code", force=False, budget=budget())
        assert result["pending_sources"] == pending
        with db() as conn:
            assert conn.execute("SELECT count(*) FROM source_sync_cursors WHERE status='ready'").fetchone()[0] == expected
        assert len(conversation_service.list_conversations(agent="claude-code")) == expected
        service._sync_locks.clear()  # restart does not lose progress
    assert service.sync_connection("claude-code", force=False, budget=budget())["reason"] == "source_unchanged"


def test_antigravity_changed_group_does_not_reparse_other_sessions(history, monkeypatch):
    paths = []
    for index in range(3):
        target = history / str(index) / ".system_generated" / "logs"
        target.mkdir(parents=True)
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            source = target / name
            source.write_text(json.dumps({"type": "USER_INPUT", "step_index": 1, "content": "Synthetic group"}) + "\n")
            paths.append(source)
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    first = service.sync_connection("antigravity", force=False, budget=budget(1))
    assert first["processed_sources"] == 2 and first["pending_sources"] == 2
    service.sync_connection("antigravity", force=False, budget=budget(1))
    service.sync_connection("antigravity", force=False, budget=budget(1))
    paths[0].write_text(json.dumps({"type": "USER_INPUT", "step_index": 1, "content": "Synthetic revision"}) + "\n")
    import app.adapters
    original = app.adapters.read_sessions
    calls = []
    def tracked(*args, **kwargs):
        calls.append(kwargs["source_paths"])
        return original(*args, **kwargs)
    monkeypatch.setattr(app.adapters, "read_sessions", tracked)
    service.sync_connection("antigravity", force=False, budget=budget())
    assert set(calls[0]) == {str(paths[0]), str(paths[1])}


def test_cancelled_background_budget_does_not_advance_sources(history):
    (history / "one.jsonl").write_text('{"type":"user","uuid":"1","message":{"role":"user","content":"fixture"}}\n')
    service.patch_connection("claude-code", paths=[str(history)], enabled=True, confirmed=True)
    control = budget()
    control.stop.set()
    with pytest.raises(SyncCancelled):
        service.sync_connection("claude-code", force=False, budget=control)
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM source_sync_cursors").fetchone()[0] == 0


def test_background_parses_one_source_at_a_time_and_keeps_failed_cursor(history, monkeypatch):
    for name in ("a", "b", "c"):
        (history / f"{name}.jsonl").write_text(json.dumps({"type": "user", "uuid": name, "sessionId": name,
            "message": {"role": "user", "content": "synthetic"}}) + "\n")
    (history / "bad.jsonl").write_text('{broken}\n')
    service.patch_connection("claude-code", paths=[str(history)], enabled=True, confirmed=True)
    import app.adapters
    original = app.adapters.read_sessions
    def tracked(*args, **kwargs):
        assert len(kwargs["source_paths"]) == 1
        return original(*args, **kwargs)
    monkeypatch.setattr(app.adapters, "read_sessions", tracked)
    result = service.sync_connection("claude-code", force=False, budget=budget(16))
    assert str(result.get("error") or "").startswith("skipped_damaged:")
    with db() as conn:
        assert conn.execute("SELECT count(*) FROM source_sync_cursors WHERE status='ready'").fetchone()[0] == 3
        failed = conn.execute("SELECT * FROM source_sync_cursors WHERE status='error'").fetchone()
        assert failed["fingerprint"]
    (history / "new.jsonl").write_text(json.dumps({"type": "user", "uuid": "new", "sessionId": "new",
        "message": {"role": "user", "content": "new synthetic"}}) + "\n")
    # A stable format error does not consume the next budget. New readable work proceeds.
    next_batch = service.sync_connection("claude-code", force=False, budget=budget(1))
    assert next_batch["pending_sources"] == 0
    assert next_batch["processed_sources"] == 1
    with db() as conn:
        assert conn.execute("SELECT status FROM source_sync_cursors WHERE source_path LIKE '%bad.jsonl'").fetchone()[0] == "error"


def test_budget_yields_cpu_and_shutdown_interrupts_wait(monkeypatch):
    from app.services import sync_budget
    times = iter((10.0, 11.0))
    cpus = iter((1.0, 1.5))
    monkeypatch.setattr(sync_budget.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(sync_budget.time, "thread_time", lambda: next(cpus))
    class Stop:
        def is_set(self): return False
        def wait(self, delay):
            assert delay == pytest.approx(1.5)
            return True
    with pytest.raises(SyncCancelled):
        SyncBudget(Stop(), duty_cycle=0.2).checkpoint()


def test_worker_shutdown_waits_for_active_atomic_unit(history, monkeypatch):
    import asyncio
    from app.main import _background_worker
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    service.patch_connection("claude-code", paths=[str(history)], enabled=True, confirmed=True)
    def active_unit(*args, budget, **kwargs):
        entered.set()
        release.wait(2)
        assert budget.stop.is_set()
        exited.set()
        return {}
    monkeypatch.setattr(service, "sync_connection", active_unit)
    async def scenario():
        task = asyncio.create_task(_background_worker())
        while not entered.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and not exited.is_set()
        release.set()
        await asyncio.wait_for(task, 1)
        assert exited.is_set()
    asyncio.run(scenario())


def _native_antigravity(root, session_id, text="Question"):
    source = root / session_id / ".system_generated" / "logs" / "transcript_full.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"type": "USER_INPUT", "step_index": 1, "content": text}) + "\n")
    return source


def test_newest_antigravity_source_precedes_lexical_backlog(history):
    import os
    old = _native_antigravity(history, "aaa-old")
    newest = _native_antigravity(history, "zzz-new")
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newest, ns=(2_000_000_000, 2_000_000_000))
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    result = service.sync_connection("antigravity", force=False, budget=budget(1))
    assert result["pending_sources"] == 1
    assert [item["external_id"] for item in conversation_service.list_conversations(agent="antigravity")] == ["antigravity:zzz-new"]
    with newest.open("a") as handle:
        handle.write('{"type":"PLANNER_RESPONSE","step_index":2,"content":"Finished now"}\n')
    service.sync_connection("antigravity", force=False, budget=budget(1))
    conversations = conversation_service.list_conversations(agent="antigravity")
    assert len(conversations) == 1  # Fresh append takes priority over the old backlog.
    assert conversation_service.get_conversation(conversations[0]["id"])["messages"][-1]["content"] == "Finished now"
    service.sync_connection("antigravity", force=False, budget=budget(1))
    assert len(conversation_service.list_conversations(agent="antigravity")) == 2


def test_targeted_native_uuid_refreshes_same_source_without_history_walk(history, monkeypatch):
    source = _native_antigravity(history, "selected")
    _native_antigravity(history, "unrelated")
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    def reject_walk(*args, **kwargs):
        raise AssertionError("Targeted relay must not enumerate the host history")
    monkeypatch.setattr(service, "_iter_source_files", reject_walk)
    result = service.sync_conversation_for_relay("selected")
    assert result[0]["status"] == "ok"
    conversations = conversation_service.list_conversations(agent="antigravity")
    assert len(conversations) == 1
    conversation_id = conversations[0]["id"]
    with source.open("a") as handle:
        handle.write('{"type":"PLANNER_RESPONSE","step_index":2,"content":"Latest answer"}\n')
    refreshed = service.sync_conversation_for_relay(
        conversation_id, agent="antigravity", source_path=str(source))
    assert refreshed[0]["status"] == "ok"
    updated = conversation_service.get_conversation(conversation_id)
    assert updated["messages"][-1]["content"] == "Latest answer"
    assert updated["cwd"] in ("", None)
    assert updated["project_id"] is None
    assert service.sync_conversation_for_relay("selected")[0]["reason"] == "source_unchanged"


def test_targeted_refresh_detects_changed_companion_of_unchanged_indexed_source(history, monkeypatch):
    full = _native_antigravity(history, "selected")
    normal = full.with_name("transcript.jsonl")
    normal.write_text(full.read_text())
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    service.sync_conversation_for_relay("selected")
    with normal.open("a") as handle:
        handle.write('{"type":"PLANNER_RESPONSE","step_index":2,"content":"Fresh companion"}\n')
    result = service.sync_conversation_for_relay("antigravity:selected", agent="antigravity", source_path=str(full))
    assert result[0]["status"] == "ok"
    conversation = conversation_service.list_conversations(agent="antigravity")[0]
    assert conversation_service.get_conversation(conversation["id"])["messages"][-1]["content"] == "Fresh companion"


def test_targeted_refresh_rejects_outside_root_and_preserves_tombstone(history, tmp_path):
    source = _native_antigravity(history, "selected")
    outside = _native_antigravity(tmp_path / "outside", "forbidden")
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    result = service.sync_conversation_for_relay("forbidden", agent="antigravity", source_path=str(outside))
    assert result[0]["status"] == "error"
    assert conversation_service.list_conversations(agent="antigravity") == []
    service.sync_conversation_for_relay("selected")
    conversation = conversation_service.list_conversations(agent="antigravity")[0]
    conversation_service.delete_conversation(conversation["id"])
    with source.open("a") as handle:
        handle.write('{"type":"PLANNER_RESPONSE","step_index":2,"content":"Must not resurrect"}\n')
    service.sync_conversation_for_relay("selected")
    assert conversation_service.list_conversations(agent="antigravity") == []


def test_targeted_refresh_keeps_surface_ambiguity_and_respects_pause(history):
    for surface in ("antigravity", "antigravity-cli"):
        _native_antigravity(history / surface / "brain", "same-id")
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    service.sync_conversation_for_relay("same-id")
    assert {item["external_id"] for item in conversation_service.list_conversations(agent="antigravity")} == {
        "antigravity:same-id", "antigravity:cli:same-id",
    }
    service.patch_connection("antigravity", paused=True, confirmed=True)
    assert service.sync_conversation_for_relay("same-id")[0]["reason"] == "connection_inactive"


def test_targeted_refresh_does_not_wait_behind_active_history_sync(history):
    _native_antigravity(history, "selected")
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    lock = service._sync_locks.setdefault("antigravity", threading.Lock())
    with lock:
        assert service.sync_conversation_for_relay("selected")[0]["reason"] == "sync_in_progress"


def test_targeted_refresh_keeps_last_committed_dialogue_when_full_is_moving(history, monkeypatch):
    full = _native_antigravity(history, "selected")
    normal = full.with_name("transcript.jsonl")
    normal.write_text(full.read_text())
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    service.sync_conversation_for_relay("selected")
    with full.open("a") as handle:
        handle.write('{"type":"PLANNER_RESPONSE","step_index":2,"content":"Full response"}\n')
    import app.adapters
    original = app.adapters.read_sessions
    def moving(*args, **kwargs):
        sessions = original(*args, **kwargs)
        with full.open("a") as handle:
            handle.write('{"type":"PLANNER_RESPONSE","step_index":3,"content":"Final response"}\n')
        return sessions
    monkeypatch.setattr(app.adapters, "read_sessions", moving)
    assert service.sync_conversation_for_relay("selected")[0]["status"] == "partial"
    conversation = conversation_service.list_conversations(agent="antigravity")[0]
    assert len(conversation_service.get_conversation(conversation["id"])["messages"]) == 1
    monkeypatch.setattr(app.adapters, "read_sessions", original)
    service.sync_conversation_for_relay("selected")
    assert conversation_service.get_conversation(conversation["id"])["messages"][-1]["content"] == "Final response"


def test_targeted_refresh_accepts_complete_prefix_without_touching_native_file(history):
    source = _native_antigravity(history, "selected")
    with source.open("ab") as handle:
        handle.write(b'{"type":"PLANNER_RESPONSE","step_index":2,"content":"partial')
    before = source.read_bytes()
    service.patch_connection("antigravity", paths=[str(source)], enabled=True, confirmed=True)
    result = service.sync_conversation_for_relay("selected")
    assert result[0]["status"] == "ok"
    assert source.read_bytes() == before
    conversation = conversation_service.list_conversations(agent="antigravity")[0]
    assert len(conversation_service.get_conversation(conversation["id"])["messages"]) == 1
    with source.open("ab") as handle:
        handle.write(b' completed"}\n')
    service.sync_conversation_for_relay("selected")
    assert conversation_service.get_conversation(conversation["id"])["messages"][-1]["content"] == "partial completed"


def test_targeted_exact_file_grant_does_not_import_ungranted_companion(history):
    source = _native_antigravity(history, "selected")
    normal = source.with_name("transcript.jsonl")
    normal.write_text(source.read_text() + '{"type":"PLANNER_RESPONSE","step_index":2,"content":"Not granted"}\n')
    service.patch_connection("antigravity", paths=[str(source)], enabled=True, confirmed=True)
    service.sync_conversation_for_relay("selected", agent="antigravity", source_path=str(source))
    conversation = conversation_service.list_conversations(agent="antigravity")[0]
    assert len(conversation_service.get_conversation(conversation["id"])["messages"]) == 1


def test_targeted_refresh_retries_failed_snapshot_before_claiming_freshness(history, monkeypatch):
    source = _native_antigravity(history, "selected")
    service.patch_connection("antigravity", paths=[str(source)], enabled=True, confirmed=True)
    service.sync_conversation_for_relay("selected")
    with source.open("a") as handle:
        handle.write('{"type":"PLANNER_RESPONSE","step_index":2,"content":"Fresh after retry"}\n')
    import app.adapters
    from app.adapters import AgentAdapterError
    original = app.adapters.read_sessions
    def unavailable(*args, **kwargs):
        raise AgentAdapterError(host="antigravity", code="file_changed", path=str(source), detail="Retry stable source")
    monkeypatch.setattr(app.adapters, "read_sessions", unavailable)
    assert service.sync_conversation_for_relay("selected")[0]["status"] == "partial"
    monkeypatch.setattr(app.adapters, "read_sessions", original)
    assert service.sync_conversation_for_relay("selected")[0]["status"] == "ok"
    conversation = conversation_service.list_conversations(agent="antigravity")[0]
    assert conversation_service.get_conversation(conversation["id"])["messages"][-1]["content"] == "Fresh after retry"


def test_recent_relay_discovery_only_enqueues_background_work(history, monkeypatch):
    from app.services import history_watcher
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    requested = []
    def request():
        requested.append(True)
        return True
    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not synchronously scan or execute native hosts")
    monkeypatch.setattr(history_watcher, "request_history_reconcile", request)
    monkeypatch.setattr(service, "_iter_source_files", forbidden)
    monkeypatch.setattr(service, "sync_connection", forbidden)
    lock = service._sync_locks.setdefault("antigravity", threading.Lock())
    with lock:
        statuses = service.sync_recent_connections_for_relay()
    assert requested == [True]
    assert next(item for item in statuses if item["agent"] == "antigravity") == {
        "agent": "antigravity", "status": "pending",
        "reason": "background_refresh_requested", "processed_sources": 0,
    }
    monkeypatch.setattr(history_watcher, "request_history_reconcile", lambda: False)
    assert all(item["status"] == "pending" and item["reason"] == "background_refresh_unavailable"
               for item in service.sync_recent_connections_for_relay())


def test_targeted_freshness_is_not_poisoned_by_unrelated_failed_source(history):
    source = _native_antigravity(history, "selected")
    service.patch_connection("antigravity", paths=[str(history)], enabled=True, confirmed=True)
    service.sync_conversation_for_relay("selected")
    selected = conversation_service.list_conversations(agent="antigravity")[0]
    missing_source = history / "old-missing" / ".system_generated" / "logs" / "transcript_full.jsonl"
    with db() as conn:
        service._write_source_cursor(conn, "antigravity", {
            "source_path": str(missing_source), "fingerprint": "old-missing-fingerprint",
            "file_identity": "[1,2]", "complete_offset": 0,
        }, error="permission_denied: unrelated source unavailable")
        conn.execute("UPDATE connections SET error='partial_sync: unrelated source unavailable' WHERE id='antigravity'")
    result = service.sync_conversation_for_relay(
        selected["id"], agent="antigravity", source_path=str(source))[0]
    assert result["status"] == "ok"
    assert result["target_observed"] is True
    assert result["error"] is None
    assert result["connection_error"]
    assert service.get_connection("antigravity")["error"]
    # The unchanged-source fast path must scope freshness the same way.
    unchanged = service.sync_conversation_for_relay("selected")[0]
    assert unchanged["status"] == "skipped"
    assert unchanged["reason"] == "source_unchanged"
    assert unchanged["error"] is None
    assert unchanged["connection_error"]
    with db() as conn:
        row = conn.execute(
            "SELECT status,error,fingerprint FROM source_sync_cursors WHERE host='antigravity' AND source_path=?",
            (str(missing_source),),
        ).fetchone()
        assert row["status"] == "error"
        assert row["error"] == "permission_denied: unrelated source unavailable"
        assert row["fingerprint"] == "old-missing-fingerprint"
