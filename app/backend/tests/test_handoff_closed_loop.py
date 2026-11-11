"""Gold-standard closed loop: interrupt → identify → six-field card → confirm → continue."""
import pytest
from fastapi import HTTPException

from app import mcp_server
from app.database import create_schema, now_iso
from app.integrations.cc_hook import format_host_response
from app.security import authenticated_agent
from app.services import content_service, conversation_service, handoff_service, project_service, retrieval_service


def _as(agent: str):
    token = authenticated_agent.set(agent)
    return token


def test_confirm_then_continue_from_selected_task(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "closed-loop.db"))
    create_schema()

    project = project_service.create_project(name="Closed Loop Auth")
    project_id = project["id"]

    d_old = content_service.create_content(
        title="Auth token",
        content="Use session cookies.",
        project_id=project_id,
        category="decision",
        agent="cursor",
    )
    d_new = content_service.create_content(
        title="Auth token",
        content="Use JWT access tokens.",
        project_id=project_id,
        category="decision",
        agent="cursor",
        supersedes_id=d_old["id"],
    )
    assert content_service.get_content(d_old["id"])["status"] == "historical"

    t_auth = content_service.create_content(
        title="Auth engine",
        content="Wire JWT in auth/jwt.py after the parser merge.",
        project_id=project_id,
        category="task",
        agent="cursor",
        tags=["status:in_progress"],
    )
    t_ui = content_service.create_content(
        title="Settings UI",
        content="Unrelated settings page.",
        project_id=project_id,
        category="task",
        agent="cursor",
        tags=["status:pending"],
    )
    t_parser = content_service.create_content(
        title="Parser",
        content="Parser already merged.",
        project_id=project_id,
        category="task",
        agent="cursor",
        tags=["status:completed"],
    )

    conv = conversation_service.upsert_conversation(
        agent="cursor",
        title="Auth engine interrupted",
        external_id="cursor:closed-loop-raw",
        project_id=project_id,
        messages=[
            {
                "role": "user",
                "content": "Implement auth engine; do not rewrite the parser. Token issuer URL 尚未确定.",
            },
            {
                "role": "assistant",
                "content": "Parser already merged. Next: wire JWT in auth/jwt.py. 阻塞: token issuer URL 尚未确定. 下一步应先确认 issuer.",
            },
        ],
    )
    assert not conv.get("process_summary")

    with pytest.raises(HTTPException) as error:
        handoff_service.create_handoff(project_id=project_id, target_agent="codex", query="接着做")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "ambiguous_task"

    identity = _as("codex")
    try:
        ambiguous = mcp_server.start_handoff(target_agent="codex", project_id=project_id, query="接着做")
        assert ambiguous["ok"] is False
        assert ambiguous["code"] == "ambiguous_task"
        titles = {item["title"] for item in ambiguous["candidates"]}
        assert "Auth engine" in titles
        assert "Settings UI" in titles
        assert "Parser" not in titles

        created = mcp_server.start_handoff(
            target_agent="codex",
            project_id=project_id,
            task_id=t_auth["id"],
            conversation_id=conv["id"],
            source_agent="cursor",
        )
        hid = created["handoff_id"]
        card = created["card"]
        assert created["status"] == "awaiting_confirmation"
        assert "Auth engine" in card["goal"]
        assert "wire JWT" in card["next_steps"] or "issuer" in card["next_steps"]
        assert "尚未确定" in card["blockers"]
        assert "JWT access tokens" in card["decisions"]
        assert "session cookies" not in card["decisions"]
        assert "Settings UI" not in card["next_steps"]
        assert "已完成勿重做" in card["progress"]
        assert "Parser" in card["progress"]
        assert "并行任务未选定" in card["progress"]
        assert created["continuation"]["must_not_execute"] is True

        unconfirmed = mcp_server.read_handoff(hid)
        assert unconfirmed["status"] == "awaiting_confirmation"
        assert unconfirmed["must_not_execute"] is True
        assert unconfirmed["card"] == card
    finally:
        authenticated_agent.reset(identity)

    identity = _as("cursor")
    try:
        denied = mcp_server.confirm_handoff(hid, confirmed=True)
        assert denied["ok"] is False
    finally:
        authenticated_agent.reset(identity)

    identity = _as("codex")
    try:
        confirmed = mcp_server.confirm_handoff(hid, confirmed=True)
        assert confirmed["ok"] is True
        assert confirmed["status"] == "confirmed"
        continuation = confirmed["continuation"]
        assert continuation["status"] == "ready"
        assert continuation["must_not_execute"] is False
        assert continuation["selected_task_id"] == t_auth["id"]
        assert continuation["resume_from"]
        assert {item["id"] for item in continuation["parallel_open_tasks"]} == {t_ui["id"]}
        assert {item["id"] for item in continuation["do_not_redo"]} == {t_parser["id"]}
        assert any(item["id"] == d_new["id"] for item in continuation["active_decisions"])
        assert any(item["id"] == d_old["id"] for item in continuation["excluded_historical_decisions"])

        read = mcp_server.read_handoff(hid)
        assert read["continuation"]["selected_task_id"] == t_auth["id"]

        pack = retrieval_service.assemble_context(
            "continue auth engine", project_id=project_id, consumer_id="codex", budget_tokens=32000
        )
        assert pack["continuation"]["status"] == "ready"
        assert pack["continuation"]["selected_task_id"] == t_auth["id"]
        texts = " ".join(entry["text"] for entry in pack["entries"])
        assert "JWT access tokens" in texts
        assert "session cookies" not in texts
        resume_ids = {entry["id"] for entry in pack["entries"] if entry.get("tier") == "resume"}
        assert t_auth["id"] in resume_ids
        assert t_parser["id"] not in {entry["id"] for entry in pack["entries"]}

        assembled = mcp_server.assemble_context("接着做", project_id=project_id, max_tokens=32000)
        assert assembled["trigger"] == "opt_in_mcp"
        assert assembled["continuation"]["status"] == "ready"
        assert assembled["continuation"]["selected_task_id"] == t_auth["id"]
    finally:
        authenticated_agent.reset(identity)

    for host, event in (("codex", "session-start"), ("claude-code", "UserPromptSubmit"), ("cursor", "beforeSubmitPrompt")):
        expected = {"continue": True} if host == "cursor" and event == "beforeSubmitPrompt" else {}
        assert format_host_response(host, event, {"injection": "card", "card": card}) == expected

    conversation_service.upsert_conversation(
        agent="cursor",
        title="Auth engine interrupted",
        external_id="cursor:closed-loop-raw",
        project_id=project_id,
        messages=[
            *conv["messages"],
            {"role": "user", "content": "extra turn after confirm " + now_iso()},
        ],
    )
    identity = _as("codex")
    try:
        with pytest.raises(HTTPException) as stale:
            mcp_server.read_handoff(hid)
        assert stale.value.status_code == 409
    finally:
        authenticated_agent.reset(identity)


def test_interrupted_conversation_without_summary_builds_raw_card(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "raw-interrupt.db"))
    create_schema()
    project = project_service.create_project(name="Raw Interrupt")
    conv = conversation_service.upsert_conversation(
        agent="omp",
        title="Quota exhausted mid-task",
        external_id="omp:raw-interrupt",
        project_id=project["id"],
        cwd=str(tmp_path / "pagination"),
        source_path=str(tmp_path / "captured-omp.jsonl"),
        messages=[
            {"role": "user", "content": "Fix the flaky pagination test. 不要下载模型；不得发起付费调用。下一步先复现失败用例。"},
            {"role": "assistant", "content": "Started tracing. 阻塞: 还缺失败日志。"},
        ],
    )
    card = handoff_service.create_handoff(
        project_id=project["id"],
        target_agent="codex",
        conversation_id=conv["id"],
        source_agent="omp",
    )
    assert card["status"] == "awaiting_confirmation"
    assert "复现失败用例" in card["card"]["next_steps"] or "下一步" in card["card"]["next_steps"]
    assert "失败日志" in card["card"]["blockers"] or "阻塞" in card["card"]["blockers"]
    assert "不要下载模型" in card["card"]["decisions"]
    assert "不得发起付费调用" in card["card"]["decisions"]
    assert conv["cwd"] in card["card"]["evidence"]
    assert conv["source_path"] in card["card"]["evidence"]
    confirmed = handoff_service.confirm_handoff(card["id"], True, actor="codex")
    continuation = handoff_service.continuation_contract(confirmed, "codex")
    assert continuation["status"] == "ready"
    assert continuation["resume_from"] == card["card"]["next_steps"]


def test_discover_relay_conversations_and_http_get_fallback(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "relay-get-fallback.db"))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    project = project_service.create_project(name="Relay Fallback Project")
    conv = conversation_service.upsert_conversation(
        agent="claude-code",
        title="Refactor relay tool visibility",
        external_id="claude-code:relay-fallback-1",
        project_id=project["id"],
        cwd=str(tmp_path / "relay-repo"),
        source_path=str(tmp_path / "session.jsonl"),
        messages=[
            {"role": "user", "content": "Ensure discover_relay_conversations and HTTP fallback work. 下一步验证 #1 选择。"},
            {"role": "assistant", "content": "Added GET /api/handoffs/relay/conversation."},
        ],
    )

    identity = _as("codex")
    try:
        discovered = mcp_server.discover_relay_conversations(limit=10, language="zh")
        assert discovered["status"] == "selection_required"
        assert discovered["total"] >= 1
        assert any(item["id"] == conv["id"] for item in discovered["candidates"])
    finally:
        authenticated_agent.reset(identity)

    client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
    list_res = client.get("/api/handoffs/relay/conversation?consumer=codex&sync_first=false")
    assert list_res.status_code == 200
    list_body = list_res.json()
    assert list_body["status"] == "selection_required"
    assert any(item["id"] == conv["id"] for item in list_body["candidates"])

    relay_res = client.get("/api/handoffs/relay/conversation?selection=1&consumer=codex&sync_first=false")
    assert relay_res.status_code == 200
    relay_body = relay_res.json()
    assert relay_body["ok"] is True
    assert relay_body["status"] == "loaded"
    assert relay_body["confirmed"] is False
    assert relay_body["must_not_execute"] is True
    assert relay_body["conversation_id"] == conv["id"]
    assert relay_body["continuation"]["must_not_execute"] is True
    assert relay_body["continuation"]["status"] == "awaiting_confirmation"
    tasked = client.get("/api/handoffs/relay/conversation?selection=1&consumer=codex&sync_first=false&query=补上可见性测试断言")
    assert tasked.status_code == 200
    assert tasked.json()["must_not_execute"] is False
    assert tasked.json()["status"] == "confirmed"
    assert tasked.json()["continuation"]["status"] == "ready"


def test_codex_xml_title_cleaning_and_continuation_session_filtering(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "codex-xml-clean.db"))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    project = project_service.create_project(name="漫剧")
    goal_conv = conversation_service.upsert_conversation(
        agent="codex",
        title='<codex_internal_context source="goal">',
        external_id="codex:goal-conv",
        project_id=project["id"],
        cwd=str(tmp_path / "manju"),
        source_path=str(tmp_path / "goal.jsonl"),
        messages=[
            {
                "role": "user",
                "content": (
                    '<codex_internal_context source="goal">\n'
                    "Continue working toward the active thread goal.\n"
                    "<objective>\n# 419页PPT修复与交付检查方案\n</objective>\n"
                    "</codex_internal_context>"
                ),
            },
            {"role": "assistant", "content": "已完成四页核验。"},
        ],
    )
    trigger_conv = conversation_service.upsert_conversation(
        agent="codex",
        title="<environment_context>",
        external_id="codex:trigger-only",
        project_id=project["id"],
        cwd=str(tmp_path / "manju"),
        source_path=str(tmp_path / "trigger.jsonl"),
        messages=[
            {"role": "user", "content": "<environment_context>\n  <cwd>/Volumes/BIWIN_DEV/漫剧</cwd>\n</environment_context>"},
            {"role": "user", "content": '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>'},
            {"role": "user", "content": "[$contextcore-handoff](/Users/qiaozhi/.agents/skills/contextcore-handoff/SKILL.md)"},
            {"role": "user", "content": "<skill>\n<name>contextcore-handoff</name>\n</skill>"},
            {
                "role": "assistant",
                "content": "已收到 contextcore-handoff 技能。目前未发现 assemble_context、start_handoff 等交接工具。",
            },
        ],
    )

    disc = handoff_service.discover_relay_conversations(consumer="codex", sync_first=False, language="zh")
    cand_ids = [c["id"] for c in disc["candidates"]]
    assert goal_conv["id"] in cand_ids
    assert trigger_conv["id"] not in cand_ids
    goal_item = next(c for c in disc["candidates"] if c["id"] == goal_conv["id"])
    assert goal_item["title"] == "419页PPT修复与交付检查方案"


def test_one_click_connect_recovers_owned_token_and_refreshes_skill_asset(tmp_path, monkeypatch):
    import hashlib
    import json
    from fastapi.testclient import TestClient
    from app.main import app
    from app.services import connection_service
    from app.services.management_service import ManagementService

    db_file = tmp_path / "one-click-recovery.db"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_file))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    create_schema()

    client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
    csrf = client.get("/api/session").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}

    # Initial one-click-connect for codex
    res1 = client.post("/api/connections/codex/one-click-connect", json={}, headers=headers)
    assert res1.status_code == 200
    body1 = res1.json()
    assert body1["ok"] is True
    assert body1["status"] == "connected"

    # Simulate legacy SKILL.md owned by manifest + stale skill asset in ManagementService
    skill_path = tmp_path / ".agents" / "skills" / "contextcore-handoff" / "SKILL.md"
    manifest_path = tmp_path / ".contextcore" / "manifests" / "codex.json"
    old_skill = b"# Legacy Skill\n1. Call assemble_context\n"
    skill_path.write_bytes(old_skill)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skill_hash"] = hashlib.sha256(old_skill).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    mgr = ManagementService(db_path=db_file, home=tmp_path)
    stale_asset = mgr.save(
        "asset",
        {
            "kind": "skill",
            "name": "contextcore-handoff",
            "install_mode": "copy",
            "files": {"SKILL.md": old_skill.decode("utf-8")},
            "agent_ids": ["codex"],
            "source": {"type": "local", "path": str(skill_path.parent)},
        },
    )

    # Disable connection (which clears token_hash in connections table while credential remains on disk)
    connection_service.patch_connection("codex", enabled=False, confirmed=True)

    # Re-run one-click-connect: should recover owned token_hash and upgrade SKILL.md cleanly
    res2 = client.post("/api/connections/codex/one-click-connect", json={}, headers=headers)
    assert res2.status_code == 200
    body2 = res2.json()
    assert body2["ok"] is True, body2
    assert body2["warning"] is None
    assert "discover_relay_conversations" in skill_path.read_text(encoding="utf-8")

    # Sync local environment: should also refresh the stale contextcore-handoff asset in management_entities
    mgr.sync_local_environment()
    refreshed_asset = mgr.get("asset", stale_asset["id"])
    assert "discover_relay_conversations" in refreshed_asset["files"]["SKILL.md"]


def test_discover_relay_conversations_scopes_to_project_and_cleans_titles(tmp_path, monkeypatch):
    db_file = tmp_path / "relay-project-scope.db"
    monkeypatch.setenv("CONTEXTCORE_DB", str(db_file))
    monkeypatch.setenv("CONTEXTCORE_NO_BOOTSTRAP", "1")
    create_schema()

    yu_dir = tmp_path / "yu"
    yu_dir.mkdir(parents=True, exist_ok=True)
    other_dir = tmp_path / "other_project"
    other_dir.mkdir(parents=True, exist_ok=True)

    yu_proj = project_service.create_project("yu", "yu project", [str(yu_dir)])
    other_proj = project_service.create_project("漫剧", "other project", [str(other_dir)])

    # 1. Unrelated project conversation (should NOT appear when scoped to yu)
    other_conv = conversation_service.upsert_conversation(
        agent="codex",
        title="419 页 PPT 修订：额外页面素材",
        external_id="codex:other-1",
        project_id=other_proj["id"],
        cwd=str(other_dir),
        source_path=str(tmp_path / "other.jsonl"),
        messages=[
            {"role": "user", "content": "修改漫剧419页PPT素材"},
            {"role": "assistant", "content": "已处理漫剧PPT素材"},
        ],
    )

    # 2. yu conversation with generic first-turn opener "了解一下项目。" followed by real task
    yu_conv_1 = conversation_service.upsert_conversation(
        agent="codex",
        title="了解一下项目。",
        external_id="codex:yu-1",
        project_id=yu_proj["id"],
        cwd=str(yu_dir),
        source_path=str(tmp_path / "yu1.jsonl"),
        messages=[
            {"role": "user", "content": "了解一下项目。"},
            {"role": "assistant", "content": "已梳理项目结构。"},
            {
                "role": "user",
                "content": "# Files mentioned by the user:\n## 闲鱼自动回复方案.docx: /Users/qiaozhi/Desktop/yu/闲鱼自动回复方案.docx\n\n## My request for Codex:\n把这里面的闲鱼和淘宝客服的都给他做完\n",
            },
            {"role": "assistant", "content": "好的，开始实现闲鱼与淘宝客服处理。"},
        ],
    )

    # 3. Duplicate yu conversation (same agent, project, title, goal)
    yu_conv_dup = conversation_service.upsert_conversation(
        agent="codex",
        title="把这里面的闲鱼和淘宝客服的都给他做完",
        external_id="codex:yu-1-dup",
        project_id=yu_proj["id"],
        cwd=str(yu_dir),
        source_path=str(tmp_path / "yu1-dup.jsonl"),
        messages=[
            {"role": "user", "content": "把这里面的闲鱼和淘宝客服的都给他做完"},
            {"role": "assistant", "content": "好的，开始实现闲鱼与淘宝客服处理。"},
        ],
    )

    # 4. Latest continuation-only trigger session inside yu (where user just ran /contextcore)
    yu_trigger = conversation_service.upsert_conversation(
        agent="codex",
        title="/contextcore",
        external_id="codex:yu-trigger",
        project_id=yu_proj["id"],
        cwd=str(yu_dir),
        source_path=str(tmp_path / "yu-trigger.jsonl"),
        messages=[
            {"role": "user", "content": "<environment_context>\n  <cwd>" + str(yu_dir) + "</cwd>\n</environment_context>"},
            {"role": "user", "content": "[$contextcore-handoff](/Users/qiaozhi/.agents/skills/contextcore-handoff/SKILL.md)"},
            {"role": "assistant", "content": "已收到 contextcore-handoff 技能。"},
        ],
    )

    # Auto-inferred scope from consumer's latest continuation-only trigger session in yu
    disc_auto = handoff_service.discover_relay_conversations(consumer="codex", sync_first=False, language="zh")
    assert disc_auto["scoped_project_id"] == yu_proj["id"]
    assert disc_auto["scoped_project_name"] == "yu"
    cand_ids = [c["id"] for c in disc_auto["candidates"]]
    assert other_conv["id"] not in cand_ids
    assert yu_trigger["id"] not in cand_ids
    assert len(disc_auto["candidates"]) == 1
    assert disc_auto["candidates"][0]["title"] == "把这里面的闲鱼和淘宝客服的都给他做完"
    assert "当前项目【yu】" in disc_auto["question_prompt"]

    relay_auto = handoff_service.relay_conversation(consumer="codex", sync_first=False, language="zh")
    assert "当前项目【yu】" in relay_auto["question_prompt"]

    # Explicit cwd scope also scopes strictly to yu
    disc_cwd = handoff_service.discover_relay_conversations(
        consumer="claude-code",
        cwd=str(yu_dir / "subdir"),
        sync_first=False,
        language="zh",
    )
    assert disc_cwd["scoped_project_id"] == yu_proj["id"]
    assert len(disc_cwd["candidates"]) == 1
    assert disc_cwd["candidates"][0]["project_name"] == "yu"

    # Continuation-only sessions in yu must not fall back to 漫剧.
    only_dir = tmp_path / "only"
    only_dir.mkdir()
    only_trigger = project_service.create_project("only-trigger", "empty of real chats", [str(only_dir)])
    conversation_service.upsert_conversation(
        agent="codex",
        title="/contextcore",
        external_id="codex:only-trigger",
        project_id=only_trigger["id"],
        cwd=str(tmp_path / "only"),
        messages=[{"role": "user", "content": "/contextcore"}, {"role": "assistant", "content": "已收到。"}],
    )
    disc_only = handoff_service.discover_relay_conversations(
        consumer="omp", project_id=only_trigger["id"], sync_first=False, language="zh",
    )
    assert [item["id"] for item in disc_only["candidates"]] == []
    assert "漫剧" not in disc_only["question_prompt"]
    assert disc_only["scope_widened"] is False





