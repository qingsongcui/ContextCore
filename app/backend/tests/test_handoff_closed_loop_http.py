"""Isolated HTTP/MCP closed loop: interrupt → card → confirm → continuation.

Uses a throwaway data directory and synthetic host tokens. Does not touch
the production 8420 service, launchd, or paid model calls.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

from app.integrations.cc_hook import format_host_response
from app.integrations.constants import SUPPORTED_HOSTS


def _utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _rpc(client, headers, seq, method, params):
    response = client.post(
        "/mcp/",
        headers=headers,
        json={"jsonrpc": "2.0", "id": seq, "method": method, "params": params},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "error" not in payload, payload
    return payload["result"]


def _call(client, headers, seq, name, arguments, expect_error=False):
    result = _rpc(client, headers, seq, "tools/call", {"name": name, "arguments": arguments})
    if expect_error:
        assert result.get("isError") is True, result
        return result
    assert not result.get("isError"), result
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = [item["text"] for item in result.get("content", []) if item.get("type") == "text"]
    assert texts, result
    return json.loads(texts[0])


def test_http_mcp_confirm_then_continue_without_compute(tmp_path, monkeypatch):
    data = tmp_path / "data"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CONTEXTCORE_DB", str(data / "contextcore.db"))
    from app.database import create_schema, seed_data, db

    create_schema()
    seed_data()
    tokens = {agent: f"synthetic-http-closed-loop-{agent}" for agent in SUPPORTED_HOSTS}
    with db() as conn:
        for agent_id, token in tokens.items():
            conn.execute(
                "UPDATE connections SET enabled=1, paused=0, token_hash=? WHERE id=?",
                (hashlib.sha256(token.encode()).hexdigest(), agent_id),
            )

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = {**os.environ, "HOME": str(home), "CONTEXTCORE_DATA_DIR": str(data)}
    env.update({
        "CODEX_HOME": str(home / ".codex"), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
        **{
            "SENRELAY_" + agent.upper().replace("-", "_") + "_ROOTS": str(home / "history" / agent)
            for agent in SUPPORTED_HOSTS
        },
    })
    log_path = tmp_path / "server.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.cli",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--data-dir",
                str(data),
            ],
            cwd=Path(__file__).parents[1],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError(f"Isolated HTTP service failed to start\n{log_path.read_text()}")

                csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
                settings = client.get("/api/settings").json()
                assert settings.get("compute_authorized") is False

                scene_dir = str(tmp_path / "selected-project")
                project = client.post("/api/projects", headers=csrf, json={
                    "name": "Closed Loop HTTP", "directories": [scene_dir],
                }).json()
                project_id = project["id"]

                old = client.post(
                    "/api/content",
                    headers=csrf,
                    json={
                        "project_id": project_id,
                        "title": "Auth token",
                        "category": "decision",
                        "content": "Use session cookies.",
                        "agent": "cursor",
                    },
                ).json()
                new = client.post(
                    "/api/content",
                    headers=csrf,
                    json={
                        "project_id": project_id,
                        "title": "Auth token",
                        "category": "decision",
                        "content": "Use JWT access tokens.",
                        "agent": "cursor",
                        "supersedes_id": old["id"],
                    },
                ).json()
                auth = client.post(
                    "/api/content",
                    headers=csrf,
                    json={
                        "project_id": project_id,
                        "title": "Auth engine",
                        "category": "task",
                        "content": "Wire JWT in auth/jwt.py after the parser merge.",
                        "agent": "cursor",
                        "tags": ["status:in_progress"],
                    },
                ).json()
                ui = client.post(
                    "/api/content",
                    headers=csrf,
                    json={
                        "project_id": project_id,
                        "title": "Settings UI",
                        "category": "task",
                        "content": "Unrelated settings page.",
                        "agent": "cursor",
                        "tags": ["status:pending"],
                    },
                ).json()
                parser = client.post(
                    "/api/content",
                    headers=csrf,
                    json={
                        "project_id": project_id,
                        "title": "Parser",
                        "category": "task",
                        "content": "Parser already merged.",
                        "agent": "cursor",
                        "tags": ["status:completed"],
                    },
                ).json()
                conv_response = client.post(
                    "/api/conversations",
                    headers=csrf,
                    json={
                        "project_id": project_id,
                        "agent": "cursor",
                        "title": "Auth engine interrupted",
                        "source_path": scene_dir + "/captured.jsonl",
                        "process_summary": "",
                        "messages": [
                            {
                                "id": "m-user",
                                "role": "user",
                                "timestamp": _utc(),
                                "content": "Implement auth engine; do not rewrite the parser. Token issuer URL 尚未确定.",
                            },
                            {
                                "id": "m-assistant",
                                "role": "assistant",
                                "timestamp": _utc(),
                                "content": "Parser already merged. Next: wire JWT in auth/jwt.py:42. verification artifact: artifacts/auth-failure.log. 阻塞: token issuer URL 尚未确定. 下一步应先确认 issuer --tenant selected.",
                            },
                        ],
                    },
                )
                assert conv_response.status_code == 200, conv_response.text
                conv = conv_response.json()
                assert not conv.get("process_summary")

                historical_constraint = client.post("/api/content", headers=csrf, json={
                    "project_id": project_id, "title": "Debugging constraint", "category": "preference",
                    "content": "HISTORICAL_ONLY allow paid debugging.", "related_ids": [auth["id"]],
                }).json()
                current_constraint = client.post("/api/content", headers=csrf, json={
                    "project_id": project_id, "title": "Debugging constraint", "category": "preference",
                    "content": "CURRENT_ONLY no paid calls; preserve parser.", "related_ids": [auth["id"]],
                    "supersedes_id": historical_constraint["id"],
                }).json()
                parallel_response = client.post("/api/conversations", headers=csrf, json={
                    "project_id": project_id, "agent": "cursor", "title": "Newer parallel UI scene",
                    "messages": [{
                        "id": "parallel-user", "role": "user", "timestamp": _utc(),
                        "content": "PARALLEL_ONLY rewrite settings.tsx",
                    }],
                })
                assert parallel_response.status_code == 200, parallel_response.text
                parallel_conv = parallel_response.json()
                parallel_constraint = client.post("/api/content", headers=csrf, json={
                    "project_id": project_id, "title": "UI-only preference", "category": "preference",
                    "content": "PARALLEL_ONLY use paid UI debugger.", "related_ids": [ui["id"]],
                }).json()

                def mcp_headers(agent):
                    return {
                        "Authorization": f"Bearer {tokens[agent]}",
                        "Accept": "application/json, text/event-stream",
                    }

                seq = {"n": 0}

                def next_id():
                    seq["n"] += 1
                    return seq["n"]

                for agent in tokens:
                    init = _rpc(
                        client,
                        mcp_headers(agent),
                        next_id(),
                        "initialize",
                        {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": f"closed-loop-{agent}", "version": "1"},
                        },
                    )
                    assert init.get("serverInfo")

                ambiguous = _call(
                    client,
                    mcp_headers("codex"),
                    next_id(),
                    "start_handoff",
                    {"target_agent": "codex", "project_id": project_id, "query": "接着做"},
                )
                assert ambiguous["ok"] is False
                assert ambiguous["code"] == "ambiguous_task"
                titles = {item["title"] for item in ambiguous["candidates"]}
                assert "Auth engine" in titles
                assert "Settings UI" in titles
                assert "Parser" not in titles

                created = _call(
                    client,
                    mcp_headers("codex"),
                    next_id(),
                    "start_handoff",
                    {
                        "target_agent": "codex",
                        "project_id": project_id,
                        "task_id": auth["id"],
                        "conversation_id": conv["id"],
                        "source_agent": "cursor",
                    },
                )
                hid = created["handoff_id"]
                card = created["card"]
                assert set(card) == {"goal", "progress", "decisions", "evidence", "blockers", "next_steps"}
                assert "CURRENT_ONLY" in card["decisions"]
                assert "do not rewrite the parser" in card["decisions"]
                assert "HISTORICAL_ONLY" not in card["decisions"]
                assert "PARALLEL_ONLY" not in card["decisions"] + card["evidence"] + card["next_steps"]
                assert scene_dir in card["evidence"]
                assert "captured.jsonl" in card["evidence"]
                assert "artifacts/auth-failure.log" in card["evidence"]
                assert "--tenant selected" in card["next_steps"]
                assert current_constraint["id"] in created["source_ids"]
                assert parallel_conv["id"] not in created["source_ids"]
                assert created["continuation"]["source_revisions"][current_constraint["id"]]["version"] == 1
                readback = created["continuation"]["readback"]
                assert set(readback) == {"goal", "progress", "constraints", "scene", "blockers", "next"}
                assert readback["goal"] == card["goal"]
                assert "Parser" in readback["progress"]
                assert "do not rewrite the parser" in readback["constraints"]
                assert scene_dir in readback["scene"]
                assert "尚未确定" in readback["blockers"]
                assert "--tenant selected" in readback["next"]
                assert created["status"] == "awaiting_confirmation"
                assert created["must_confirm"] is True
                assert created["continuation"]["must_not_execute"] is True
                assert created["continuation"]["status"] == "awaiting_confirmation"
                assert "Auth engine" in card["goal"]
                assert "wire JWT" in card["next_steps"] or "issuer" in card["next_steps"]
                assert "尚未确定" in card["blockers"]
                assert "JWT access tokens" in card["decisions"]
                assert "session cookies" not in card["decisions"]
                assert "Settings UI" not in card["next_steps"]
                assert "已完成勿重做" in card["progress"]
                assert "Parser" in card["progress"]

                waiting = _call(
                    client,
                    mcp_headers("codex"),
                    next_id(),
                    "assemble_context",
                    {"task_query": "接着做", "project_id": project_id, "max_tokens": 32000},
                )
                assert waiting["trigger"] == "opt_in_mcp"
                assert waiting["continuation"]["status"] == "awaiting_confirmation"
                assert waiting["continuation"]["must_not_execute"] is True

                unconfirmed = _call(
                    client,
                    mcp_headers("codex"),
                    next_id(),
                    "read_handoff",
                    {"handoff_id": hid},
                )
                assert unconfirmed["status"] == "awaiting_confirmation"
                assert unconfirmed["must_not_execute"] is True
                assert "not confirmed" in json.dumps(unconfirmed).lower()

                denied = _call(
                    client,
                    mcp_headers("cursor"),
                    next_id(),
                    "confirm_handoff",
                    {"handoff_id": hid, "confirmed": True},
                )
                assert denied["ok"] is False

                confirmed = _call(
                    client,
                    mcp_headers("codex"),
                    next_id(),
                    "confirm_handoff",
                    {"handoff_id": hid, "confirmed": True},
                )
                continuation = confirmed["continuation"]
                assert confirmed["ok"] is True
                assert confirmed["status"] == "confirmed"
                assert continuation["status"] == "ready"
                assert continuation["must_not_execute"] is False
                assert continuation["selected_task_id"] == auth["id"]
                assert continuation["resume_from"]
                assert {item["id"] for item in continuation["parallel_open_tasks"]} == {ui["id"]}
                assert {item["id"] for item in continuation["do_not_redo"]} == {parser["id"]}
                assert any(item["id"] == new["id"] for item in continuation["active_decisions"])
                assert any(item["id"] == old["id"] for item in continuation["excluded_historical_decisions"])

                assembled = _call(
                    client,
                    mcp_headers("codex"),
                    next_id(),
                    "assemble_context",
                    {"task_query": "接着做", "project_id": project_id, "max_tokens": 32000},
                )
                assert assembled["trigger"] == "opt_in_mcp"
                assert assembled["continuation"]["status"] == "ready"
                assert assembled["continuation"]["selected_task_id"] == auth["id"]
                retrieved = json.dumps(assembled["blocks"], ensure_ascii=False)
                assert "JWT access tokens" in retrieved
                assert "session cookies" not in retrieved
                assert any(
                    item["id"] == old["id"]
                    for item in assembled["continuation"]["excluded_historical_decisions"]
                )
                assert parser["id"] not in {block["id"] for block in assembled["blocks"]}
                assert auth["id"] in {block["id"] for block in assembled["blocks"]}

                read = _call(
                    client,
                    mcp_headers("codex"),
                    next_id(),
                    "read_handoff",
                    {"handoff_id": hid},
                )
                assert read["continuation"]["selected_task_id"] == auth["id"]
                assert read["card"] == card
                assert assembled["continuation"]["card"] == card
                assert parallel_constraint["id"] not in {
                    item["id"] for item in assembled["continuation"]["active_decisions"]
                }
                assert assembled["continuation"]["resume_from"] == card["next_steps"]
                assert assembled["estimated_tokens"] <= assembled["budget_tokens"]

                too_small = _call(client, mcp_headers("codex"), next_id(), "assemble_context", {
                    "task_query": "接着做", "project_id": project_id, "max_tokens": 256,
                })
                assert too_small["status"] == "insufficient_budget"
                assert too_small["continuation"]["must_not_execute"] is True
                assert too_small["continuation"]["resume_from"] is None

                changed = client.patch(f"/api/content/{current_constraint['id']}", headers=csrf, json={
                    "content": "CURRENT_ONLY changed by the user.", "expected_version": 1,
                })
                assert changed.status_code == 200, changed.text
                _call(client, mcp_headers("codex"), next_id(), "read_handoff", {"handoff_id": hid}, expect_error=True)
                stale = _call(client, mcp_headers("codex"), next_id(), "assemble_context", {
                    "task_query": "接着做", "project_id": project_id, "max_tokens": 32000,
                })
                assert stale["continuation"]["status"] == "stale"
                assert stale["continuation"]["must_not_execute"] is True

                hook = client.post(
                    "/api/connections/codex/hook",
                    headers=mcp_headers("codex"),
                    json={"event": "session-start", "session_id": "closed-loop-http", "prompt": "接着做"},
                )
                assert hook.status_code == 200
                assert format_host_response("codex", "session-start", hook.json()) == {}
                assert format_host_response("cursor", "beforeSubmitPrompt", hook.json()) == {"continue": True}
                # Each credential now acts as the successor, reading and editing
                # the same independent memory. Versions cross identity boundaries;
                # an actor string in a request never supplies authentication.
                shared = _call(client, mcp_headers("codex"), next_id(), "save_memory", {
                    "project_id": project_id, "title": "Cross-Agent shared fact",
                    "content": "Initial shared value", "category": "knowledge",
                })
                shared_id = shared["id"]
                version = shared["version"]
                previous_value = "Initial shared value"
                ready_cards = {}
                for agent in tokens:
                    previous = _call(client, mcp_headers(agent), next_id(), "read_memory", {"id": shared_id})
                    assert previous["content"] == previous_value
                    assert previous["version"] == version
                    previous_value = f"Shared value written by authenticated {agent}"
                    edited = _call(client, mcp_headers(agent), next_id(), "edit_memory", {
                        "id": shared_id, "expected_version": version, "content": previous_value,
                    })
                    assert edited["version"] == version + 1
                    _call(client, mcp_headers(agent), next_id(), "edit_memory", {
                        "id": shared_id, "expected_version": version, "content": "Stale writer must lose",
                    }, expect_error=True)
                    version = edited["version"]
                    predecessor = "codex" if agent == "cursor" else "cursor"
                    source_response = client.post("/api/conversations", headers=csrf, json={
                        "project_id": project_id, "agent": predecessor,
                        "title": f"Unavailable predecessor for {agent}", "process_summary": "",
                        "messages": [
                            {"id": f"unavailable-user-{agent}", "timestamp": _utc(), "role": "user", "content": "Continue Auth engine; do not rewrite the parser."},
                            {"id": f"unavailable-assistant-{agent}", "timestamp": _utc(), "role": "assistant", "content": "Quota exhausted. 阻塞: issuer 未确定. 下一步: confirm issuer --tenant selected."},
                        ],
                    })
                    assert source_response.status_code == 200, source_response.text
                    source = source_response.json()
                    assert not source["process_summary"]
                    # Pausing collection does not revoke shared-memory rights.
                    paused = client.patch(f"/api/connections/{agent}", headers=csrf, json={
                        "paused": True, "confirmed": True,
                    })
                    assert paused.status_code == 200, paused.text
                    pending_card = _call(client, mcp_headers(agent), next_id(), "start_handoff", {
                        "target_agent": agent, "project_id": project_id, "task_id": auth["id"],
                        "conversation_id": source["id"], "source_agent": predecessor,
                    })
                    candidate_id = pending_card["handoff_id"]
                    ready_cards[agent] = candidate_id
                    contract = pending_card["continuation"]
                    assert contract["must_not_execute"] is True and contract["resume_from"] is None
                    assert "do not rewrite the parser" in contract["readback"]["constraints"]
                    assert "--tenant selected" in contract["readback"]["next"]
                    assert client.post("/mcp/", json={
                        "jsonrpc": "2.0", "id": next_id(), "method": "tools/call",
                        "params": {"name": "confirm_handoff", "arguments": {
                            "handoff_id": candidate_id, "confirmed": True, "actor": agent,
                        }},
                    }).status_code == 401
                    wrong_agent = next(other for other in tokens if other != agent)
                    silent = client.post(f"/api/connections/{agent}/hook", headers=mcp_headers(agent), json={
                        "event": "session-start", "conversationId" if agent == "antigravity" else "session_id": f"paused-{agent}", "prompt": "接着做",
                        "context_request": True, "actor": wrong_agent, "confirmed": True,
                    })
                    assert silent.status_code == 200, silent.text
                    assert not {"injection", "card", "handoff_id"}.intersection(silent.json())
                    assert format_host_response(agent, "session-start", silent.json()) == {}
                    denied = _call(client, mcp_headers(wrong_agent), next_id(), "confirm_handoff", {
                        "handoff_id": candidate_id, "confirmed": True,
                    })
                    assert denied["ok"] is False
                    assert client.get(f"/api/handoffs/{candidate_id}").json()["status"] == "awaiting_confirmation"
                    assert _call(client, mcp_headers(agent), next_id(), "confirm_handoff", {
                        "handoff_id": candidate_id, "confirmed": False,
                    })["ok"] is False
                    approved = _call(client, mcp_headers(agent), next_id(), "confirm_handoff", {
                        "handoff_id": candidate_id, "confirmed": True,
                    })
                    assert approved["continuation"]["resume_from"] == pending_card["card"]["next_steps"]
                    assert approved["continuation"]["selected_task_id"] == auth["id"]
                    read = _call(client, mcp_headers(agent), next_id(), "read_handoff", {
                        "handoff_id": candidate_id,
                    })
                    assert read["continuation"]["must_not_execute"] is False
                    assert read["continuation"]["readback"] == contract["readback"]

                # Revocation is per identity: an invalidated target cannot use
                # an already-confirmed card, while other targets still can.
                keeper = "opencode"
                for agent in tokens:
                    if agent == keeper:
                        continue
                    revoked = client.post(f"/api/connections/{agent}/token/revoke", headers=csrf)
                    assert revoked.status_code == 200, revoked.text
                    for name, arguments in (
                        ("read_memory", {"id": shared_id}),
                        ("read_handoff", {"handoff_id": ready_cards[agent]}),
                        ("confirm_handoff", {"handoff_id": ready_cards[agent], "confirmed": True}),
                    ):
                        rejected = client.post("/mcp/", headers=mcp_headers(agent), json={
                            "jsonrpc": "2.0", "id": next_id(), "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        })
                        assert rejected.status_code == 401, rejected.text
                    assert _call(client, mcp_headers(keeper), next_id(), "read_memory", {
                        "id": shared_id,
                    })["content"] == previous_value

                # Delete a raw source without deleting independent memory, then
                # restart the real service. Revocations and tombstones persist.
                attributed = _call(client, mcp_headers(keeper), next_id(), "auto_summarize_memory", {
                    "project_id": project_id, "conversation_id": conv["id"],
                    "memories": [{
                        "category": "knowledge", "stance": "fact",
                        "title": "Source-backed independent memory", "content": "The captured task concerns auth engine.",
                        "quote": "Implement auth engine; do not rewrite the parser.",
                        "source_message_index": 0,
                    }],
                })
                surviving_id = next(
                    item["id"] for item in attributed["saved_memories"]
                    if item["title"] == "Source-backed independent memory"
                )
                assert client.delete(f"/api/conversations/{conv['id']}", headers=csrf).status_code == 200
                assert _call(client, mcp_headers(keeper), next_id(), "read_memory", {
                    "id": shared_id,
                })["content"] == previous_value
                independent = _call(client, mcp_headers(keeper), next_id(), "read_memory", {"id": surviving_id})
                assert independent["sources"][0]["id"] == conv["id"]
                assert independent["sources"][0]["deleted"] is True
                assert client.delete(
                    f"/api/content/{shared_id}?expected_version={version}", headers=csrf,
                ).status_code == 200
                proc.terminate()
                proc.wait(timeout=10)
                proc = subprocess.Popen(
                    proc.args, cwd=Path(__file__).parents[1], env=env, stdout=log, stderr=log,
                )
                for _ in range(100):
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    raise AssertionError("Synthetic restart failed")
                assert client.get(f"/api/conversations/{conv['id']}").status_code == 404
                independent = _call(client, mcp_headers(keeper), next_id(), "read_memory", {"id": surviving_id})
                assert independent["content"] == "The captured task concerns auth engine."
                assert independent["sources"][0]["deleted"] is True
                _call(client, mcp_headers(keeper), next_id(), "read_memory", {"id": shared_id}, expect_error=True)
                # The final revision was deleted; saving the identical memory
                # through a different authenticated identity cannot resurrect it.
                _call(client, mcp_headers(keeper), next_id(), "save_memory", {
                    "project_id": project_id, "title": "Cross-Agent shared fact",
                    "content": previous_value, "category": "knowledge",
                }, expect_error=True)
                rejected = client.post("/mcp/", headers=mcp_headers("codex"), json={
                    "jsonrpc": "2.0", "id": next_id(), "method": "tools/list", "params": {},
                })
                assert rejected.status_code == 401

                assert client.get("/api/settings").json().get("compute_authorized") is False
                assert not list(home.rglob("*.token"))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
