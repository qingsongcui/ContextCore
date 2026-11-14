---
name: contextcore
description: "ContextCore (续境) cross-agent working memory closed loop and conversation relay for OpenCode: proactively bootstrap project memory, maintain working tasks with honest citations, and continue prior conversations across agents."
---

# ContextCore Working Memory & Relay Command for OpenCode

The OpenCode plugin captures events silently and does not inject noisy context into chat. Use ContextCore MCP tools for both the **Daily Working Memory Closed Loop** and **Explicit `/contextcore` Relay**.

## 1. Daily Working Memory Closed Loop (`日常工作记忆闭环`)

1. **Proactive Scoped Read (`收到任务先按需查`)**:
   - Before starting substantive coding, debugging, refactoring, or design work in a project — or when switching tasks/projects/Agents, when the user mentions `"上次"` / `"按之前的"`, hitting a potential pitfall, or recovering after context compaction — call `bootstrap_working_memory(cwd="<current_working_directory>", query="<current_task_summary>")`.
   - Skip for trivial 1-turn Q&A (e.g. `"现在有几个分支"`). Use `search_memory` / `read_memory` for deeper lookups.
2. **Read ≠ Execution Permission (`尊重 must_not_execute`)**:
   - If `bootstrap_working_memory` returns `must_not_execute=True` (`execution_mode="discussion_only"` or unconfirmed handoff), OR if the user is only discussing a plan/architecture, **stay strictly in discussion mode** — NEVER edit code until explicitly authorized.
   - Ask the user when `task_selection_required=True` or `pending_conflicts` exist. Verify `applicability` conditions before applying lessons.
3. **Restrained 4-Event Writes (`克制写入，零噪音`)**:
   - **(a) User explicit decision / preference / correction** -> `save_memory(cwd=..., category="decision"|"preference", title=..., content=..., is_user_correction=True|False)`.
   - **(b) Task stage completion (verified), blocker, failed attempt, or one-off constraint (`"这次先别..."`)** -> `upsert_working_task(cwd=..., title=..., goal=..., add_completed_step=..., current_state=..., add_temp_constraint=..., add_failed_attempt=..., blockers=..., next_steps=..., execution_mode=...)`.
   - **(c) Validated reusable conditional lesson** -> `promote_task_lesson(task_id=..., title=..., content=..., applicability={"when": "...", "env": "..."})`.
   - **(d) User marks an old rule outdated** -> `mark_memory_outdated(memory_id=..., reason=...)`.
   - **Forbidden**: Never write unconfirmed AI proposals, unverified guesses, or raw command/traceback logs.
4. **Honest Citation & Feedback (`真实引用`)**:
   - Pass `cited_memory_ids=[...]` to `upsert_working_task` or call `cite_memory(memory_ids=[...], reason=...)` when loaded memories genuinely guide your work, and briefly inform the user of referenced constraints/progress.

## 2. Explicit `/contextcore` Cross-Agent Relay (`跨 Agent 对话接力`)

1. When the user invokes `/contextcore` or says `"接力"`, `"接着做"`, or `"continue"` without specifying a conversation, call `discover_relay_conversations(cwd="<current_working_directory>")` or `relay_conversation(cwd="<current_working_directory>")` (or `relay_project` for project-level relay).
2. Present the numbered conversation list — **every row MUST show `title` (对话主题) + `brief_summary` (对话简介：聊了什么、最后停在哪) + `agent` + `updated_at` + `conversation_id`/`external_id` (NEVER omit `brief_summary`)** — and ask which conversation number to continue.
3. Once selected (`1` or `conversation_id`), call `relay_conversation(conversation_id=..., cwd="<current_working_directory>")`.
4. When `status="loaded"` (`must_not_execute=True`) or `status="sync_pending"`, report `✅ 已加载对话上下文：《<标题>》` (noting if background history refresh is still pending) with a 2–3 line summary and **stop to wait for the user's concrete next instruction**. Only execute immediately when `must_not_execute=False` (`status="confirmed"` / `continuation.status="ready"`). Do not redo completed work.

## 3. Local CLI / HTTP Fallback (When MCP Tools Are Not Loaded Yet)

If MCP tools are not visible in the current session, NEVER stop and claim tools are missing — query the local ContextCore loopback API:

```bash
# Discover recent conversations for current project
curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "consumer=opencode" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "consumer=opencode" --data-urlencode "cwd=$PWD"

# Load selected conversation (<N> or <conversation_id>)
curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=opencode" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=opencode" --data-urlencode "cwd=$PWD"
```
