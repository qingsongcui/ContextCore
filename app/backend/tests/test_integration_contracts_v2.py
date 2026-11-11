from pathlib import Path

import pytest


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "data.db"))
    from app.database import create_schema, seed_data, db
    from app.services.derivation_service import ensure_schema
    create_schema()
    seed_data()
    with db() as conn:
        ensure_schema(conn)
    return tmp_path


def test_worktrees_share_project_without_mixing_working_directory(isolated):
    from app.services.project_service import reconstruct_or_resolve_project, repository_identity
    from app.database import db
    repo = isolated / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    p1 = reconstruct_or_resolve_project(str(repo))
    worktree = isolated / "feature"
    worktree.mkdir()
    gitdir = repo / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..")
    (worktree / ".git").write_text(f"gitdir: {gitdir}")
    assert repository_identity(str(worktree))["surface"] == "worktree"
    assert reconstruct_or_resolve_project(str(worktree)) == p1
    with db() as conn:
        paths = {r[0] for r in conn.execute("SELECT path FROM project_directories WHERE project_id=?", (p1,))}
    assert str(repo) in paths and str(worktree) in paths


def test_source_delete_does_not_delete_independent_memory(isolated):
    from app.services import conversation_service, content_service
    from app.database import db
    conversation = conversation_service.create_conversation(agent="omp", title="Synthetic", messages=[{"role": "user", "content": "synthetic source"}])
    memory = content_service.create_content(title="retained fact", content="independent memory", source_id=conversation["id"], source_kind="conversation", quote="synthetic source")
    conversation_service.delete_conversation(conversation["id"])
    assert content_service.get_content(memory["id"])["content"] == "independent memory"
    with db() as conn:
        source = conn.execute("SELECT deleted,quote FROM content_sources WHERE content_id=?", (memory["id"],)).fetchone()
        assert source["deleted"] == 1 and source["quote"] is None
        assert conn.execute("SELECT count(*) FROM derivation_cleanup WHERE entity_id=? AND operation='delete'", (conversation["id"],)).fetchone()[0] == 1


def test_compaction_advances_context_epoch_but_native_replay_is_stable(isolated):
    from app.services.context_epoch_service import epoch_for_record
    start = {"event": "SessionStart", "session_id": "synthetic", "event_id": "start-1", "metadata": {"source": "startup"}}
    compact = {"event": "SessionStart", "session_id": "synthetic", "event_id": "compact-1", "metadata": {"source": "compact"}}
    consumer, before = epoch_for_record("claude-code", start)
    assert epoch_for_record("claude-code", start) == (consumer, before)
    _, after = epoch_for_record("claude-code", compact)
    assert before != after
    assert epoch_for_record("claude-code", compact) == (consumer, after)
    assert epoch_for_record("claude-code", start) == (consumer, after)
    assert epoch_for_record("claude-code", {"event": "UserPromptSubmit", "session_id": "synthetic"}) == (consumer, after)


def test_authorized_filesystem_change_wakes_reconcile(isolated):
    import asyncio
    from app.database import db, encode
    from app.services.history_watcher import authorized_watch_roots, watch_authorized_histories
    root = isolated / "history"
    root.mkdir()
    with db() as conn:
        conn.execute("UPDATE connections SET enabled=1,paths=? WHERE id='codex'", (encode([str(root)]),))
    assert authorized_watch_roots() == (str(root.resolve()),)

    async def exercise():
        event = asyncio.Event()
        watcher = asyncio.create_task(watch_authorized_histories(event))
        try:
            for number in range(25):
                (root / "synthetic.jsonl").write_text('{"synthetic":' + str(number) + '}\n')
                try:
                    await asyncio.wait_for(event.wait(), .2)
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                raise AssertionError("Authorized filesystem events did not wake reconciliation")
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
    asyncio.run(exercise())
    with db() as conn:
        conn.execute("UPDATE connections SET paused=1 WHERE id='codex'")
    assert authorized_watch_roots() == ()


def test_explicit_reinstall_reuses_verified_credential_and_rejects_drift(isolated):
    from fastapi import HTTPException
    from app.services.connection_service import install_connection
    home = isolated / "synthetic-home"
    home.mkdir()
    install_connection("codex", confirmed=True, home=home)
    credential = home / ".contextcore" / "credentials" / "codex.token"
    original = credential.read_bytes()
    install_connection("codex", confirmed=True, home=home)
    assert credential.read_bytes() == original
    credential.write_text("synthetic-untrusted-replacement")
    with pytest.raises(HTTPException) as error:
        install_connection("codex", confirmed=True, home=home)
    assert error.value.status_code == 409
    assert credential.read_text() == "synthetic-untrusted-replacement"
