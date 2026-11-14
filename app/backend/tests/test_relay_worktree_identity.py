import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.database import create_schema, db, decode, encode, seed_data
from app.routers.conversations import router as conversations_router
from app.services import conversation_service, handoff_service, project_service


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=test@example.com", "-c", "user.name=Test",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "relay.sqlite"))
    create_schema()
    seed_data()
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "f.txt").write_text("a\n", encoding="utf-8")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-m", "init")
    worktree = tmp_path / "feature"
    _git(root, "worktree", "add", "-b", "feature", str(worktree))
    project_id = project_service.reconstruct_or_resolve_project(str(root))
    assert project_service.reconstruct_or_resolve_project(str(worktree)) == project_id
    return {"root": root, "worktree": worktree, "project_id": project_id}


def _conversation(repo, folder, external_id, updated_at="2026-11-12T00:00:00Z"):
    return conversation_service.upsert_conversation(
        agent="cursor",
        project_id=repo["project_id"],
        external_id=external_id,
        title="Same opening",
        cwd=str(folder),
        messages=[{"role": "user", "content": "检查是否可能存在这些问题，开头一百二十字完全相同。"}],
        started_at="2026-11-12T00:00:00Z",
        updated_at=updated_at,
    )


def test_worktrees_stay_separate_and_selector_shows_directory_and_branch(repo):
    main = _conversation(repo, repo["root"], "main-session")
    feature = _conversation(repo, repo["worktree"], "feature-session", "2026-11-12T01:00:00Z")
    duplicate = _conversation(repo, repo["root"], "main-duplicate", "2026-11-12T02:00:00Z")
    assert main["git_branch"] == "main"
    assert feature["git_branch"] == "feature"
    assert main["git_head"] and feature["git_head"]

    discovered = handoff_service.discover_relay_conversations(
        consumer="codex", limit=10, sync_first=False, link_unlinked=False, language="zh",
        project_id=repo["project_id"],
    )
    ids = [item["conversation_id"] for item in discovered["candidates"]]
    assert feature["id"] in ids
    assert duplicate["id"] in ids
    # Identical opening text is not a duplicate native session.
    assert main["id"] in ids
    assert len(ids) == 3
    prompt = discovered["question_prompt"]
    assert f"目录: {repo['root']}" in prompt
    assert f"目录: {repo['worktree']}" in prompt
    assert "分支: main" in prompt
    assert "分支: feature" in prompt


def test_loaded_working_directory_is_the_selected_conversation(repo):
    feature = _conversation(repo, repo["worktree"], "feature-session")
    loaded = handoff_service.relay_conversation(
        conversation_id=feature["id"], consumer="codex", sync_first=False, language="zh",
        cwd=str(repo["worktree"]),
    )
    work_lines = [line for line in loaded["context_text"].splitlines() if line.startswith("- 工作目录:")]
    assert work_lines == [f"- 工作目录: {repo['worktree']}"]
    assert str(repo["root"]) not in work_lines[0]
    assert "不是这个会话的目录" in loaded["context_text"]
    assert loaded["project"]["working_directory"] == str(repo["worktree"])


def test_handoff_rejects_commit_and_uncommitted_changes(repo):
    main = _conversation(repo, repo["root"], "main-session")
    card = handoff_service.create_handoff(
        project_id=repo["project_id"], conversation_id=main["id"], target_agent="codex", source_agent="cursor",
    )
    state = card["source_revisions"][main["id"]]["code_state"]
    assert state["kind"] == "git" and state["complete"] is True and state["dirty"] is False
    assert state["branch"] == "main"
    assert "代码状态:" in card["card"]["evidence"]
    assert "工作区干净" in card["card"]["evidence"]

    (repo["root"] / "f.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(HTTPException) as dirty:
        handoff_service.confirm_handoff(card["id"], True, actor="codex")
    assert dirty.value.status_code == 409
    assert "drifted" in dirty.value.detail

    (repo["root"] / "f.txt").write_text("a\n", encoding="utf-8")
    handoff_service.confirm_handoff(card["id"], True, actor="codex")

    (repo["root"] / "f.txt").write_text("next\n", encoding="utf-8")
    _git(repo["root"], "add", "f.txt")
    _git(repo["root"], "commit", "-m", "next")
    with pytest.raises(HTTPException) as committed:
        handoff_service.confirm_handoff(card["id"], True, actor="codex")
    assert committed.value.status_code == 409
    assert "drifted" in committed.value.detail


def test_legacy_card_without_code_state_cannot_confirm_a_git_checkout(repo):
    main = _conversation(repo, repo["root"], "main-session")
    card = handoff_service.create_handoff(
        project_id=repo["project_id"], conversation_id=main["id"], target_agent="codex", source_agent="cursor",
    )
    with db() as conn:
        revisions = decode(conn.execute("SELECT source_revisions FROM handoffs WHERE id = ?", (card["id"],)).fetchone()["source_revisions"], {})
        revisions[main["id"]].pop("code_state")
        conn.execute("UPDATE handoffs SET source_revisions = ? WHERE id = ?", (encode(revisions), card["id"]))
    with pytest.raises(HTTPException) as missing:
        handoff_service.confirm_handoff(card["id"], True, actor="codex")
    assert missing.value.status_code == 409
    assert "drifted" in missing.value.detail


def test_unchanged_resync_preserves_recorded_branch_and_head(repo):
    main = _conversation(repo, repo["root"], "main-session")
    original_head = main["git_head"]
    assert main["git_branch"] == "main" and original_head

    _git(repo["root"], "checkout", "-b", "switched")
    (repo["root"] / "f.txt").write_text("switched\n", encoding="utf-8")
    _git(repo["root"], "add", "f.txt")
    _git(repo["root"], "commit", "-m", "on switched")

    resynced = _conversation(repo, repo["root"], "main-session")
    assert resynced["_changed"] is False
    assert resynced["git_branch"] == "main"
    assert resynced["git_head"] == original_head

    with db() as conn:
        conn.execute("UPDATE conversations SET git_branch = NULL, git_head = NULL WHERE id = ?", (main["id"],))
    backfilled = _conversation(repo, repo["root"], "main-session")
    assert backfilled["git_branch"] == "switched"
    assert backfilled["git_head"] and backfilled["git_head"] != original_head

    _git(repo["root"], "checkout", "-b", "appended")
    appended = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=repo["project_id"],
        external_id="main-session",
        title="Same opening",
        cwd=str(repo["root"]),
        messages=[
            {"role": "user", "content": "检查是否可能存在这些问题，开头一百二十字完全相同。"},
            {"role": "assistant", "content": "已补充新回复。"},
        ],
        started_at="2026-11-12T00:00:00Z",
        updated_at="2026-11-12T04:00:00Z",
    )
    assert appended["_changed"] is True
    assert appended["git_branch"] == "appended"


def test_detached_head_and_legacy_branch_labels_do_not_misattribute_live_branch(repo):
    _git(repo["root"], "checkout", "--detach", "HEAD")
    detached = _conversation(repo, repo["root"], "detached-session", "2026-11-12T01:00:00Z")
    assert detached["git_branch"] is None
    assert detached["git_head"]

    _git(repo["root"], "checkout", "main")
    legacy = _conversation(repo, repo["root"], "legacy-session", "2026-11-12T02:00:00Z")
    with db() as conn:
        conn.execute(
            "UPDATE conversations SET title = ?, git_branch = NULL, git_head = NULL WHERE id = ?",
            ("Legacy opening", legacy["id"]),
        )

    zh = handoff_service.discover_relay_conversations(
        consumer="codex", limit=10, sync_first=False, link_unlinked=False, language="zh",
        project_id=repo["project_id"],
    )
    by_id_zh = {item["conversation_id"]: item for item in zh["candidates"]}
    assert by_id_zh[detached["id"]]["git_branch"] == f"HEAD@{detached['git_head'][:12]}"
    assert by_id_zh[legacy["id"]]["git_branch"] == "main（当前目录）"

    en = handoff_service.discover_relay_conversations(
        consumer="codex", limit=10, sync_first=False, link_unlinked=False, language="en",
        project_id=repo["project_id"],
    )
    by_id_en = {item["conversation_id"]: item for item in en["candidates"]}
    assert by_id_en[detached["id"]]["git_branch"] == f"HEAD@{detached['git_head'][:12]}"
    assert by_id_en[legacy["id"]]["git_branch"] == "main (current checkout)"


def test_handoff_detects_staged_index_drift_submodule_drift_and_incomplete_code_state(repo, tmp_path):
    main = _conversation(repo, repo["root"], "main-session")
    clean_card = handoff_service.create_handoff(
        project_id=repo["project_id"], conversation_id=main["id"], target_agent="codex", source_agent="cursor",
    )

    # 1. Staged index change reverted in working tree (worktree matches HEAD, index differs)
    (repo["root"] / "f.txt").write_text("staged-only\n", encoding="utf-8")
    _git(repo["root"], "add", "f.txt")
    (repo["root"] / "f.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(HTTPException) as staged_only:
        handoff_service.confirm_handoff(clean_card["id"], True, actor="codex")
    assert staged_only.value.status_code == 409
    assert "drifted" in staged_only.value.detail
    _git(repo["root"], "reset", "--hard", "HEAD")

    # 2. Staging an already-dirty file without changing working-tree bytes
    (repo["root"] / "f.txt").write_text("dirty-bytes\n", encoding="utf-8")
    dirty_card = handoff_service.create_handoff(
        project_id=repo["project_id"], conversation_id=main["id"], target_agent="codex", source_agent="cursor",
    )
    _git(repo["root"], "add", "f.txt")
    with pytest.raises(HTTPException) as staged_shift:
        handoff_service.confirm_handoff(dirty_card["id"], True, actor="codex")
    assert staged_shift.value.status_code == 409
    assert "drifted" in staged_shift.value.detail
    _git(repo["root"], "reset", "--hard", "HEAD")

    # 3. Submodule gitlink commit drift between two non-HEAD commits
    sub_origin = tmp_path / "sub_origin"
    sub_origin.mkdir()
    _git(sub_origin, "init", "-b", "main")
    shas = []
    for label in ("v1", "v2", "v3"):
        (sub_origin / "s.txt").write_text(f"{label}\n", encoding="utf-8")
        _git(sub_origin, "add", "s.txt")
        _git(sub_origin, "commit", "-m", label)
        shas.append(subprocess.run(
            ["git", "-C", str(sub_origin), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip())
    _git(repo["root"], "-c", "protocol.file.allow=always", "submodule", "add", str(sub_origin), "sub")
    _git(repo["root"], "commit", "-m", "add submodule")
    sub_checkout = repo["root"] / "sub"
    _git(sub_checkout, "checkout", shas[0])
    sub_card = handoff_service.create_handoff(
        project_id=repo["project_id"], conversation_id=main["id"], target_agent="codex", source_agent="cursor",
    )
    _git(sub_checkout, "checkout", shas[1])
    with pytest.raises(HTTPException) as sub_drift:
        handoff_service.confirm_handoff(sub_card["id"], True, actor="codex")
    assert sub_drift.value.status_code == 409
    assert "drifted" in sub_drift.value.detail

    # 4. Unborn branch (complete=False) fails closed instead of silently allowing confirmation
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-b", "main")
    (unborn / "draft.txt").write_text("v1\n", encoding="utf-8")
    unborn_pid = project_service.reconstruct_or_resolve_project(str(unborn))
    unborn_conv = conversation_service.upsert_conversation(
        agent="cursor",
        project_id=unborn_pid,
        external_id="unborn-session",
        title="Unborn repo",
        cwd=str(unborn),
        messages=[{"role": "user", "content": "initial draft"}],
    )
    unborn_card = handoff_service.create_handoff(
        project_id=unborn_pid, conversation_id=unborn_conv["id"], target_agent="codex", source_agent="cursor",
    )
    assert unborn_card["source_revisions"][unborn_conv["id"]]["code_state"]["complete"] is False
    (unborn / "draft.txt").write_text("v2\n", encoding="utf-8")
    with pytest.raises(HTTPException) as incomplete:
        handoff_service.confirm_handoff(unborn_card["id"], True, actor="codex")
    assert incomplete.value.status_code == 409
    assert "drifted" in incomplete.value.detail


def test_conversation_index_and_api_expose_cwd_branch_and_head(repo):
    feature = _conversation(repo, repo["worktree"], "feature-api")
    indexed = conversation_service.conversation_index(project_id=repo["project_id"])["items"]
    assert indexed[0]["cwd"] == str(repo["worktree"])
    assert indexed[0]["git_branch"] == "feature"
    assert indexed[0]["git_head"] == feature["git_head"]

    summary_list = conversation_service.list_conversations(project_id=repo["project_id"], summary=True)
    assert summary_list[0]["git_branch"] == "feature"
    assert summary_list[0]["git_head"] == feature["git_head"]

    meta_only = conversation_service.get_conversation(feature["id"], include_messages=False)
    assert meta_only["git_branch"] == "feature"
    assert meta_only["git_head"] == feature["git_head"]

    app = FastAPI()
    app.include_router(conversations_router)
    client = TestClient(app)
    detail = client.get(f"/api/conversations/{feature['id']}").json()
    assert detail["cwd"] == str(repo["worktree"])
    assert detail["git_branch"] == "feature"
    assert detail["git_head"] == feature["git_head"]
    listed = client.get("/api/conversations", params={"project_id": repo["project_id"]}).json()
    assert listed[0]["cwd"] == str(repo["worktree"])
    assert listed[0]["git_branch"] == "feature"
    assert listed[0]["git_head"] == feature["git_head"]


def test_symlinked_cwd_sorting_dedup_and_loaded_branch_context(repo, tmp_path):
    link_dir = tmp_path / "feature-link"
    link_dir.symlink_to(repo["worktree"], target_is_directory=True)
    feature = _conversation(repo, link_dir, "feature-older", "2026-11-12T01:00:00Z")
    newer = _conversation(repo, repo["root"], "main-newer", "2026-11-12T03:00:00Z")

    discovered = handoff_service.discover_relay_conversations(
        consumer="codex", limit=10, sync_first=False, link_unlinked=False, language="zh",
        cwd=str(repo["worktree"]), project_id=repo["project_id"],
    )
    assert discovered["candidates"][0]["conversation_id"] == newer["id"]
    assert discovered["candidates"][1]["conversation_id"] == feature["id"]

    dir_lines = handoff_service._session_directory_lines(
        lambda zh, _en: zh,
        str(link_dir) + "/",
        [str(repo["worktree"]), str(repo["root"])],
    )
    assert dir_lines == [
        f"- 工作目录: {link_dir}/",
        f"- 项目其他已登记目录（不是这个会话的目录）: {repo['root']}",
    ]

    loaded = handoff_service.relay_conversation(
        conversation_id=feature["id"], consumer="cursor", sync_first=False, language="zh",
        cwd=str(link_dir),
    )
    assert loaded["selected_conversation"]["git_branch"] == "feature"
    assert loaded["selected_conversation"]["git_head"] == feature["git_head"]
    assert f"- 会话分支: feature @ {feature['git_head'][:12]}" in loaded["context_text"]
    assert "代码状态:" in loaded["card"]["evidence"]

