"""Comprehensive tests for Cross-Agent Project Relay, Implicit Memory Summarization, and Agent Management parity."""
from __future__ import annotations

from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.security import authenticated_agent
from app.database import create_schema
from app.main import app
from app import mcp_server
from app.services import (
    content_service,
    conversation_service,
    distillation_service,
    handoff_service,
    management_service,
    project_service,
    provider_presets,
)


def _as_agent(agent: str):
    return authenticated_agent.set(agent)


class TestCrossAgentProjectRelay:
    def test_discover_and_relay_project_compressed_and_full_modes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "relay.db"))
        create_schema()

        proj_alpha = project_service.create_project(name="Alpha Payments", description="Stripe webhook service")
        proj_beta = project_service.create_project(name="Beta Dashboard", description="React analytics UI")

        content_service.create_content(
            title="Webhook signature verification",
            content="Always verify Stripe-Signature header using raw request body.",
            category="decision",
            project_id=proj_alpha["id"],
            agent="claude-code",
        )
        content_service.create_content(
            title="Implement retry queue",
            content="Add dead-letter queue for failed webhook deliveries.",
            category="task",
            project_id=proj_alpha["id"],
            agent="claude-code",
            tags=["status:in_progress"],
        )
        conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj_alpha["id"],
            title="Stripe webhook debugging",
            external_id="cc:alpha-1",
            messages=[
                {"role": "user", "content": "Help me debug the Stripe webhook signature check."},
                {"role": "assistant", "content": "Verified raw body parsing; next step is implementing retry queue."},
            ],
        )

        conversation_service.upsert_conversation(
            agent="cursor",
            project_id=proj_beta["id"],
            title="Chart component layout",
            external_id="cursor:beta-1",
            messages=[
                {"role": "user", "content": "Fix the responsive grid on the analytics dashboard."},
                {"role": "assistant", "content": "Updated CSS grid columns; need to verify mobile breakpoints."},
            ],
        )

        # 1. Discover relay candidates across agents
        discovery = handoff_service.discover_relay_projects(consumer="codex", sync_first=False)
        assert discovery["total"] == 2
        assert len(discovery["candidates"]) == 2
        alpha_cand = next(c for c in discovery["candidates"] if c["project_id"] == proj_alpha["id"])
        assert alpha_cand["conversation_count"] == 1
        assert alpha_cand["memory_count"] == 1
        assert alpha_cand["latest_conversation_title"] == "Stripe webhook debugging"
        assert "Stripe webhook" in alpha_cand["latest_conversation_summary"]
        assert "1." in discovery["question_prompt"]
        assert "2." in discovery["question_prompt"]
        assert "Alpha Payments" in discovery["question_prompt"]
        assert "Beta Dashboard" in discovery["question_prompt"]
        assert "完整上下文" in discovery["question_prompt"]

        # 2. assemble_context("接力") without project_id automatically triggers relay discovery
        token = _as_agent("codex")
        try:
            assembled = mcp_server.assemble_context(task_query="接力", max_tokens=32000)
            assert assembled.get("relay_required") is True
            assert len(assembled.get("relay_candidates") or []) == 2
            assert "Alpha Payments" in (assembled.get("question_prompt") or "")

            # start_handoff without project_id when multiple projects exist returns relay_candidates + question_prompt
            ambiguous = mcp_server.start_handoff(target_agent="codex", query="接着做")
            assert ambiguous["status"] == "ambiguous_project"
            assert ambiguous["code"] == "ambiguous_project"
            assert len(ambiguous.get("relay_candidates") or []) == 2
            assert "Alpha Payments" in (ambiguous.get("question_prompt") or "")

            # 3. Relay by 1-based index or project name in compressed mode
            alpha_idx = next(
                c["index"] for c in discovery["candidates"] if c["project_id"] == proj_alpha["id"]
            )
            relayed_compressed = mcp_server.relay_project(
                project_id=str(alpha_idx),
                context_mode="compressed", max_tokens=32000,
                query="继续完成重试队列",
            )
            assert relayed_compressed["status"] == "relayed"
            assert relayed_compressed["context_mode"] == "compressed"
            assert relayed_compressed["project"]["id"] == proj_alpha["id"]
            assert relayed_compressed["handoff"]["status"] == "awaiting_confirmation"
            assert relayed_compressed["continuation"]["must_not_execute"] is True
            assert "goal" in relayed_compressed["card"]
            assert isinstance(relayed_compressed["readback"], dict)
            assert "full_conversations" not in relayed_compressed

            # 4. Relay by project name in full mode
            relayed_full = mcp_server.relay_project(
                project_id="Alpha Payments",
                context_mode="full", max_tokens=32000,
                query="继续完成重试队列",
            )
            assert relayed_full["status"] == "relayed"
            assert relayed_full["context_mode"] == "full"
            assert relayed_full["project"]["id"] == proj_alpha["id"]
            assert len(relayed_full["context"]["conversations"]) >= 1
            assert len(relayed_full["context"]["conversations"][0]["messages"]) == 2
            assert any("Stripe-Signature" in m["content"] for m in relayed_full["context"]["memories"])
        finally:
            authenticated_agent.reset(token)

    def test_relay_project_edge_cases_and_http_endpoints(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "relay-edge.db"))
        create_schema()

        proj = project_service.create_project(name="MultiTask Project")
        content_service.create_content(
            title="Task One",
            content="First open task",
            category="task",
            project_id=proj["id"],
            agent="codex",
            tags=["status:in_progress"],
        )
        t2 = content_service.create_content(
            title="Task Two",
            content="Second open task",
            category="task",
            project_id=proj["id"],
            agent="codex",
            tags=["status:pending"],
        )

        # Invalid context_mode -> 422
        with pytest.raises(HTTPException) as exc_info:
            handoff_service.relay_project(project_id=proj["id"], consumer="codex", context_mode="invalid_mode")
        assert exc_info.value.status_code == 422

        # Unknown project -> 404
        with pytest.raises(HTTPException) as exc_404:
            handoff_service.relay_project(project_id="proj_nonexistent", consumer="codex", sync_first=False)
        assert exc_404.value.status_code == 404

        # Multiple open tasks without task_id -> returns selection_required with task candidates
        ambiguous_task_res = handoff_service.relay_project(
            project_id=proj["id"], consumer="codex", sync_first=False
        )
        assert ambiguous_task_res["status"] == "selection_required"
        assert ambiguous_task_res["code"] == "ambiguous_task"
        assert len(ambiguous_task_res["candidates"]) == 2

        # Selecting explicit task_id succeeds via HTTP endpoint
        client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
        csrf_token = client.get("/api/session").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}
        cand_resp = client.get("/api/handoffs/relay/candidates?sync_first=false")
        assert cand_resp.status_code == 200
        assert cand_resp.json()["total"] >= 1

        relay_resp = client.post(
            "/api/handoffs/relay",
            headers=headers,
            json={
                "project_id": proj["id"],
                "consumer": "omp",
                "context_mode": "full", "max_tokens": 32000,
                "task_id": t2["id"],
                "sync_first": False,
            },
        )
        assert relay_resp.status_code == 200
        payload = relay_resp.json()
        assert payload["status"] == "relayed"
        assert payload["context_mode"] == "full"
        assert payload["project"]["id"] == proj["id"]


class TestImplicitMemorySummarization:
    def test_implicit_signal_extraction_and_auto_summarize_dedup_and_supersede(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "auto-mem.db"))
        create_schema()

        proj = project_service.create_project(name="Memory Test Project")
        conv = conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj["id"],
            title="Architecture discussion",
            external_id="cc:mem-1",
            messages=[
                {"role": "user", "content": "以后统一使用 pnpm 和 TypeScript strict 模式，不要用 yarn。"},
                {"role": "assistant", "content": "好的，我们决定采用 SQLite WAL 模式存储本地状态。"},
                {"role": "user", "content": "注意踩坑：避免在事务外单独调用 fsync 导致锁竞争。"},
            ],
        )

        # 1. Implicit extraction from conversation messages + explicit memories
        res1 = distillation_service.auto_summarize_memory(
            project_id=proj["id"],
            conversation_id=conv["id"],
            session_summary="确定了包管理器偏好、SQLite WAL 架构决策以及事务锁注意事项。",
            memories=[
                {
                    "category": "preference",
                    "title": "包管理器规范",
                    "content": conv["messages"][0]["content"],
                    "tags": ["tooling"],
                    "quote": conv["messages"][0]["content"],
                    "source_message_index": 0,
                    "user_message_index": 0,
                }
            ],
            actor="claude-code",
            auto_extract_from_messages=True,
        )
        assert res1["ok"] is True
        assert res1["summary_updated"] is True
        assert res1["created_count"] >= 2  # explicit + implicitly extracted

        # Verify conversation process_summary was updated
        updated_conv = conversation_service.get_conversation(conv["id"])
        assert "SQLite WAL" in updated_conv["process_summary"]

        # 2. Calling again with identical explicit memory -> deduplicated (unchanged)
        res2 = distillation_service.auto_summarize_memory(
            project_id=proj["id"],
            conversation_id=conv["id"],
            memories=[
                {
                    "category": "preference",
                    "title": "包管理器规范",
                    "content": conv["messages"][0]["content"],
                    "quote": conv["messages"][0]["content"],
                    "source_message_index": 0,
                    "user_message_index": 0,
                }
            ],
            actor="claude-code",
            auto_extract_from_messages=False,
        )
        assert res2["created_count"] == 0
        assert res2["updated_count"] == 0
        assert res2["unchanged_count"] == 1

        # Same title with different content requires conflict resolution, not silent retirement.
        revised_preference = "统一使用 pnpm 9+ Workspace 模式，禁止使用 yarn 或 npm。"
        conversation_service.upsert_conversation(
            agent="claude-code", project_id=proj["id"], title=conv["title"], external_id="cc:mem-1",
            messages=[*conv["messages"], {"role": "user", "content": revised_preference}],
        )
        res3 = distillation_service.auto_summarize_memory(
            project_id=proj["id"],
            conversation_id=conv["id"],
            memories=[
                {
                    "category": "preference",
                    "title": "包管理器规范",
                    "content": revised_preference,
                    "quote": revised_preference,
                    "source_message_index": 3,
                    "user_message_index": 3,
                }
            ],
            actor="claude-code",
            auto_extract_from_messages=False,
        )
        assert res3["updated_count"] == 1
        assert res3["saved_memories"][0]["content"] == revised_preference
        original = content_service.get_content(res2["saved_memories"][0]["id"])
        assert original["status"] == "historical"

    def test_mcp_and_http_auto_summarize(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "auto-mem-http.db"))
        create_schema()

        token = _as_agent("cursor")
        try:
            mcp_res = mcp_server.auto_summarize_memory(
                cwd=str(tmp_path / "my-workspace"),
                session_summary="完成了登录页重构并沉淀了表单校验规范。",
                memories=[
                    {
                        "category": "decision",
                        "title": "表单校验方案",
                        "content": "统一使用服务端 schema 与前端实时校验结合。",
                    }
                ],
            )
            assert mcp_res["ok"] is True
            assert mcp_res["created_count"] == 1
            assert mcp_res["saved_memories"][0]["title"] == "表单校验方案"
            project_id = mcp_res["project_id"]
            assert project_id
        finally:
            authenticated_agent.reset(token)

        client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
        csrf_token = client.get("/api/session").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}
        http_res = client.post(
            "/api/processing/auto-summarize",
            headers=headers,
            json={
                "project_id": project_id,
                "session_summary": "新增了按钮无障碍测试约定。",
                "memories": [
                    {
                        "category": "lesson",
                        "title": "无障碍按钮测试",
                        "content": "所有图标按钮必须提供 aria-label。",
                    }
                ],
            },
        )
        assert http_res.status_code == 200
        assert http_res.json()["created_count"] == 1



class TestManagementFeatureParity:
    def test_provider_presets_reorder_quick_switch_toggle_sync_env_and_import(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "management.db"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir(parents=True, exist_ok=True)
        create_schema()

        # 1. 12 official presets with default_model and category
        presets = provider_presets.list_presets()["presets"]
        assert len(presets) >= 12
        preset_ids = {p["id"] for p in presets}
        for expected in (
            "openai",
            "anthropic",
            "openrouter",
            "deepseek",
            "gemini",
            "siliconflow",
            "volcengine-ark",
            "aliyun-bailian",
            "zhipu-glm",
            "moonshot-kimi",
            "minimax",
            "xai-grok",
        ):
            assert expected in preset_ids

        mgr = management_service.service()

        # 2. Create two providers and test reorder_providers
        p1 = mgr.save(
            "provider",
            {
                "name": "First Provider",
                "protocol": "openai",
                "base_url": "https://api.openai.com/v1",
                "wire_api": "responses",
                "supported_endpoints": ["chat/completions", "responses"],
                "agent_ids": ["codex"],
            },
        )
        p2 = mgr.save(
            "provider",
            {
                "name": "Second Provider",
                "protocol": "openai",
                "base_url": "https://openrouter.ai/api/v1",
                "wire_api": "responses",
                "supported_endpoints": ["chat/completions", "responses"],
                "agent_ids": ["codex"],
            },
        )
        m2 = mgr.save(
            "model",
            {
                "name": "OR Default",
                "provider_id": p2["id"],
                "model_id": "openai/gpt-4.1-mini",
            },
        )

        reordered = mgr.reorder_providers([p2["id"], p1["id"]])
        assert [item["id"] for item in reordered[:2]] == [p2["id"], p1["id"]]
        assert reordered[0]["sort_index"] == 0
        assert reordered[1]["sort_index"] == 1

        # Catalog page also respects sort_index order
        page = mgr.catalog_page("provider", agent_id="codex")
        assert [item["id"] for item in page["items"][:2]] == [p2["id"], p1["id"]]

        # 3. 1-click quick_switch_provider on codex
        switched = mgr.quick_switch_provider("codex", p2["id"], m2["id"])
        assert switched["status"] == "planned"
        assert switched["host_id"] == "codex"
        assert switched["provider_id"] == p2["id"]
        assert switched["model_id"] == m2["id"]
        assert switched["plan"]["status"] == "planned"
        mgr.apply(switched["plan"]["id"], switched["plan"]["version"], True)

        # 4. 1-click quick_toggle_asset (enable then disable MCP tool on codex)
        mcp_asset = mgr.save(
            "asset",
            {
                "name": "fetch-mcp",
                "kind": "mcp",
                "config": {"transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-fetch"]},
                "agent_ids": ["codex"],
            },
        )
        enabled_res = mgr.quick_toggle_asset("codex", mcp_asset["id"], True)
        assert enabled_res["status"] == "planned"
        assert enabled_res["plan"]["status"] == "planned"
        mgr.apply(enabled_res["plan"]["id"], enabled_res["plan"]["version"], True)

        disabled_res = mgr.quick_toggle_asset("codex", mcp_asset["id"], False)
        assert disabled_res["status"] == "planned"
        assert disabled_res["plan"]["status"] == "planned"
        mgr.apply(disabled_res["plan"]["id"], disabled_res["plan"]["version"], True)

        # 5. Multi-host universal sync (sync_to_hosts)
        prompt_asset = mgr.save(
            "asset",
            {
                "name": "Global Coding Rule",
                "kind": "prompt",
                "content": "Always run unit tests before completing a task.",
                "agent_ids": ["codex"],
            },
        )
        sync_res = mgr.sync_to_hosts("asset", prompt_asset["id"], ["codex", "claude-code"], operation="install")
        assert sync_res["entity_id"] == prompt_asset["id"]
        assert len(sync_res["planned"]) == 2
        assert all(entry["status"] == "planned" for entry in sync_res["results"].values())

        # 6. Environment variable conflict detection
        monkeypatch.setenv("OPENAI_API_KEY", "sk-conflict-secret-123456")
        conflicts = mgr.check_env_conflicts("codex")
        assert conflicts["count"] >= 1
        assert any(c["env_var"] == "OPENAI_API_KEY" for c in conflicts["conflicts"])
        # Ensure raw secret is masked
        assert "sk-conflict-secret-123456" not in str(conflicts)

        # 7. Compatible JSON import supports mcpServers, providers, prompts, skills + rejects unknown
        imported = mgr.import_objects(
            {
                "format": "cc-switch",
                "data": {
                    "mcpServers": {
                        "time-server": {"command": "uvx", "args": ["mcp-server-time"]},
                    },
                    "providers": [
                        {"name": "DeepSeek Official", "protocol": "openai", "base_url": "https://api.deepseek.com/v1"},
                    ],
                    "prompts": {
                        "concise-style": {"content": "Keep explanations concise."},
                    },
                    "skills": {
                        "repo-audit": {"content": "# Repo Audit Skill\nCheck tests first."},
                    },
                    "unknownScript": "ignored",
                },
            }
        )
        assert len(imported["imported"]) == 4
        assert any(u["field"] == "unknownScript" for u in imported["unsupported"])

    def test_reviewer_regressions_relay_memory_and_management_edge_cases(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "regressions.db"))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home").mkdir(parents=True, exist_ok=True)
        repo_dir = tmp_path / "payments-repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        create_schema()

        # 1. Project with directories + unlinked conversation (project_id=None, cwd=repo_dir)
        proj = project_service.create_project(
            name="Payments Service",
            description="Core billing service",
            directories=[str(repo_dir)],
        )
        unlinked_conv = conversation_service.upsert_conversation(
            agent="codex",
            project_id=None,
            cwd=str(repo_dir),
            title="Same-agent billing session",
            external_id="codex:unlinked-1",
            updated_at="2026-10-20T08:00:00Z",
            messages=[
                {"role": "user", "content": "以后统一使用 pnpm，不要用 yarn。"},
                {"role": "assistant", "content": "Verified raw body parsing; next step is implementing retry queue."},
            ],
        )

        # Discover relay projects links unlinked conversation by cwd and populates directories
        disc = handoff_service.discover_relay_projects(consumer="codex", sync_first=False)
        cand = next(c for c in disc["candidates"] if c["project_id"] == proj["id"])
        assert str(repo_dir) in cand["directories"]
        assert cand["conversation_count"] == 1

        # Same-Agent history cannot manufacture a cross-Agent executable handoff.
        same_agent_relay = handoff_service.relay_project(
            project_id=proj["id"],
            consumer="codex",
            context_mode="full", max_tokens=32000,
            sync_first=False,
        )
        assert same_agent_relay["status"] == "selection_required"
        assert same_agent_relay["must_not_execute"] is True
        assert not same_agent_relay.get("handoff_id")

        # 3. mcp_server.assemble_context with context_mode="full" populates context_text
        token = _as_agent("codex")
        try:
            assembled_full = mcp_server.assemble_context(
                task_query="继续开发",
                project_id=proj["id"],
                context_mode="full", max_tokens=32000,
            )
            assert not assembled_full.get("context_text")
            assert not assembled_full.get("continuation")
        finally:
            authenticated_agent.reset(token)

        # 4. auto_summarize_memory:
        # - preserves existing conversation updated_at
        # - stores category="knowledge" with kind="memory" so project counts include it
        # - does not create rogue category="task" from heuristic "next step is..."
        # - skips tombstoned items without aborting the batch
        tombstoned_mem = content_service.create_content(
            title="Deprecated Rule",
            content="Do not use legacy v1 endpoint.",
            category="preference",
            project_id=proj["id"],
            agent="codex",
        )
        content_service.delete_content(tombstoned_mem["id"])

        existing_task = content_service.create_content(
            title="Primary Billing Task",
            content="Finish Stripe billing webhook.",
            category="task",
            project_id=proj["id"],
            agent="claude-code",
            tags=["status:in_progress"],
        )
        assert existing_task["id"]

        sum_res = distillation_service.auto_summarize_memory(
            project_id=proj["id"],
            conversation_id=unlinked_conv["id"],
            memories=[
                {
                    "category": "preference",
                    "title": "Deprecated Rule",
                    "content": "Do not use legacy v1 endpoint.",
                },
                {
                    "category": "knowledge",
                    "title": "Stripe Webhook Retry Spec",
                    "content": "Retries occur with exponential backoff up to 72 hours.",
                },
            ],
            actor="codex",
            auto_extract_from_messages=True,
        )
        assert sum_res["ok"] is True
        assert sum_res["skipped_count"] == 1
        assert sum_res["skipped_memories"][0]["action"] == "skipped_tombstoned"
        knowledge_saved = next(m for m in sum_res["saved_memories"] if m["category"] == "knowledge")
        loaded_knowledge = content_service.get_content(knowledge_saved["id"])
        assert loaded_knowledge["kind"] == "memory"

        # Existing conversation updated_at was preserved
        reloaded_conv = conversation_service.get_conversation(unlinked_conv["id"])
        assert reloaded_conv["updated_at"] == unlinked_conv["updated_at"]

        # Heuristic extraction did NOT create a second open task that breaks relay_project
        relayed_after_mem = handoff_service.relay_project(
            project_id=proj["id"],
            consumer="codex",
            context_mode="compressed", max_tokens=32000,
            sync_first=False,
        )
        assert relayed_after_mem["status"] == "relayed"
        assert relayed_after_mem["handoff"]["status"] == "awaiting_confirmation"
        assert relayed_after_mem["continuation"]["must_not_execute"] is True

        # 5. Hook endpoint implicitly triggers auto_summarize_memory without auto_summarize=True
        import hashlib
        from app.database import seed_data, db
        seed_data()
        cc_tok = "test-token-claude-code"
        with db() as conn:
            conn.execute(
                "UPDATE connections SET enabled = 1, paused = 0, token_hash = ? WHERE id = 'claude-code'",
                (hashlib.sha256(cc_tok.encode("utf-8")).hexdigest(),),
            )
        client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
        csrf_token = client.get("/api/session").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token, "Authorization": f"Bearer {cc_tok}"}
        hook_resp = client.post(
            "/api/connections/claude-code/hook",
            headers=headers,
            json={
                "event": "Stop",
                "session_id": "cc-implicit-hook-1",
                "cwd": str(repo_dir),
                "messages": [
                    {"role": "user", "content": "以后数据库迁移统一使用 Alembic，禁止手动改表。"},
                    {"role": "assistant", "content": "好的，已记录 Alembic 迁移规范。"},
                ],
            },
        )
        assert hook_resp.status_code == 200
        hook_json = hook_resp.json()
        assert hook_json.get("auto_memory") is not None
        assert hook_json["auto_memory"]["created_count"] >= 1

        # 6. quick_switch_provider without pre-existing model entity + rollback on failure
        mgr = management_service.service()
        anthropic_prov = mgr.save(
            "provider",
            {
                "name": "Anthropic Official",
                "protocol": "anthropic",
                "base_url": "https://api.anthropic.com/v1",
                "agent_ids": [],
            },
        )
        with pytest.raises(Exception, match="explicit model"):
            mgr.quick_switch_provider("claude-code", anthropic_prov["id"])
        model = mgr.save("model", {"name": "Chosen synthetic", "provider_id": anthropic_prov["id"], "model_id": "explicit-synthetic"})
        switched_cc = mgr.quick_switch_provider("claude-code", anthropic_prov["id"], model["id"])
        assert switched_cc["status"] == "planned"
        assert switched_cc["model_id"] == model["id"]

        # Attempting to switch codex (OpenAI-only) to an Anthropic provider fails and rolls back agent_ids
        before_prov = mgr.get("provider", anthropic_prov["id"])
        with pytest.raises( Exception ):
            mgr.quick_switch_provider("codex", anthropic_prov["id"])
        after_prov = mgr.get("provider", anthropic_prov["id"])
        assert "codex" not in (after_prov.get("agent_ids") or [])
        assert after_prov["version"] == before_prov["version"]

        # 7. sync_to_hosts partial failure (claude-code succeeds, cursor fails for provider)
        sync_partial = mgr.sync_to_hosts(
            "provider",
            anthropic_prov["id"],
            ["claude-code", "cursor"],
            operation="install", model_id=model["id"],
        )
        assert sync_partial["ok"] is False
        assert "claude-code" in sync_partial["planned"]
        assert "cursor" in sync_partial["failed"]
        after_sync_prov = mgr.get("provider", anthropic_prov["id"])
        assert "cursor" not in (after_sync_prov.get("agent_ids") or [])

    def test_sync_local_environment_and_project_batch_auto_summarize(self, tmp_path, monkeypatch):
        import json as _json
        import sqlite3 as _sqlite3
        from app.config import settings

        home_dir = tmp_path / "home"
        home_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "sync-local.db"))
        create_schema()

        # Create a simulated ~/.cc-switch/cc-switch.db with user_version=21
        cc_dir = home_dir / ".cc-switch"
        cc_dir.mkdir(parents=True, exist_ok=True)
        cc_db = cc_dir / "cc-switch.db"
        conn = _sqlite3.connect(cc_db)
        conn.executescript(
            """
            PRAGMA user_version = 21;
            CREATE TABLE providers (
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                settings_config TEXT NOT NULL,
                is_current BOOLEAN NOT NULL DEFAULT 0,
                sort_index INTEGER,
                meta TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (id, app_type)
            );
            CREATE TABLE mcp_servers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                server_config TEXT NOT NULL,
                enabled_claude BOOLEAN NOT NULL DEFAULT 0,
                enabled_codex BOOLEAN NOT NULL DEFAULT 0,
                enabled_gemini BOOLEAN NOT NULL DEFAULT 0,
                enabled_opencode BOOLEAN NOT NULL DEFAULT 0
            );
            CREATE TABLE prompts (
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                PRIMARY KEY (id, app_type)
            );
            CREATE TABLE skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                directory TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "p-deepseek",
                "claude",
                "DeepSeek CC",
                _json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                            "ANTHROPIC_AUTH_TOKEN": "sk-test-1234567890",
                            "ANTHROPIC_MODEL": "deepseek-chat",
                        }
                    }
                ),
                1,
                1,
                "{}",
            ),
        )
        conn.execute(
            "INSERT INTO mcp_servers VALUES (?, ?, ?, 1, 1, 0, 0)",
            (
                "node_repl",
                "node_repl",
                _json.dumps({"command": "npx", "args": ["-y", "mcp-node-repl"]}),
            ),
        )
        conn.execute(
            "INSERT INTO prompts VALUES (?, ?, ?, ?, 1)",
            ("pr-1", "claude", "Code Review Standard", "Always run pytest before concluding."),
        )
        conn.commit()
        conn.close()

        # Also create a local skill under ~/.claude/skills/my-skill/SKILL.md
        skill_dir = home_dir / ".claude" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# My Skill\nExample Authorization: Bearer sk-1234567890\n", encoding="utf-8")

        # Also create a local host MCP config (~/.cursor/mcp.json) with args + self-entry
        cursor_dir = home_dir / ".cursor"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        (cursor_dir / "mcp.json").write_text(
            _json.dumps(
                {
                    "mcpServers": {
                        "contextcore": {"command": "python3", "args": ["-m", "app.mcp_server"]},
                        "cursor-playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
                    }
                }
            ),
            encoding="utf-8",
        )

        from app.integrations.secret_store import SecretStore
        from app.services.management_service import set_secret_store

        class _MemVault(SecretStore):
            def __init__(self):
                self.items = {}

            def status(self):
                return {"available": True, "backend": "memory"}

            def put(self, secret: str) -> str:
                ref = f"keychain_{len(self.items) + 1:016x}"
                self.items[ref] = secret
                return ref

            def get(self, reference: str) -> str:
                return self.items[reference]

            def delete(self, reference: str) -> None:
                self.items.pop(reference, None)

        vault = _MemVault()
        set_secret_store(vault)
        try:
            client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
            csrf_token = client.get("/api/session").json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf_token}

            sync_resp = client.post("/api/manage/sync-local", headers=headers, json={})
            assert sync_resp.status_code == 200
            sync_data = sync_resp.json()
            assert sync_data["ok"] is True
            assert sync_data["total_providers"] >= 1
            assert sync_data["total_models"] >= 1
            assert sync_data["total_assets"] >= 4

            provs = client.get("/api/manage/providers").json()
            if isinstance(provs, dict):
                provs = provs["items"]
            ds_prov = next(p for p in provs if p["name"] == "DeepSeek CC")
            assert ds_prov["has_credential"] is True
            assert ds_prov["credential_reattach_required"] is False
            assert ds_prov["supported_endpoints"] == ["messages"]
            assert "claude-code" in (ds_prov.get("cc_switch_current_hosts") or [])

            # 1-click quick-switch on synced external provider checks explicit model selection (no 409 reattach error)
            qs_resp = client.post(
                "/api/manage/hosts/claude-code/quick-switch",
                headers=headers,
                json={"provider_id": ds_prov["id"]},
            )
            assert qs_resp.status_code == 422  # Imported catalog is not an explicit model selection.

            # Host MCP config was synced with args preserved, and 'contextcore' self-entry was skipped
            assets = client.get("/api/manage/assets").json()
            if isinstance(assets, dict):
                assets = assets["items"]
            asset_names = {a["name"]: a for a in assets}
            assert "contextcore" not in asset_names
            assert "cursor-playwright" in asset_names
            assert asset_names["cursor-playwright"]["config"]["args"] == ["-y", "@playwright/mcp@latest"]

            # Disabling an un-installed synced asset returns 200 disabled instead of 409
            qt_off = client.post(
                "/api/manage/hosts/cursor/quick-toggle-asset",
                headers=headers,
                json={"asset_id": asset_names["cursor-playwright"]["id"], "enabled": False},
            )
            assert qt_off.status_code == 409  # External MCP entry must not be claimed or removed.
        finally:
            set_secret_store(None)

        # Project-level auto-summarize without explicit conversation_id or messages
        proj = project_service.create_project(name="Batch Summarize Project")
        conversation_service.upsert_conversation(
            agent="codex",
            project_id=proj["id"],
            title="Database architecture session",
            external_id="codex:batch-1",
            messages=[
                {"role": "user", "content": "以后这个项目统一使用 PostgreSQL 16，禁止使用 MySQL。"},
                {"role": "assistant", "content": "已确认使用 PostgreSQL 16，下一步配置连接池。"},
            ],
        )
        sum_resp = client.post(
            "/api/processing/auto-summarize",
            headers=headers,
            json={"project_id": proj["id"], "actor": "user", "auto_extract_from_messages": True},
        )
        assert sum_resp.status_code == 200
        sum_data = sum_resp.json()
        assert sum_data["ok"] is True
        assert sum_data["created_count"] >= 1

    def test_tiered_memory_pin_consolidate_handbook_and_conflict_merge(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "tiered-mem.db"))
        create_schema()

        client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
        csrf_token = client.get("/api/session").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        proj = project_service.create_project(name="Tiered Memory Project")

        # 1. Create a global pinned preference and a project-scoped lesson, then pin the lesson
        global_pref = client.post(
            "/api/content",
            headers=headers,
            json={
                "title": "全局代码风格",
                "content": "始终使用中文回复，TypeScript 开启 strict。",
                "category": "preference",
                "scope": "global",
                "pinned": True,
            },
        ).json()
        assert global_pref["pinned"] is True
        assert global_pref["scope"] == "global"
        assert global_pref["project_id"] is None

        pitfall = client.post(
            "/api/content",
            headers=headers,
            json={
                "project_id": proj["id"],
                "title": "SQLite 并发锁注意事项",
                "content": "写操作必须使用 BEGIN IMMEDIATE 避免死锁。",
                "category": "lesson",
            },
        ).json()
        assert pitfall["pinned"] is False
        pin_res = client.post(
            f"/api/content/{pitfall['id']}/pin",
            headers=headers,
            json={"pinned": True, "expected_version": pitfall["version"]},
        )
        assert pin_res.status_code == 200
        assert pin_res.json()["pinned"] is True

        # 2. Verify assemble_context includes the pinned lesson in protected tier
        assembled = client.post(
            "/api/retrieval/assemble",
            headers=headers,
            json={
                "query": "数据库事务",
                "project_id": proj["id"],
                "budget_tokens": 8000,
                "context_epoch": "test-epoch",
                "consumer_id": "web",
            },
        ).json()
        protected_ids = {e["id"] for e in assembled["entries"] if e["tier"] == "protected"}
        assert pitfall["id"] in protected_ids
        assert global_pref["id"] in protected_ids

        # 3. Create near-duplicate memories and a noisy session filler, then run consolidate
        dup1 = client.post(
            "/api/content",
            headers=headers,
            json={
                "project_id": proj["id"],
                "title": "用户偏好与规范：使用 pnpm 管理依赖",
                "content": "统一使用 pnpm 安装依赖。",
                "category": "preference",
            },
        ).json()
        dup2 = client.post(
            "/api/content",
            headers=headers,
            json={
                "project_id": proj["id"],
                "title": "使用习惯与偏好：使用 pnpm 管理依赖",
                "content": "统一使用 pnpm 安装依赖。\n禁止使用 yarn。",
                "category": "preference",
            },
        ).json()
        filler = client.post(
            "/api/content",
            headers=headers,
            json={
                "project_id": proj["id"],
                "title": "会话进展提炼：日常闲聊",
                "content": "用户目标/提问：你好",
                "category": "background",
            },
        ).json()

        cons_res = client.post(
            "/api/content/consolidate",
            headers=headers,
            json={"project_id": proj["id"], "scope": "project"},
        ).json()
        assert cons_res["ok"] is True
        assert cons_res["merged_groups"] >= 1
        assert cons_res["archived_count"] >= 2  # 1 duplicate + 1 filler archived
        assert client.get(f"/api/content/{filler['id']}").json()["status"] == "historical"

        # 4. Export AGENTS.md / CLAUDE.md handbook
        hb_res = client.get(f"/api/content/handbook?project_id={proj['id']}&include_global=true")
        assert hb_res.status_code == 200
        hb = hb_res.json()
        assert "AGENTS.md / CLAUDE.md" in hb["markdown"]
        assert "📌 常驻核心规则" in hb["markdown"]
        assert "SQLite 并发锁注意事项" in hb["markdown"]
        assert "全局代码风格" in hb["markdown"]
        assert hb["counts"]["pinned"] >= 2

        # 5. Conflict resolution with 'merge' (Edit & Accept)
        conf = client.post(
            "/api/conflicts",
            headers=headers,
            json={
                "project_id": proj["id"],
                "existing_id": pitfall["id"],
                "reason": "新增了 WAL 模式的补充说明",
                "proposed": {
                    "title": "SQLite 并发与 WAL 规范",
                    "content": "写操作必须使用 BEGIN IMMEDIATE，并开启 WAL 模式。",
                    "category": "lesson",
                },
            },
        ).json()
        resolved = client.post(
            f"/api/conflicts/{conf['id']}/resolve",
            headers=headers,
            json={
                "resolution": "merge",
                "expected_version": 2,
                "merged_title": "SQLite 并发锁与 WAL 完整规范",
                "merged_content": "1. 写事务必须使用 BEGIN IMMEDIATE；\n2. 生产环境统一开启 WAL 模式。",
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["resolution"] == "merge"
        updated_pitfall = client.get(f"/api/content/{pitfall['id']}").json()
        assert updated_pitfall["title"] == "SQLite 并发锁与 WAL 完整规范"
        assert "WAL 模式" in updated_pitfall["content"]

        # Memories alone cannot manufacture a confirmed relay or a resume cursor.
        relay_res = client.post(
            "/api/handoffs/relay",
            headers=headers,
            json={"project_id": proj["id"], "consumer": "codex", "context_mode": "compressed", "sync_first": False},
        ).json()
        assert relay_res["status"] == "selection_required"
        assert relay_res["must_not_execute"] is True
        assert not relay_res.get("resume_from")

    def test_conversation_first_relay_live_memory_supersession_and_one_click_connect(self, tmp_path, monkeypatch):
        home_dir = tmp_path / "home"
        home_dir.mkdir(parents=True, exist_ok=True)
        launch_dir = tmp_path / "LaunchAgents"
        launch_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "conv-relay.db"))
        monkeypatch.setenv("CONTEXTCORE_LAUNCH_AGENTS_DIR", str(launch_dir))
        create_schema()

        proj = project_service.create_project(name="Auth Refactor", directories=[str(tmp_path / "auth-repo")])
        conv_cross = conversation_service.upsert_conversation(
            agent="claude-code",
            project_id=proj["id"],
            title="JWT Refresh Token 改造",
            external_id="cc:jwt-1",
            updated_at="2026-10-29T10:00:00Z",
            messages=[
                {"role": "user", "content": "请把登录鉴权改成双 Token 机制，Access Token 15 分钟过期。"},
                {"role": "assistant", "content": "已完成 JWT 签发逻辑，下一步需要实现 /api/auth/refresh 路由。"},
            ],
        )
        conv_same = conversation_service.upsert_conversation(
            agent="codex",
            project_id=proj["id"],
            title="Codex 本地调试会话",
            external_id="codex:local-2",
            updated_at="2026-10-29T09:00:00Z",
            messages=[
                {"role": "user", "content": "先跑一下现有的鉴权单测。"},
                {"role": "assistant", "content": "单测已全部通过。"},
            ],
        )
        conv_unlinked = conversation_service.upsert_conversation(
            agent="cursor",
            project_id=None,
            title="独立脚本探索",
            external_id="cursor:free-3",
            updated_at="2026-10-29T08:00:00Z",
            messages=[
                {"role": "user", "content": "写一个清理过期会话的 SQL 脚本。"},
                {"role": "assistant", "content": "已生成 DELETE FROM sessions WHERE expires_at < NOW() 脚本。"},
            ],
        )

        # 1. Conversation-first discovery lists cross-Agent, same-Agent, and unlinked conversations
        disc = handoff_service.discover_relay_conversations(consumer="codex", sync_first=False)
        assert disc["total_candidates"] == 3
        assert disc["candidates"][0]["conversation_id"] == conv_cross["id"]
        assert disc["candidates"][0]["is_cross_agent"] is True
        assert any(c["conversation_id"] == conv_same["id"] for c in disc["candidates"])
        assert any(c["conversation_id"] == conv_unlinked["id"] for c in disc["candidates"])

        # 2. Selecting candidate #1 confirms and loads conversation context in 1 step
        relayed = handoff_service.relay_conversation(
            conversation_id="1",
            consumer="codex",
            sync_first=False,
        )
        assert relayed["status"] == "loaded"
        assert relayed["must_not_execute"] is True
        assert relayed["continuation"]["must_not_execute"] is True
        assert "等待" in relayed["context_text"]
        assert relayed["selected_conversation"]["id"] == conv_cross["id"]
        assert "双 Token 机制" in relayed["context_text"]
        assert "/api/auth/refresh" in relayed["context_text"]

        # 3. Selecting an unlinked conversation also works in 1 step without project_id
        relayed_unlinked = handoff_service.relay_project(
            conversation_id=conv_unlinked["id"],
            consumer="codex",
            sync_first=False,
        )
        assert relayed_unlinked["status"] == "loaded"
        assert relayed_unlinked["must_not_execute"] is True
        assert "等待" in relayed_unlinked["context_text"]
        assert "清理过期会话" in relayed_unlinked["context_text"]

        # 4. Real-time MCP save_memory with cwd auto-project, automatic supersession, ambiguous conflict, and global scope
        auto_repo = tmp_path / "auto-cwd-repo"
        auto_repo.mkdir(parents=True, exist_ok=True)
        token = _as_agent("claude-code")
        try:
            saved_1 = mcp_server.save_memory(
                title="包管理器规范",
                content="当前项目统一使用 pnpm v9。",
                category="preference",
                cwd=str(auto_repo),
            )
            assert saved_1["action"] == "created"
            assert saved_1["status"] == "active"
            assert "💡 已自动记入续境记忆" in saved_1["summary_note"]
            auto_proj_id = saved_1["project_id"]
            assert auto_proj_id

            # Explicit update supersedes old memory to historical
            saved_2 = mcp_server.save_memory(
                title="包管理器规范",
                content="当前项目改为统一使用 bun，不再使用 pnpm。",
                category="preference",
                project_id=auto_proj_id,
            )
            assert saved_2["action"] == "updated"
            assert saved_2["superseded_id"] == saved_1["id"]
            assert content_service.get_content(saved_1["id"])["status"] == "historical"
            assert content_service.get_content(saved_2["id"])["status"] == "active"

            # Ambiguous update records a conflict in Inbox without overwriting
            saved_conflict = mcp_server.save_memory(
                title="包管理器规范",
                content="子模块可能仍需保留 npm 兼容。",
                category="preference",
                project_id=auto_proj_id,
                ambiguous=True,
            )
            assert saved_conflict["action"] == "conflict_recorded"
            assert saved_conflict["conflict_id"]

            # Global scope memory
            saved_global = mcp_server.save_memory(
                title="全局沟通语言",
                content="所有技术方案默认使用中文输出。",
                category="preference",
                scope="global",
                pinned=True,
            )
            assert saved_global["scope"] == "global"
            assert saved_global["project_id"] is None
            assert saved_global["pinned"] is True
        finally:
            authenticated_agent.reset(token)

        # 5. Agent-summarized user decision is accepted by _claim_supported_by_quote
        distilled = distillation_service.auto_summarize_memory(
            project_id=proj["id"],
            conversation_id=conv_cross["id"],
            memories=[
                {
                    "category": "decision",
                    "title": "双 Token 鉴权方案",
                    "content": "采用双 Token 机制，Access Token 设定为 15 分钟过期。",
                    "user_confirmed": True,
                }
            ],
            actor="claude-code",
            auto_extract_from_messages=False,
        )
        assert distilled["ok"] is True
        assert distilled["created_count"] == 1
        assert distilled["saved_memories"][0]["category"] == "decision"

        # 6. Settings autostart LaunchAgent install/remove & 1-click asset auto_apply.
        # Fake launchctl so register=True is exercised without loading a real user job.
        monkeypatch.setattr(
            "app.runtime.subprocess.run",
            lambda *args, **kwargs: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})(),
        )
        monkeypatch.setattr("app.runtime.is_launchd_registered", lambda *args, **kwargs: True)
        from types import SimpleNamespace
        from app import runtime as runtime_module
        monkeypatch.setattr(
            runtime_module,
            "sys",
            SimpleNamespace(**{**vars(runtime_module.sys), "platform": "darwin"}),
        )
        install_calls = {}
        real_install = runtime_module.install_autostart
        def recording_install(*args, **kwargs):
            install_calls["register"] = kwargs.get("register")
            return real_install(*args, **kwargs)
        monkeypatch.setattr("app.runtime.install_autostart", recording_install)
        client = TestClient(app, base_url="http://127.0.0.1:8420", client=("127.0.0.1", 50000))
        csrf_token = client.get("/api/session").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf_token}

        on_resp = client.patch(
            "/api/settings",
            headers=headers,
            json={"autostart": True, "install_launch_agent": True},
        )
        assert on_resp.status_code == 200
        assert on_resp.json()["autostart"] is True
        assert on_resp.json()["autostart_installed"] is True
        assert install_calls["register"] is True

        off_resp = client.patch(
            "/api/settings",
            headers=headers,
            json={"autostart": False, "install_launch_agent": True},
        )
        assert off_resp.status_code == 200
        assert off_resp.json()["autostart"] is False
        assert off_resp.json()["autostart_installed"] is False

        mgr = management_service.service()
        rule_asset = mgr.save(
            "asset",
            {
                "name": "Auto Apply Rule",
                "kind": "prompt",
                "content": "Always verify tests before finishing.",
                "agent_ids": [],
            },
        )
        auto_res = mgr.quick_toggle_asset("codex", rule_asset["id"], True, auto_apply=True)
        assert auto_res["status"] == "applied"
        assert auto_res["plan"]["status"] == "applied"

        # 7. Browser /api/retrieval/assemble binds consumer_id to 'web' regardless of caller-supplied value
        spoofed_pack = client.post(
            "/api/retrieval/assemble",
            headers=headers,
            json={
                "query": "双 Token",
                "project_id": proj["id"],
                "budget_tokens": 4000,
                "context_epoch": "web-epoch-spoof-check",
                "consumer_id": "codex",
            },
        ).json()
        assert spoofed_pack["consumer_id"] == "web"

        # 8. Whole-database backup via REST endpoint and CLI subcommand
        backup_resp = client.post("/api/settings/backup", headers=headers)
        assert backup_resp.status_code == 200
        backup_data = backup_resp.json()
        assert backup_data["ok"] is True
        assert backup_data["integrity"] == "ok"
        assert Path(backup_data["backup_path"]).is_file()
        assert backup_data["size_bytes"] > 0
        assert len(backup_data["sha256"]) == 64

        from app import cli
        cli_backup_file = tmp_path / "cli-backup.db"
        assert cli.main(["backup", "--output", str(cli_backup_file)]) == 0
        assert cli_backup_file.is_file()

        # 9. One-click connect merges newly discovered suggested_paths into an already-enabled connection
        from app.services import connection_service
        ag_desktop = home_dir / ".gemini" / "antigravity" / "brain"
        ag_ide = home_dir / ".gemini" / "antigravity-ide" / "brain"
        ag_desktop.mkdir(parents=True, exist_ok=True)
        connection_service.discover_connections()
        connection_service.patch_connection(
            connection_id="antigravity",
            enabled=True,
            paused=False,
            paths=[str(ag_desktop)],
            confirmed=True,
        )
        ag_ide.mkdir(parents=True, exist_ok=True)
        occ_resp = client.post("/api/connections/antigravity/one-click-connect", headers=headers, json={})
        assert occ_resp.status_code == 200
        ag_conn = connection_service.get_connection("antigravity")
        assert str(ag_desktop.resolve()) in [str(Path(p).expanduser().resolve()) for p in ag_conn["paths"]]
        assert str(ag_ide.resolve()) in [str(Path(p).expanduser().resolve()) for p in ag_conn["paths"]]

        # 10. Listing relay history requests background refresh; it never starts Codex.
        import json
        codex_dir = tmp_path / "codex-empty-rollouts"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "session_index.jsonl").write_text(
            json.dumps({"id": "app-srv-1", "thread_name": "App Server Thread", "updated_at": "2026-10-29T11:00:00Z"}) + "\n",
            encoding="utf-8",
        )
        connection_service.patch_connection(
            connection_id="codex",
            enabled=True,
            paused=False,
            paths=[str(codex_dir)],
            confirmed=True,
        )
        app_server_calls = []

        def fake_app_server(roots, allow_execution=False):
            app_server_calls.append((roots, allow_execution))
            return [
                {
                    "external_id": "codex:app-srv-1",
                    "session_id": "app-srv-1",
                    "title": "App Server Thread",
                    "cwd": str(tmp_path / "auth-repo"),
                    "started_at": "2026-10-29T11:00:00Z",
                    "updated_at": "2026-10-29T11:05:00Z",
                    "source_path": f"{codex_dir}/session_index.jsonl",
                    "messages": [
                        {"role": "user", "content": "从 Codex App Server 恢复这段对话"},
                        {"role": "assistant", "content": "已通过 App Server 同步该对话。"},
                    ],
                }
            ]

        monkeypatch.setattr("app.transport.fetch_codex_sessions_sync", fake_app_server)
        sync_res = handoff_service.sync_recent_agent_chats_for_relay()
        assert not app_server_calls
        assert any(s.get("agent") == "codex" and s.get("status") == "pending" for s in sync_res)
        assert not any(c.get("external_id") == "codex:app-srv-1" for c in conversation_service.list_conversations())




