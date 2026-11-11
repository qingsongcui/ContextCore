---
name: contextcore-handoff
description: ContextCore cross-agent conversation relay and real-time implicit auto-memory saving. Use when continuing work from an earlier conversation or during ongoing conversations to automatically persist memories.
tools:
  - discover_relay_conversations
  - relay_conversation
  - list_relay_conversations
  - relay_project
  - list_relay_projects
  - assemble_context
  - ack_context
  - auto_summarize_memory
  - save_memory
  - edit_memory
  - update_task_progress
  - search_memory
  - start_handoff
  - confirm_handoff
  - read_handoff
  - read_evidence
---

# ContextCore Handoff & Auto-Memory Skill for Codex

ContextCore does not auto-inject memory. Lifecycle hooks only report events. Call MCP tools when the user asks to continue or relay, and implicitly save memories during normal conversations.

## 1. Cross-Agent Conversation Relay (user types `/contextcore` or says 接力 / 接着做 / continue)

1. When the user asks to relay ("接力", "接着做", "continue") without specifying a conversation, call `discover_relay_conversations(cwd="<current_working_directory>")` or `relay_conversation(cwd="<current_working_directory>")` (or `list_relay_conversations(cwd="<current_working_directory>")`), always passing the current project directory `cwd` so conversations are scoped to the current project. It syncs recent chat histories across all authorized agents (`claude-code`, `codex`, `cursor`, `antigravity`, `omp`, `opencode`) and returns a numbered list of recent conversation records (`question_prompt` and `conversation_candidates`) for the current project.
2. Present the numbered conversation list to the user and ask which conversation number to continue.
3. Once the user replies with a conversation number (e.g. `1`) or `conversation_id`, call `relay_conversation(conversation_id=..., cwd="<current_working_directory>")`.
4. `relay_conversation` loads that selected conversation's detailed context (recent complete message turns + six-element summary + active project memories/constraints, auto-truncating older turns if over budget).
5. **Report to the user that loading is complete (`✅ 已加载对话上下文：《<标题>》`)**, summarize the previous goal and latest progress in 2–3 concise lines, and **stop to wait for the user's instruction on what to do next** (unless the user already included a specific task instruction alongside their selection, in which case execute that instruction). Do not redo `do_not_redo` or use `excluded_historical_decisions`.

## 2. Local CLI / HTTP Fallback (When MCP Tools Are Not Loaded in Current Session)

If `discover_relay_conversations`, `relay_conversation`, `assemble_context`, or `start_handoff` are not visible in the current session's MCP tool list (e.g. the user has not restarted Codex after connecting), **NEVER stop and claim that handoff tools are missing**. Immediately query the local ContextCore loopback API via shell (passing `cwd=$PWD` so only the current project's conversations are returned):

1. **Discover & list recent conversations for the current project across agents**:
   ```bash
   curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "consumer=codex" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "consumer=codex" --data-urlencode "cwd=$PWD"
   ```
   Display `question_prompt` / `conversation_candidates` to the user and ask which conversation number (`#1`, `#2`, ...) to continue.
2. **Confirm & load the selected conversation (`<N>` or `<conversation_id>`)**:
   ```bash
   curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=codex" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=codex" --data-urlencode "cwd=$PWD"
   ```
   Read `context_text` and `continuation`, report `✅ 已加载对话上下文：《<标题>》` with a 2–3 line summary of the goal and latest progress, and wait for the user's next instruction before executing tasks.

## 3. Real-Time Implicit Memory Saving (实时顺手记记忆)

During normal conversations, automatically and implicitly call `save_memory` (passing `cwd` or `scope="global"`) or `auto_summarize_memory` whenever you discover user preferences (`preference`), project background (`background`), architectural decisions (`decision`), task progress/completions (`task`), or debugging pitfalls/lessons (`knowledge`), without waiting for the user to ask "remember this". Append a concise note at the end of your reply: `💡 已自动记入续境记忆：<记忆标题>`.
