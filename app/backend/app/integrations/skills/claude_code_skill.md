# ContextCore Handoff & Auto-Memory Skill

Use this skill when the user invokes `/contextcore` or says to continue/relay work from an earlier conversation ("接力", "接着做", "continue"), or implicitly during normal conversations to summarize and persist project memories.

ContextCore hooks stay silent. Call MCP tools; do not expect auto-injected context.

## 1. Cross-Agent Conversation Relay (`跨 Agent 对话接力`)

1. When the user types `/contextcore` or says "接力", "接着做", or "continue" without specifying a conversation, call `discover_relay_conversations(cwd="<current_working_directory>")` or `relay_conversation(cwd="<current_working_directory>")` (or `list_relay_conversations(cwd="<current_working_directory>")`), always passing the current project directory `cwd` so conversations are scoped to the current project. It automatically syncs recent chat histories across all authorized agents (`claude-code`, `codex`, `cursor`, `antigravity`, `omp`, `opencode`) and returns a numbered candidate list of recent conversation records (`question_prompt` and `conversation_candidates`, showing `#序号 · [来源Agent] · 【所属项目】 · 时间 · 对话标题 — 简短摘要`).
2. Present the numbered conversation list to the user and ask which conversation record they want to continue.
3. Once the user replies with a conversation number (e.g. `1`) or `conversation_id`, call `relay_conversation(conversation_id=..., cwd="<current_working_directory>")`.
4. `relay_conversation` loads **that selected conversation's detailed context (recent complete message turns + six-element summary + active project memories/constraints)**, automatically truncating older turns if a single conversation exceeds the token budget.
5. **Report to the user that loading is complete (`✅ 已加载对话上下文：《<标题>》`)**, summarize the previous goal and latest progress in 2–3 concise lines, and **stop to wait for the user's instruction on what to do next** (unless the user already included a specific task instruction alongside their selection, in which case execute that instruction). Do not redo completed work (`do_not_redo`) or reuse historical decisions.

## 2. Local CLI / HTTP Fallback (When MCP Tools Are Not Loaded in Current Session)

If `discover_relay_conversations`, `relay_conversation`, `assemble_context`, or `start_handoff` are not visible in the current session's MCP tool list (e.g. the user has not restarted Claude Code after connecting), **NEVER stop and claim that handoff tools are missing**. Immediately query the local ContextCore loopback API via shell (passing `cwd=$PWD` so only the current project's conversations are returned):

1. **Discover & list recent conversations for the current project across agents**:
   ```bash
   curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD"
   ```
   Display `question_prompt` / `conversation_candidates` to the user and ask which conversation number (`#1`, `#2`, ...) to continue.
2. **Confirm & load the selected conversation (`<N>` or `<conversation_id>`)**:
   ```bash
   curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD"
   ```
   Read `context_text` and `continuation`, report `✅ 已加载对话上下文：《<标题>》` with a 2–3 line summary of the goal and latest progress, and wait for the user's next instruction before executing tasks.

## 3. Real-Time Implicit Memory Saving (`实时顺手记记忆`)

During normal conversations, do NOT wait for the user to explicitly say "remember this". Whenever any of the following signals emerge in dialogue, automatically and implicitly call `save_memory` (passing `cwd` to auto-match/create the project, or `scope="global"` for universal preferences) or `auto_summarize_memory` in the background:
- **User Preferences (`preference`)**: coding style, language/framework choices, prohibited operations, workflow habits.
- **Project Background (`background`)**: domain context, repository layout, environment setup.
- **Architectural Decisions (`decision`)**: technical trade-offs, schema/API contracts, chosen solutions.
- **Task Progress (`task`)**: milestone completion, blockers, next steps (or call `update_task_progress`).
- **Pitfalls & Lessons (`knowledge` / `lesson`)**: root causes of bugs, environment gotchas, verified workarounds.

After implicitly saving or summarizing memory in the background, append a brief, non-intrusive line at the end of your reply: `💡 已自动记入续境记忆：<记忆标题>`.
