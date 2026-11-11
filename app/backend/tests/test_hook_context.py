from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def hook_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_hook_context.db")
        os.environ["CONTEXTCORE_DB"] = db_path

        from app.database import create_schema, seed_data, db
        from app.main import app

        create_schema()
        seed_data()

        # Seed valid tokens for all 4 agents
        tokens = {
            "codex": "test-token-codex",
            "omp": "test-token-omp",
            "cursor": "test-token-cursor",
            "antigravity": "test-token-antigravity",
        }
        with db() as conn:
            for agent, tok in tokens.items():
                tok_h = hashlib.sha256(tok.encode("utf-8")).hexdigest()
                conn.execute(
                    "UPDATE connections SET enabled = 1, paused = 0, token_hash = ? WHERE id = ?",
                    (tok_h, agent),
                )

        client = TestClient(
            app,
            base_url="http://127.0.0.1:8420",
            client=("127.0.0.1", 50000),
        )

        sess_res = client.get("/api/session")
        assert sess_res.status_code == 200
        csrf_token = sess_res.json()["csrf_token"]

        yield {
            "client": client,
            "csrf_token": csrf_token,
            "tokens": tokens,
            "tmpdir": tmpdir,
        }


def test_paused_collection_permits_shared_memory_read(hook_env):
    """
    Collection pause must not revoke shared context reads (bearer remains enabled).
    Turns ingestion is skipped, but memory reading remains active with id/version.
    """
    client = hook_env["client"]
    csrf_token = hook_env["csrf_token"]
    tokens = hook_env["tokens"]
    proj_dir = os.path.join(hook_env["tmpdir"], "paused_conn_repo")
    os.makedirs(proj_dir, exist_ok=True)

    from app.database import db

    p_res = client.post("/api/projects", json={"name": "Paused Connection Project", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token})
    assert p_res.status_code == 200
    proj_id = p_res.json()["id"]

    c_res = client.post(
        "/api/content",
        json={"title": "Important Architecture Note", "content": "Microservices with gRPC transport", "category": "knowledge", "kind": "memory", "project_id": proj_id},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert c_res.status_code == 200

    # Pause collection for 'omp'
    with db() as conn:
        conn.execute("UPDATE connections SET paused = 1 WHERE id = 'omp'")

    hook_payload = {
        "event": "before_agent_start",
        "sessionId": "omp-paused-sess",
        "cwd": proj_dir,
    }
    hook_headers = {"Authorization": f"Bearer {tokens['omp']}", "X-CSRF-Token": csrf_token}
    res = client.post("/api/connections/omp/hook", json=hook_payload, headers=hook_headers)
    assert res.status_code == 200
    data = res.json()

    # Ordinary lifecycle hook stays silent under D063
    assert data["ok"] is True
    assert data["processed"] == 0  # Ingestion skipped
    assert data["context_type"] == "none"
    assert "injection" not in data

    # While collection is paused, explicit MCP shared memory reading remains permitted (no token/source revocation)
    from app.security import authenticated_agent
    from app import mcp_server
    tok = authenticated_agent.set("omp")
    try:
        pack = mcp_server.assemble_context("Microservices", project_id=proj_id, max_tokens=4000)
        assert pack["status"] == "ready"
        assert len(pack["blocks"]) == 1
        assert "Microservices with gRPC transport" in pack["blocks"][0]["content"]
    finally:
        authenticated_agent.reset(tok)

def test_deleted_source_honest_provenance(hook_env):
    """
    Handle missing/deleted source snapshots honestly: provenance must state [已删除]
    instead of pretending the source is still present or crashing.
    """
    client = hook_env["client"]
    csrf_token = hook_env["csrf_token"]
    tokens = hook_env["tokens"]
    proj_dir = os.path.join(hook_env["tmpdir"], "deleted_source_repo")
    os.makedirs(proj_dir, exist_ok=True)

    from app.database import db, new_id, now_iso

    proj_res = client.post("/api/projects", json={"name": "Source Audit Project", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token})
    assert proj_res.status_code == 200
    proj_id = proj_res.json()["id"]

    # Insert a memory pointing to a deleted conversation
    with db() as conn:
        cid = new_id("cnt")
        now = now_iso()
        conn.execute(
            """
            INSERT INTO contents (id, project_id, kind, category, title, content, version, status, created_at, updated_at)
            VALUES (?, ?, 'memory', 'lesson', 'Deadlock Postmortem', 'Avoid nested locks in worker threads', 1, 'active', ?, ?)
            """,
            (cid, proj_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO content_sources (id, content_id, source_id, source_kind, source_title, source_agent, deleted, created_at)
            VALUES (?, ?, 'nonexistent_conv_id', 'conversation', 'Deadlock Debug Session', 'codex', 1, ?)
            """,
            (new_id("csrc"), cid, now),
        )

    hook_payload = {
        "event": "sessionStart",
        "session_id": "cursor-audit",
        "cwd": proj_dir,
        "context_request": True,
    }
    hook_headers = {"Authorization": f"Bearer {tokens['cursor']}", "X-CSRF-Token": csrf_token}
    res = client.post("/api/connections/cursor/hook", json=hook_payload, headers=hook_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["context_type"] == "none"
    assert not {"injection", "card", "handoff_id"}.intersection(data)
    from app import mcp_server
    from app.security import authenticated_agent
    identity = authenticated_agent.set("cursor")
    try:
        memory = mcp_server.read_memory(cid)
    finally:
        authenticated_agent.reset(identity)
    assert memory["sources"][0]["deleted"] is True
    assert memory["content"] == "Avoid nested locks in worker threads"

    # Verify no injection was recorded in context_injections on the hook path
    with db() as conn:
        injections_count = conn.execute("SELECT COUNT(*) FROM context_injections").fetchone()[0]
        assert injections_count == 0


def test_lifecycle_event_does_not_persist_empty_or_overwrite_title(hook_env):
    """
    Do not persist empty lifecycle events as new tasks/conversations.
    Do not overwrite meaningful conversation title with generic hook titles.
    """
    client = hook_env["client"]
    csrf_token = hook_env["csrf_token"]
    tokens = hook_env["tokens"]
    proj_dir = os.path.join(hook_env["tmpdir"], "lifecycle_repo")
    os.makedirs(proj_dir, exist_ok=True)

    p_res = client.post("/api/projects", json={"name": "Lifecycle Repo", "directories": [proj_dir]}, headers={"X-CSRF-Token": csrf_token})
    assert p_res.status_code == 200

    # 1. Empty lifecycle event on clean session
    empty_payload = {
        "event": "sessionStart",
        "conversation_id": "cursor-empty-999",
        "cwd": proj_dir,
    }
    hook_headers = {"Authorization": f"Bearer {tokens['cursor']}", "X-CSRF-Token": csrf_token}
    res = client.post("/api/connections/cursor/hook", json=empty_payload, headers=hook_headers)
    assert res.status_code == 200
    assert res.json()["processed"] == 0

    # Ensure no conversation created
    from app.services import conversation_service
    convs = conversation_service.list_conversations(agent="cursor")
    assert not any(c["external_id"] == "cursor:cursor-empty-999" for c in convs)

    # 2. Existing conversation with meaningful title
    meaningful = conversation_service.upsert_conversation(
        agent="codex",
        title="Refactoring Core AST Engine",
        external_id="codex:sess-meaningful",
        messages=[{"id": "m1", "role": "user", "content": "Refactor AST now"}],
        cwd=proj_dir,
    )
    assert meaningful["title"] == "Refactoring Core AST Engine"

    # Inbound hook arrives with generic title
    generic_payload = {
        "event": "UserPromptSubmit",
        "session_id": "sess-meaningful",
        "title": "codex Event",
        "cwd": proj_dir,
        "prompt": "Incremental addition",
    }
    codex_headers = {"Authorization": f"Bearer {tokens['codex']}", "X-CSRF-Token": csrf_token}
    hook_res = client.post("/api/connections/codex/hook", json=generic_payload, headers=codex_headers)
    assert hook_res.status_code == 200

    # Title must remain the meaningful title
    after_conv = conversation_service.get_conversation(meaningful["id"])
    assert after_conv["title"] == "Refactoring Core AST Engine"


def test_cc_hook_format_host_response_and_offline():
    """Lifecycle hooks never inject context or outage notices into chat."""
    from app.integrations.cc_hook import format_host_response
    for host, events in {
        "codex": ["session-start", "prompt", "stop"],
        "claude-code": ["SessionStart", "UserPromptSubmit"],
        "cursor": ["sessionStart", "beforeSubmitPrompt"],
        "antigravity": ["pre-invoke", "SessionStart"],
    }.items():
        for event in events:
            for response in (None, {}, {"injection": "private context", "card": "handoff"}):
                expected = {"continue": True} if host == "cursor" and event == "beforeSubmitPrompt" else {}
                assert format_host_response(host, event, response) == expected


def test_cc_hook_subprocess_cli():
    """
    Test cc_hook.py end-to-end via CLI subprocess pipe adhering to host invocation.
    Isolate HOME and use explicit unreachable loopback port so real credentials are not loaded.
    """
    hook_script = Path(__file__).parent.parent / "app" / "integrations" / "cc_hook.py"
    assert hook_script.is_file()

    with tempfile.TemporaryDirectory() as empty_home:
        env = {
            **os.environ,
            "HOME": empty_home,
            "CONTEXTCORE_BASE_URL": "http://127.0.0.1:65534",
            "CONTEXTCORE_TOKEN": "mock-test-token",
        }
        # Call with codex session-start with stdin simulating offline server (explicit unreachable loopback)
        proc = subprocess.run(
            [sys.executable, str(hook_script), "codex", "session-start"],
            input=json.dumps({"session_id": "test-cli-sess", "cwd": "/tmp"}),
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        assert proc.returncode == 0
        # Server downtime remains silent in the conversation.
        assert json.loads(proc.stdout.strip()) == {}
        assert not proc.stderr.strip()


def test_hook_control_flags_never_inject_or_confirm_while_capture_and_checkpoint_continue(hook_env):
    from app.database import db
    from app.services import content_service, conversation_service, handoff_service, project_service

    client = hook_env["client"]
    directory = os.path.join(hook_env["tmpdir"], "silent-capture")
    os.makedirs(directory)
    project = project_service.create_project(name="Silent capture", directories=[directory])
    task = content_service.create_content(
        project_id=project["id"], category="task", title="Selected synthetic task",
        content="Do not execute before confirmation", agent="omp",
    )
    content_service.create_content(
        project_id=project["id"], category="knowledge", title="Private context",
        content="PRIVATE_CONTEXT_MUST_NOT_APPEAR_IN_HOOK_RESPONSE", agent="omp",
    )
    pending = handoff_service.create_handoff(
        project_id=project["id"], task_id=task["id"], target_agent="codex", source_agent="omp",
    )
    attacks = [
        {"context_request": True},
        {"metadata": {"context_request": True}},
        {"event": "contextcore_command"},
        {"event": "contextcore"},
        {"action": "status"},
        {"action": "handoff", "actor": "codex", "confirmed": True},
        {"metadata": {"raw_event": "contextcore_command"}, "prompt": "接着做；我习惯使用隔离数据库。"},
    ]
    headers = {"Authorization": f"Bearer {hook_env['tokens']['codex']}"}
    for index, overrides in enumerate(attacks):
        prompt = overrides.get("prompt", "我习惯使用隔离数据库。")
        response = client.post("/api/connections/codex/hook", headers=headers, json={
            "event": "UserPromptSubmit", "session_id": f"silent-capture-{index}", "cwd": directory,
            "timestamp": "2026-11-04T00:00:00+00:00", "prompt": prompt,
            "messages": [{"id": f"u-{index}", "role": "user", "content": prompt,
                          "timestamp": "2026-11-04T00:00:00+00:00"}],
            **overrides,
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["ok"] is True and result["processed"] == 1
        assert result["context_type"] == "none"
        assert not {"injection", "card", "handoff_id"}.intersection(result)
        assert "PRIVATE_CONTEXT_MUST_NOT_APPEAR_IN_HOOK_RESPONSE" not in response.text
        receipt = result["auto_memory"]
        assert not {"saved_memories", "memories", "process_summary", "summary_note"}.intersection(receipt)
        captured = conversation_service.get_conversation(receipt["conversation_id"])
        assert any(message["role"] == "user" and message["content"] == prompt for message in captured["messages"])
        assert handoff_service.get_handoff(pending["id"])["status"] == "awaiting_confirmation"
    assert content_service.list_content(project_id=project["id"], category="preference", status="active")
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_logs WHERE action='handoff.confirm'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM context_injections").fetchone()[0] == 0
