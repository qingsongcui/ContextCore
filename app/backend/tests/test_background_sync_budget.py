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
