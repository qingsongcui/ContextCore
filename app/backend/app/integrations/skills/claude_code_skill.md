---
name: contextcore
description: "ContextCore (续境) 跨 Agent 工作记忆闭环与对话接力技能。日常开发、切换任务或恢复上下文前主动调用 bootstrap_working_memory 按需查记忆；推进中用 upsert_working_task + cite_memory 维护短期任务与真实引用；用户输入 /contextcore、接力、接着做、continue 时跨 Agent 接力历史对话。"
---

# ContextCore Working Memory & Handoff Skill (Claude Code)

ContextCore hooks stay silent and never auto-inject noisy dumps. Drive both **Daily Working Memory Closed Loop** and **Explicit `/contextcore` Relay** via MCP tools.

## 1. Daily Working Memory Closed Loop (`日常工作记忆闭环`)

1. **Proactive Scoped Read (`收到任务先按需查`)**:
   - Before starting substantive coding, debugging, refactoring, or design work in a project — or when switching tasks/projects/Agents, when the user mentions `"上次"` / `"按之前的"`, hitting a potential pitfall, or recovering after context compaction — call `bootstrap_working_memory(cwd="<current_working_directory>", query="<current_task_summary>")`.
   - Skip calling it for trivial 1-turn Q&A (e.g. `"现在有几个分支"`, `"git status"`).
   - Need deeper lookup on a specific topic? Call `search_memory(query=..., project_id=...)` or `read_memory(id=...)`.
2. **Read ≠ Execution Permission (`读取不等于授权改代码`)**:
   - If `bootstrap_working_memory` returns `must_not_execute=True` (`execution_mode="discussion_only"` or unconfirmed handoff), OR if the user is only discussing a plan/architecture, **stay strictly in discussion mode** — NEVER edit files until the user explicitly authorizes code changes.
   - If `task_selection_required=True` (multiple active tasks) or `pending_conflicts` is non-empty, ask the user before assuming one.
   - For lessons with `applicability` / `applicability_warning`, verify environment/version match before applying.
3. **Restrained 4-Event Writes (`克制写入，零噪音`)**:
   - **(a) User explicit decision / preference / correction**: Call `save_memory(cwd=..., category="decision"|"preference", title=..., content=..., is_user_correction=True|False)` (use `scope="global"` only for cross-project personal preferences).
   - **(b) Short-term task progress / blocker / failed attempt / one-off constraint (`"这次先别..."`)**: Call `upsert_working_task(cwd=..., title=..., goal=..., add_completed_step=..., current_state=..., add_temp_constraint=..., add_failed_attempt=..., blockers=..., next_steps=..., execution_mode="discussion_only"|"approved")`. NEVER store `"这次先别..."` or transient progress in long-term `save_memory`.
   - **(c) Validated reusable conditional lesson**: Call `promote_task_lesson(task_id=..., title=..., content=..., applicability={"when": "...", "env": "..."})`.
   - **(d) User declares an old rule obsolete**: Call `mark_memory_outdated(memory_id=..., reason=...)`.
   - **Strictly Forbidden**: Never save unconfirmed AI proposals (`"我建议..."`), unverified debugging guesses (`"可能是因为..."`), or raw command/traceback logs.
4. **Honest Citation & Lightweight Feedback (`真实引用与反馈`)**:
   - When loaded memories genuinely guide your code or decisions, pass `cited_memory_ids=[...]` to `upsert_working_task(...)` or call `cite_memory(memory_ids=[...], reason=..., task_id=...)`.
   - Briefly mention referenced constraints/task state to the user (e.g. `💡 已参考项目约束：<标题>` / `💡 已更新续境记忆：<标题>`).

## 2. Explicit `/contextcore` Cross-Agent Relay (`跨 Agent 对话接力`)

1. When the user types `/contextcore` or says `"接力"`, `"接着做"`, or `"continue"` without specifying a conversation, call `discover_relay_conversations(cwd="<current_working_directory>")` or `relay_conversation(cwd="<current_working_directory>")` (or `relay_project` for project-level relay), always passing `cwd`.
2. Present the numbered candidate list — **every row MUST show `#序号 · [来源Agent] · 时间 · 对话主题 (title) + 对话简介 (brief_summary：聊了什么、停在哪) · ID / 原生会话ID` (NEVER omit `brief_summary`)** — and ask which conversation to continue.
3. Once the user replies with a number (e.g. `1`) or `conversation_id`, call `relay_conversation(conversation_id=..., cwd="<current_working_directory>")`.
4. If `transcript_paging.complete` is `false` (or `coverage.omitted_messages > 0`), **immediately page `read_conversation_messages(conversation_id=..., offset=..., limit=30)` sequentially from `offset=0` until `next_offset` is `null`** so you read the ENTIRE conversation from turn 0 to the end (do not stop with only the tail).
5. Compact the entire conversation you just read in context (as in `/compact`: goal & background, full progression & modified files/commands, key decisions & user corrections, verified vs unverified/pitfalls, current stopping point, and the last 1–2 verbatim turns). Present `✅ 已读完并压缩完整上下文：《<标题>》（共 <total> 条消息）` with that clean compacted context — **NEVER present a handoff card / six-element card / missing-handoff notice**.
6. If `status="loaded"` (`must_not_execute=True`) or `status="sync_pending"`, stop and wait for the user's concrete next instruction (noting if background history refresh is still pending). Only execute immediately when `must_not_execute=False` (`status="confirmed"` / `continuation.status="ready"`).

## 3. Local CLI / HTTP Fallback (When MCP Tools Are Not Loaded Yet)

If MCP tools are not yet visible in the current session (e.g. before restarting Claude Code), NEVER claim handoff tools are missing — query the local ContextCore loopback API (`$PWD`):

```bash
# List candidate conversations
curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD"

# Load selected conversation (<N> or <conversation_id>)
curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD"

# Page full transcript from offset=0, 30, ... until all `total` messages are read, then compact context
curl -sS "http://127.0.0.1:8420/api/conversations/<conversation_id>/messages?offset=0&limit=30" || curl -sS "http://127.0.0.1:18420/api/conversations/<conversation_id>/messages?offset=0&limit=30"
```
