"""Integration and contract tests for Wave 2B REST routers, MCP closed-loop server, and 6 host skills."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def env_setup():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        db_path = tmp_path / "test_mcp_closed_loop.db"
        os.environ["CONTEXTCORE_DB"] = str(db_path)
        os.environ["CONTEXTCORE_NO_BOOTSTRAP"] = "1"

        from app.database import create_schema
        from app.main import app

        create_schema()
        client = TestClient(
            app,
            base_url="http://127.0.0.1:8420",
            client=("127.0.0.1", 50000),
        )
        csrf = client.get("/api/session").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}
        yield client, headers, tmp_path


def test_rest_working_tasks_and_content_lifecycle_and_usage_summary(env_setup):
    """Verify REST /api/working-tasks, /api/content outdated/correct/exclude_category, and usage_summary 3-state progression."""
    from app import mcp_server
    from app.security import authenticated_agent

    client, headers, tmp_path = env_setup
    repo_dir = tmp_path / "alpha-repo"
    repo_dir.mkdir(parents=True, exist_ok=True)

    proj = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "Alpha Workbench", "directories": [str(repo_dir)]},
    ).json()
    pid = proj["id"]

    # 1. Create long-term memory with applicability -> starts in 'saved' usage state
    mem_res = client.post(
        "/api/content",
        headers=headers,
        json={
            "project_id": pid,
            "kind": "memory",
            "category": "decision",
            "title": "Database driver",
            "content": "Use SQLite WAL mode with BEGIN IMMEDIATE transactions.",
            "applicability": {"env": "local-desktop", "when": "SQLite write paths"},
            "origin_authority": "user_explicit",
        },
    )
    assert mem_res.status_code == 200, mem_res.text
    mem = mem_res.json()
    assert mem["origin_authority"] == "user_explicit"
    assert mem["applicability"]["env"] == "local-desktop"
    assert mem["usage_summary"]["state"] == "saved"
    assert mem["usage_summary"]["provided_count"] == 0
    assert mem["usage_summary"]["cited_count"] == 0

    # Also create a legacy category='task' item to verify exclude_category='task'
    legacy_task = client.post(
        "/api/content",
        headers=headers,
        json={
            "project_id": pid,
            "kind": "memory",
            "category": "task",
            "title": "Legacy task row",
            "content": "Should be hidden when exclude_category=task",
        },
    ).json()

    filtered_list = client.get(
        f"/api/content?project_id={pid}&status=active&exclude_category=task"
    ).json()
    filtered_ids = {item["id"] for item in filtered_list}
    assert mem["id"] in filtered_ids
    assert legacy_task["id"] not in filtered_ids

    filtered_index = client.get(
        f"/api/content/index?project_id={pid}&status=active&exclude_category=task"
    ).json()
    assert mem["id"] in {item["id"] for item in filtered_index["items"]}
    assert legacy_task["id"] not in {item["id"] for item in filtered_index["items"]}

    # 2. MCP read_memory transitions usage_summary from 'saved' -> 'provided'
    tok = authenticated_agent.set("claude-code")
    try:
        read_back = mcp_server.read_memory(mem["id"])
        assert read_back["usage_summary"]["state"] == "provided"
        assert read_back["usage_summary"]["provided_count"] == 1
        assert read_back["usage_summary"]["cited_count"] == 0
    finally:
        authenticated_agent.reset(tok)

    # 3. Create short-term working task via POST /api/working-tasks in discussion_only mode
    wt_res = client.post(
        "/api/working-tasks",
        headers=headers,
        json={
            "project_id": pid,
            "title": "Refactor auth middleware",
            "status": "in_progress",
            "execution_mode": "discussion_only",
            "git_branch": "feat/auth-mw",
            "last_agent": "omp",
            "goal": "Align CSRF & bearer middleware order",
            "current_state": "Comparing 2 middleware ordering options",
            "completed_steps": ["Audited existing SecurityMiddleware"],
            "temp_constraints": ["这次先别改前端登录弹窗"],
            "failed_attempts": ["Moving CORS innermost broke preflight"],
            "blockers": ["Waiting on user choice for token header"],
            "next_steps": ["Confirm option B with user"],
            "cited_memory_ids": [mem["id"]],
        },
    )
    assert wt_res.status_code == 200, wt_res.text
    wt = wt_res.json()
    assert wt["id"].startswith("tsk_")
    assert wt["execution_mode"] == "discussion_only"
    assert wt["must_not_execute"] is True
    assert wt["temp_constraints"] == ["这次先别改前端登录弹窗"]
    assert len(wt["events"]) >= 1

    # Memory usage_summary must now be 'cited'!
    mem_after_cite = client.get(f"/api/content/{mem['id']}").json()
    assert mem_after_cite["usage_summary"]["state"] == "cited"
    assert mem_after_cite["usage_summary"]["provided_count"] >= 1
    assert mem_after_cite["usage_summary"]["cited_count"] >= 1

    # 4. GET /api/working-tasks list & detail with events, PATCH CAS & pause/resume
    active_list = client.get(f"/api/working-tasks?project_id={pid}&active_only=true").json()
    assert any(t["id"] == wt["id"] for t in active_list)
    branch_list = client.get(f"/api/working-tasks?project_id={pid}&git_branch=feat/auth-mw").json()
    assert [t["id"] for t in branch_list] == [wt["id"]]

    detail = client.get(f"/api/working-tasks/{wt['id']}").json()
    assert len(detail["events"]) >= 1

    # Stale expected_version -> 409
    stale_patch = client.patch(
        f"/api/working-tasks/{wt['id']}",
        headers=headers,
        json={"status": "paused", "expected_version": 999},
    )
    assert stale_patch.status_code == 409

    # Pause -> exits active_only=true list
    paused = client.patch(
        f"/api/working-tasks/{wt['id']}",
        headers=headers,
        json={"status": "paused", "execution_mode": "approved", "expected_version": wt["version"]},
    ).json()
    assert paused["status"] == "paused"
    assert paused["execution_mode"] == "approved"
    assert paused["must_not_execute"] is False
    assert all(
        t["id"] != wt["id"]
        for t in client.get(f"/api/working-tasks?project_id={pid}&active_only=true").json()
    )
    assert any(
        t["id"] == wt["id"]
        for t in client.get(f"/api/working-tasks?project_id={pid}&active_only=false").json()
    )

    # Link a real source conversation with the quote used below (promotion now requires it)
    from app.services import conversation_service

    promote_conv = conversation_service.upsert_conversation(
        agent="codex",
        project_id=pid,
        external_id="conv-cors-promote-evidence",
        title="CORS middleware order debugging",
        messages=[
            {
                "role": "user",
                "content": "Moving CORS innermost broke preflight; keep SecurityMiddleware outside CORSMiddleware.",
            }
        ],
    )
    linked = client.patch(
        f"/api/working-tasks/{wt['id']}",
        headers=headers,
        json={"source_conversation_id": promote_conv["id"], "expected_version": paused["version"]},
    )
    assert linked.status_code == 200, linked.text

    # 5. POST /api/working-tasks/{id}/promote requires non-empty applicability and returns {"task", "memory"}
    bad_promote = client.post(
        f"/api/working-tasks/{wt['id']}/promote",
        headers=headers,
        json={
            "title": "CORS middleware order",
            "content": "Register SecurityMiddleware after CORSMiddleware.",
            "category": "lesson",
            "applicability": {},
        },
    )
    assert bad_promote.status_code == 422

    good_promote = client.post(
        f"/api/working-tasks/{wt['id']}/promote",
        headers=headers,
        json={
            "title": "CORS middleware order",
            "content": "Register SecurityMiddleware after CORSMiddleware so preflight headers wrap properly.",
            "category": "lesson",
            "applicability": {"when": "FastAPI middleware registration", "paths": ["app/main.py"]},
            "evidence_quote": "Moving CORS innermost broke preflight",
            "actor": "manual",
            "complete_task": True,
        },
    )
    assert good_promote.status_code == 200, good_promote.text
    prom_body = good_promote.json()
    assert prom_body["task"]["id"] == wt["id"]
    assert prom_body["task"]["status"] == "completed"
    assert prom_body["memory"]["title"] == "CORS middleware order"
    assert prom_body["memory"]["origin_authority"] == "verified_outcome"
    assert prom_body["memory"]["promoted_from_task_id"] == wt["id"]

    # 6. POST /api/content/{id}/correct & POST /api/content/{id}/outdated
    corrected_res = client.post(
        f"/api/content/{mem['id']}/correct",
        headers=headers,
        json={
            "content": "Use SQLite WAL mode with busy_timeout=5000ms and BEGIN IMMEDIATE.",
            "expected_version": mem["version"],
        },
    )
    assert corrected_res.status_code == 200, corrected_res.text
    corrected = corrected_res.json()
    assert corrected["id"] != mem["id"]
    assert corrected["status"] == "active"
    assert mem["id"] in corrected["related_ids"]

    old_mem = client.get(f"/api/content/{mem['id']}").json()
    assert old_mem["status"] == "historical"
    assert old_mem["superseded_by_id"] == corrected["id"]

    outdated_res = client.post(
        f"/api/content/{corrected['id']}/outdated",
        headers=headers,
        json={"reason": "Migrated write lock wrapper", "expected_version": corrected["version"]},
    )
    assert outdated_res.status_code == 200, outdated_res.text
    outdated_item = outdated_res.json()
    assert outdated_item["status"] == "outdated"
    assert outdated_item["outdated_reason"] == "Migrated write lock wrapper"

    assert corrected["id"] not in {
        i["id"] for i in client.get(f"/api/content?project_id={pid}&status=active").json()
    }
    assert corrected["id"] in {
        i["id"] for i in client.get(f"/api/content?project_id={pid}&status=outdated").json()
    }
    assert mem["id"] in {
        i["id"] for i in client.get(f"/api/content?project_id={pid}&status=historical").json()
    }
    all_ids = {i["id"] for i in client.get(f"/api/content?project_id={pid}&status=all").json()}
    assert {mem["id"], corrected["id"]} <= all_ids

    # 7. DELETE /api/working-tasks/{id}
    del_res = client.delete(f"/api/working-tasks/{wt['id']}", headers=headers)
    assert del_res.status_code == 200
    assert del_res.json()["ok"] is True
    assert client.get(f"/api/working-tasks/{wt['id']}").status_code == 404


def test_mcp_tool_annotations_and_initialize_contract():
    """Verify FastMCP instructions and readOnlyHint=True ToolAnnotations on all 8 read-only tools."""
    from app.mcp_server import contextcore_mcp

    instructions = contextcore_mcp.instructions or ""
    for required_term in (
        "bootstrap_working_memory",
        "must_not_execute",
        "upsert_working_task",
        "promote_task_lesson",
        "mark_memory_outdated",
        "cite_memory",
        "relay_conversation",
    ):
        assert required_term in instructions, f"Missing {required_term!r} in FastMCP instructions"

    tools_by_name = {tool.name: tool for tool in contextcore_mcp._tool_manager.list_tools()}
    expected_read_only_tools = {
        "bootstrap_working_memory",
        "search_memory",
        "assemble_context",
        "get_project_brief",
        "read_memory",
        "get_system_status",
        "read_evidence",
        "read_handoff",
        "read_conversation_messages",
    }
    assert expected_read_only_tools <= set(tools_by_name)
    for tool_name in expected_read_only_tools:
        ann = tools_by_name[tool_name].annotations
        assert ann is not None, f"{tool_name} missing ToolAnnotations"
        assert ann.readOnlyHint is True, f"{tool_name} must have readOnlyHint=True"
        assert ann.destructiveHint is False, f"{tool_name} must have destructiveHint=False"
        assert ann.idempotentHint is True, f"{tool_name} must have idempotentHint=True"
        assert ann.openWorldHint is False, f"{tool_name} must have openWorldHint=False"

    expected_write_tools = {
        "upsert_working_task",
        "promote_task_lesson",
        "cite_memory",
        "mark_memory_outdated",
        "save_memory",
    }
    assert expected_write_tools <= set(tools_by_name)
    for tool_name in expected_write_tools:
        ann = tools_by_name[tool_name].annotations
        assert ann is not None and ann.readOnlyHint is False


def test_mcp_closed_loop_cross_agent_and_guardrails(env_setup):
    """End-to-end MCP test: bootstrap -> discussion_only guard -> multi-task ambiguity -> cite -> promote -> save_memory guardrails -> mark_outdated."""
    from app import mcp_server
    from app.security import authenticated_agent
    from app.services import content_service, project_service

    _, _, tmp_path = env_setup
    repo_a = tmp_path / "service-a"
    repo_b = tmp_path / "service-b"
    repo_a.mkdir()
    repo_b.mkdir()

    proj_a = project_service.create_project("Service A", "Main backend", [str(repo_a)])
    proj_b = project_service.create_project("Service B", "Unrelated project", [str(repo_b)])

    # Seed foreign project rule (must NEVER leak into Service A bootstrap)
    foreign_rule = content_service.create_content(
        title="Foreign Queue Rule",
        content="Always use RabbitMQ for Service B jobs.",
        project_id=proj_b["id"],
        category="decision",
        origin_authority="user_explicit",
    )

    # Step 1: OMP saves explicit user preference + global preference + discussion-only working task
    tok_omp = authenticated_agent.set("omp")
    try:
        pref_a = mcp_server.save_memory(
            cwd=str(repo_a),
            title="Package Manager",
            content="统一使用 pnpm 安装依赖，禁止混用 yarn。",
            category="preference",
            is_user_correction=True,
        )
        assert pref_a["ok"] is True and pref_a["action"] == "created"

        global_pref = mcp_server.save_memory(
            scope="global",
            title="Response Language",
            content="默认使用简洁中文输出技术结论。",
            category="preference",
            pinned=True,
            is_user_correction=True,
        )
        assert global_pref["ok"] is True

        disc_task = mcp_server.upsert_working_task(
            cwd=str(repo_a),
            title="Design rate limiter",
            execution_mode="discussion_only",
            goal="Compare token bucket vs sliding window before touching code",
            current_state="User asked for trade-off comparison only",
            next_steps=["Wait for user decision on algorithm"],
        )
        assert disc_task["ok"] is True
        assert disc_task["must_not_execute"] is True
        task_id = disc_task["task_id"]
    finally:
        authenticated_agent.reset(tok_omp)

    # Step 2: Codex bootstraps working memory in repo_a -> sees constraints, discussion_only must_not_execute=True, zero leakage from proj_b
    tok_codex = authenticated_agent.set("codex")
    try:
        boot = mcp_server.bootstrap_working_memory(
            cwd=str(repo_a),
            query="rate limiter design",
            session_key="codex-sess-1",
        )
        assert boot["ok"] is True
        assert boot["project_id"] == proj_a["id"]
        constraint_ids = {c["id"] for c in boot["applicable_constraints"]}
        assert pref_a["id"] in constraint_ids
        assert global_pref["id"] in constraint_ids
        assert foreign_rule["id"] not in constraint_ids
        assert boot["selected_task"]["id"] == task_id
        assert boot["must_not_execute"] is True
        assert boot["task_selection_required"] is False
        assert boot["feedback_summary"]

        # Verify provided telemetry was recorded on pref_a
        pref_after_boot = content_service.get_content(pref_a["id"])
        assert pref_after_boot["usage_summary"]["state"] == "provided"
        assert pref_after_boot["usage_summary"]["provided_count"] >= 1

        # Step 3: Add a second active working task -> ambiguous bootstrap sets task_selection_required=True
        second_task = mcp_server.upsert_working_task(
            project_id=proj_a["id"],
            title="Fix webhook retry jitter",
            execution_mode="approved",
            goal="Add exponential backoff jitter",
        )
        boot_multi = mcp_server.bootstrap_working_memory(cwd=str(repo_a), query="general check")
        assert len(boot_multi["active_tasks"]) == 2
        assert boot_multi["task_selection_required"] is True

        # Pause second task -> sole active task is selected cleanly again
        mcp_server.upsert_working_task(
            task_id=second_task["task_id"],
            title="Fix webhook retry jitter",
            status="paused",
        )

        # Step 4: User authorizes execution -> Codex updates task with cited_memory_ids & cites global_pref
        progressed = mcp_server.upsert_working_task(
            task_id=task_id,
            title="Design rate limiter",
            execution_mode="approved",
            status="in_progress",
            add_completed_step="Implemented sliding window counter in local SQLite",
            current_state="Unit tests green",
            cited_memory_ids=[pref_a["id"]],
        )
        assert progressed["must_not_execute"] is False
        assert content_service.get_content(pref_a["id"])["usage_summary"]["state"] == "cited"

        cite_res = mcp_server.cite_memory(
            memory_ids=[global_pref["id"]],
            reason="Formatted technical summary in concise Chinese",
            task_id=task_id,
            conversation_id="conv-codex-1",
        )
        assert cite_res["ok"] is True
        assert cite_res["summaries"][global_pref["id"]]["state"] == "cited"
        assert cite_res["summaries"][global_pref["id"]]["last_cited_agent"] == "codex"

        # Step 5: Promote validated conditional lesson (empty applicability rejected; valid applicability accepted)
        from app.services import conversation_service

        promote_conv = conversation_service.upsert_conversation(
            agent="codex",
            project_id=proj_a["id"],
            external_id="conv-rate-limiter-promote-evidence",
            title="Rate limiter clock injection",
            messages=[
                {
                    "role": "assistant",
                    "content": "Unit tests green after injecting deterministic clock into the sliding window limiter.",
                }
            ],
        )
        mcp_server.upsert_working_task(
            task_id=task_id,
            title="Design rate limiter",
            source_conversation_id=promote_conv["id"],
            expected_version=progressed["version"],
        )

        with pytest.raises(HTTPException) as exc_info:
            mcp_server.promote_task_lesson(
                task_id=task_id,
                title="Sliding window clock skew",
                content="Use monotonic clock delta for local rate-limit tests.",
                applicability={},
            )
        assert exc_info.value.status_code == 422

        promoted = mcp_server.promote_task_lesson(
            task_id=task_id,
            title="Sliding window clock skew",
            content="Inject clock function in rate limiter to avoid flaky boundary tests.",
            applicability={"when": "Rate limiter unit tests", "env": "pytest"},
            evidence_quote="Unit tests green after injecting deterministic clock",
        )
        assert promoted["ok"] is True
        lesson_id = promoted["content_id"]
        assert lesson_id

        search_hit = mcp_server.search_memory("clock function rate limiter", project_id=proj_a["id"])
        assert any(it["id"] == lesson_id and it["applicability_warning"] for it in search_hit["items"])

        # Step 6: save_memory governance guardrails
        # 6a. One-off constraint ("这次先别改界面") routes to working_tasks.temp_constraints, NOT long-term contents
        before_ids = {c["id"] for c in content_service.list_content(project_id=proj_a["id"], status="all", limit=200)}
        routed = mcp_server.save_memory(
            project_id=proj_a["id"],
            title="前端改动限制",
            content="这次先别改界面样式，只调整后端限流逻辑。",
            category="preference",
        )
        assert routed["ok"] is True
        assert routed["action"] == "routed_to_working_task"
        assert routed["task_id"] == task_id
        after_ids = {c["id"] for c in content_service.list_content(project_id=proj_a["id"], status="all", limit=200)}
        assert after_ids == before_ids
        assert any("这次先别改界面" in c for c in routed["working_task"]["temp_constraints"])

        # 6b. Reject unconfirmed AI proposal (even if agent tries self-asserting origin_authority="user_explicit" without is_user_correction)
        rej_proposal = mcp_server.save_memory(
            project_id=proj_a["id"],
            title="API 协议改造",
            content="我建议先改为 GraphQL 方案，待用户确认后再推进。",
            category="decision",
            origin_authority="user_explicit",
        )
        assert rej_proposal["ok"] is False
        assert rej_proposal["action"] == "rejected"

        # 6c. Reject unverified debugging guess
        rej_guess = mcp_server.save_memory(
            project_id=proj_a["id"],
            title="超时原因",
            content="可能是因为 SQLite 锁未释放导致的猜测，尚未验证。",
            category="lesson",
        )
        assert rej_guess["ok"] is False
        assert rej_guess["action"] == "rejected"

        # 6d. Reject raw tool output / Python traceback
        rej_log = mcp_server.save_memory(
            project_id=proj_a["id"],
            title="报错日志",
            content='Traceback (most recent call last):\n  File "app/main.py", line 42, in run\n    raise RuntimeError("boom")',
            category="lesson",
        )
        assert rej_log["ok"] is False
        assert rej_log["action"] == "rejected"

        # 6e. Unconfirmed contradiction against active preference opens pending conflict instead of overwriting
        conflict_write = mcp_server.save_memory(
            project_id=proj_a["id"],
            title="Package Manager",
            content="项目依赖安装使用 yarn。",
            category="preference",
        )
        assert conflict_write["ok"] is True
        assert conflict_write["action"] == "conflict_recorded"
        assert conflict_write["status"] == "conflict_pending"
        assert conflict_write["conflict_id"]

        boot_with_conflict = mcp_server.bootstrap_working_memory(cwd=str(repo_a))
        assert any(c["id"] == conflict_write["conflict_id"] for c in boot_with_conflict["pending_conflicts"])

        # 6f. Mark lesson outdated -> disappears from active search_memory & bootstrap_working_memory
        marked = mcp_server.mark_memory_outdated(
            memory_id=lesson_id,
            reason="Replaced by built-in FakeClock fixture",
        )
        assert marked["ok"] is True
        assert marked["status"] == "outdated"
        assert lesson_id not in {
            it["id"] for it in mcp_server.search_memory("clock function", project_id=proj_a["id"])["items"]
        }
        assert lesson_id not in {
            it["id"] for it in mcp_server.bootstrap_working_memory(cwd=str(repo_a), query="clock")["relevant_lessons"]
        }

        # 6g. Tombstone anti-resurrection on deleted memory
        to_del = mcp_server.save_memory(
            project_id=proj_a["id"],
            title="Deprecated lint flag",
            content="Enable legacy eslint --no-eslintrc flag.",
            category="knowledge",
        )
        assert to_del["ok"] is True
        del_res = mcp_server.delete_memory(
            to_del["id"], confirmed=True, expected_version=to_del["version"]
        )
        assert del_res["ok"] is True
        with pytest.raises(HTTPException) as tomb_exc:
            mcp_server.save_memory(
                project_id=proj_a["id"],
                title="Deprecated lint flag",
                content="Enable legacy eslint --no-eslintrc flag.",
                category="knowledge",
            )
        assert tomb_exc.value.status_code == 410

        # 6h. update_task_progress works on tsk_... working tasks as well
        upd_wt = mcp_server.update_task_progress(
            task_id=task_id,
            status="completed",
            expected_version= mcp_server.upsert_working_task(task_id=task_id, title="Design rate limiter")["version"],
            detail="Rate limiter shipped and verified",
        )
        assert upd_wt["ok"] is True
        assert upd_wt["working_task"]["status"] == "completed"
    finally:
        authenticated_agent.reset(tok_codex)


def test_all_six_host_skills_meet_frontmatter_size_and_closed_loop_invariants():
    """Verify all 6 host skill/command templates preserve unified name: contextcore, <16KB size, and dual workflows."""
    from app.integrations.constants import SUPPORTED_HOSTS
    from app.integrations.host_management_native import native_skill
    from app.integrations.structured_config import parse_document
    from app.integrations.templates import SKILLS_DIR, render_skill_or_command

    expected_files = [
        "claude_code_skill.md",
        "codex_skill.md",
        "cursor_skill.md",
        "antigravity_skill.md",
        "omp_command.md",
        "opencode_command.md",
    ]
    required_keywords = (
        "bootstrap_working_memory",
        "must_not_execute",
        "upsert_working_task",
        "promote_task_lesson",
        "cite_memory",
        "mark_memory_outdated",
        "discover_relay_conversations",
        "relay_conversation",
        "read_conversation_messages",
    )

    for filename in expected_files:
        path = SKILLS_DIR / filename
        assert path.is_file(), f"Missing skill file: {filename}"
        raw_bytes = path.read_bytes()
        assert len(raw_bytes) < 16_000, f"{filename} exceeds 16,000-byte limit ({len(raw_bytes)} bytes)"
        text = raw_bytes.decode("utf-8")

        fm_match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
        assert fm_match is not None, f"{filename} must start with closed YAML frontmatter"
        meta = parse_document("yaml", fm_match.group(1))
        assert meta.get("name") == "contextcore", f"{filename} must keep unified name: contextcore"
        assert isinstance(meta.get("description"), str) and meta["description"].strip()

        for kw in required_keywords:
            assert kw in text, f"{filename} missing required closed-loop/relay keyword: {kw}"

        # Validate against native_skill validators for Cursor, Antigravity, OMP, Codex
        for native_host in ("cursor", "antigravity", "omp", "codex-desktop"):
            folder_name, files = native_skill(
                {"id": "contextcore", "name": "ContextCore", "files": {"SKILL.md": text}},
                native_host,
            )
            assert folder_name == "contextcore"
            assert "bootstrap_working_memory" in files["SKILL.md"]

    # Also verify render_skill_or_command(host) across all 6 SUPPORTED_HOSTS
    for host in SUPPORTED_HOSTS:
        rendered = render_skill_or_command(host)
        content_bytes = rendered["content"].encode("utf-8")
        assert 0 < len(content_bytes) < 16_000
        for kw in required_keywords:
            assert kw in rendered["content"], f"Rendered skill for {host} missing {kw}"


def test_mcp_search_zero_popularity_bias_project_brief_and_legacy_task_bridge(env_setup):
    """Verify search_memory has zero popularity boost, get_project_brief records provided telemetry, and legacy update_task_progress syncs working_tasks."""
    from app import mcp_server
    from app.security import authenticated_agent
    from app.services import content_service, project_service, working_memory_service

    _, _, tmp_path = env_setup
    repo = tmp_path / "rank-repo"
    repo.mkdir()
    proj = project_service.create_project("Rank Repo", "Zero popularity test", [str(repo)])
    pid = proj["id"]

    weak_item = content_service.create_content(
        title="General notes",
        content="Mentions migrations once among many unrelated paragraphs about UI buttons and CSS spacing.",
        project_id=pid,
        category="knowledge",
    )
    strong_item = content_service.create_content(
        title="SQLite schema migrations protocol",
        content="SQLite schema migrations must always run idempotent ALTER TABLE checks inside migrations.",
        project_id=pid,
        category="decision",
    )

    tok = authenticated_agent.set("cursor")
    try:
        # Read weak_item 8 times so its provided_count is high -> must NOT outrank strong_item!
        for _ in range(8):
            mcp_server.read_memory(weak_item["id"])
        assert content_service.get_content(weak_item["id"])["usage_summary"]["provided_count"] == 8

        search_res = mcp_server.search_memory("SQLite schema migrations", project_id=pid, limit=5)
        assert search_res["items"][0]["id"] == strong_item["id"]
        assert content_service.get_content(strong_item["id"])["usage_summary"]["state"] == "provided"

        # get_project_brief includes working_tasks & records provided on active decisions
        brief = mcp_server.get_project_brief(pid)
        assert any(d["id"] == strong_item["id"] for d in brief["decisions"])
        assert "working_tasks" in brief

        # Legacy category='task' item updated via update_task_progress bridges into working_tasks
        legacy_task = content_service.create_content(
            title="Migrate legacy webhook table",
            content="Initial pending state",
            project_id=pid,
            category="task",
            tags=["status:pending"],
        )
        upd = mcp_server.update_task_progress(
            legacy_task["id"],
            status="in_progress",
            expected_version=legacy_task["version"],
            detail="Backfilled 50% of webhook rows",
        )
        assert upd == {
            "ok": True,
            "task_id": legacy_task["id"],
            "status": "in_progress",
            "version": legacy_task["version"] + 1,
        }
        wts = working_memory_service.list_working_tasks(project_id=pid, active_only=True)
        mirrored = next((t for t in wts if t.get("legacy_content_id") == legacy_task["id"]), None)
        assert mirrored is not None
        assert mirrored["status"] == "in_progress"
        assert mirrored["current_state"] == "Backfilled 50% of webhook rows"
    finally:
        authenticated_agent.reset(tok)


def test_fallback_bootstrap_working_memory_multi_task_and_git_branch(env_setup, monkeypatch):
    """Verify _fallback_bootstrap_working_memory handles >1 active tasks with detached/non-git and branch-matched checkouts."""
    from app import mcp_server
    from app.security import authenticated_agent
    from app.services import project_service, retrieval_service

    _, _, tmp_path = env_setup
    repo = tmp_path / "fallback-repo"
    repo.mkdir()
    proj = project_service.create_project("Fallback Repo", "Fallback multi-task test", [str(repo)])
    pid = proj["id"]

    tok = authenticated_agent.set("codex")
    try:
        t1 = mcp_server.upsert_working_task(
            project_id=pid,
            title="Implement OAuth PKCE",
            git_branch="feat/oauth-pkce",
            execution_mode="discussion_only",
            goal="Draft PKCE exchange",
        )
        t2 = mcp_server.upsert_working_task(
            project_id=pid,
            title="Optimize audit export",
            git_branch="feat/audit-export",
            execution_mode="approved",
            goal="Stream CSV rows",
        )
        assert t1["ok"] and t2["ok"]

        # Force fallback path by hiding retrieval_service.bootstrap_working_memory
        monkeypatch.setattr(retrieval_service, "bootstrap_working_memory", None, raising=False)

        # 1. Detached HEAD must NOT match or crash -> task_selection_required=True
        monkeypatch.setattr(
            project_service,
            "read_git_checkout",
            lambda _path: {"branch": "feat/oauth-pkce", "detached": True},
        )
        res_detached = mcp_server.bootstrap_working_memory(cwd=str(repo))
        assert res_detached["ok"] is True
        assert len(res_detached["active_tasks"]) == 2
        assert res_detached["selected_task"] is None
        assert res_detached["task_selection_required"] is True

        # 2. Attached checkout matching unique git_branch selects that task and propagates must_not_execute
        monkeypatch.setattr(
            project_service,
            "read_git_checkout",
            lambda _path: {"branch": "feat/oauth-pkce", "detached": False},
        )
        res_branch = mcp_server.bootstrap_working_memory(cwd=str(repo))
        assert res_branch["ok"] is True
        assert res_branch["selected_task"]["id"] == t1["task_id"]
        assert res_branch["task_selection_required"] is False
        assert res_branch["must_not_execute"] is True
    finally:
        authenticated_agent.reset(tok)




def test_save_memory_without_resolvable_project_is_rejected(env_setup):
    """save_memory must never fall back to global scope when no project can be resolved."""
    from app import mcp_server
    from app.security import authenticated_agent
    from app.services import content_service

    tok = authenticated_agent.set("omp")
    try:
        with pytest.raises(HTTPException) as exc_info:
            mcp_server.save_memory(
                title="Package manager",
                content="统一使用 pnpm 安装依赖。",
                category="preference",
                is_user_correction=True,
            )
        assert exc_info.value.status_code == 422
        assert "project" in exc_info.value.detail.lower()

        # scope='global' stays an explicit, permitted choice
        global_pref = mcp_server.save_memory(
            scope="global",
            title="Response language",
            content="默认使用简洁中文输出技术结论。",
            category="preference",
            is_user_correction=True,
        )
        assert global_pref["scope"] == "global"
        assert global_pref["project_id"] is None
        assert content_service.get_content(global_pref["id"])["project_id"] is None
    finally:
        authenticated_agent.reset(tok)


def test_fallback_bootstrap_fails_closed_on_ambiguity_and_stays_within_budget(env_setup, monkeypatch):
    """The MCP fallback must also gate ambiguous tasks and bound its serialized payload."""
    import json

    from app import mcp_server
    from app.security import authenticated_agent
    from app.services import project_service, retrieval_service

    _, _, tmp_path = env_setup
    repo = tmp_path / "fallback-budget-repo"
    repo.mkdir()
    proj = project_service.create_project("Fallback Budget Repo", "Fallback compaction test", [str(repo)])
    pid = proj["id"]

    tok = authenticated_agent.set("codex")
    try:
        verbose = "限流窗口与审计链路的完整实现细节" * 20
        t1 = mcp_server.upsert_working_task(
            project_id=pid,
            title="Implement rate limiter",
            execution_mode="discussion_only",
            goal=verbose,
            current_state=verbose,
        )
        t2 = mcp_server.upsert_working_task(
            project_id=pid,
            title="Implement audit export",
            goal=verbose,
            current_state=verbose,
        )
        assert t1["ok"] and t2["ok"]

        monkeypatch.setattr(retrieval_service, "bootstrap_working_memory", None, raising=False)
        res = mcp_server.bootstrap_working_memory(cwd=str(repo), max_tokens=1200)
        assert res["task_selection_required"] is True
        assert res["must_not_execute"] is True
        assert res["estimated_tokens"] <= 1200
        assert len(json.dumps(res, ensure_ascii=False).encode("utf-8")) == res["estimated_tokens"]
        assert res["tasks_truncated"] is True
        for task in res["active_tasks"]:
            assert {"id", "title", "status", "execution_mode", "temp_constraints"} <= set(task)
    finally:
        authenticated_agent.reset(tok)


def test_working_tasks_summary_history_and_offset_endpoints(env_setup):
    """GET /api/working-tasks/summary plus status=history & offset must not be shadowed by /{task_id}."""
    client, headers, tmp_path = env_setup
    repo = tmp_path / "summary-repo"
    repo.mkdir()
    proj = client.post(
        "/api/projects",
        headers=headers,
        json={"name": "Summary Workbench", "directories": [str(repo)]},
    ).json()
    pid = proj["id"]

    created = []
    for title in ("Refactor rate limiter", "Fix audit export", "Add CSV streaming"):
        res = client.post(
            "/api/working-tasks",
            headers=headers,
            json={"project_id": pid, "title": title, "goal": "stage goal", "status": "active"},
        )
        assert res.status_code == 200, res.text
        created.append(res.json())

    client.patch(
        f"/api/working-tasks/{created[0]['id']}",
        headers=headers,
        json={"status": "completed", "expected_version": created[0]["version"]},
    ).raise_for_status()
    client.patch(
        f"/api/working-tasks/{created[1]['id']}",
        headers=headers,
        json={"status": "paused", "expected_version": created[1]["version"]},
    ).raise_for_status()

    summary = client.get(f"/api/working-tasks/summary?project_id={pid}")
    assert summary.status_code == 200, summary.text
    assert summary.json() == {"active": 1, "history": 2, "all": 3}

    history = client.get(f"/api/working-tasks?project_id={pid}&status=history")
    assert history.status_code == 200, history.text
    assert {t["id"] for t in history.json()} == {created[0]["id"], created[1]["id"]}

    page_one = client.get(f"/api/working-tasks?project_id={pid}&limit=2&offset=0").json()
    page_two = client.get(f"/api/working-tasks?project_id={pid}&limit=2&offset=2").json()
    assert len(page_one) == 2 and len(page_two) == 1
    assert {t["id"] for t in [*page_one, *page_two]} == {t["id"] for t in created}


def test_mcp_assemble_context_propagates_task_selection_requirement(env_setup):
    """Ambiguous parallel work must surface in MCP assemble_context without permission to execute."""
    from app import mcp_server
    from app.security import authenticated_agent
    from app.services import project_service

    _, _, tmp_path = env_setup
    repo = tmp_path / "assemble-ambiguity-repo"
    repo.mkdir()
    proj = project_service.create_project("Assemble Ambiguity Repo", "Ambiguity propagation", [str(repo)])
    pid = proj["id"]

    tok = authenticated_agent.set("claude-code")
    try:
        for title in ("Implement rate limiter", "Implement audit export"):
            mcp_server.upsert_working_task(project_id=pid, title=title, goal="stage goal")

        packed = mcp_server.assemble_context(task_query="项目整体状态检查", project_id=pid, max_tokens=4000)
        assert packed["task_selection_required"] is True
        assert packed["must_not_execute"] is True
    finally:
        authenticated_agent.reset(tok)
