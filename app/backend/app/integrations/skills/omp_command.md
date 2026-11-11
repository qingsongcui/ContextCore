---
description: ContextCore cross-agent conversation relay and real-time implicit auto-memory manager
tools:
  - contextcore:discover_relay_conversations
  - contextcore:relay_conversation
  - contextcore:list_relay_conversations
  - contextcore:relay_project
  - contextcore:list_relay_projects
  - contextcore:assemble_context
  - contextcore:ack_context
  - contextcore:auto_summarize_memory
  - contextcore:save_memory
  - contextcore:edit_memory
  - contextcore:update_task_progress
  - contextcore:search_memory
  - contextcore:start_handoff
  - contextcore:confirm_handoff
  - contextcore:read_handoff
  - contextcore:read_evidence
---

# /contextcore Command for Oh My Pi

Extension auto events stay silent and must not rewrite the user prompt. Only this command and MCP tools may return ContextCore results.

## 1. Cross-Agent Conversation Relay

1. When the user types `/contextcore` or says "接力", "接着做", or "continue" without specifying a conversation, call `discover_relay_conversations(cwd="<current_working_directory>")` or `relay_conversation(cwd="<current_working_directory>")` (or `list_relay_conversations(cwd="<current_working_directory>")`), always passing the current project directory `cwd` so conversations are scoped to the current project. It syncs recent chat histories across all authorized agents (`claude-code`, `codex`, `cursor`, `antigravity`, `omp`, `opencode`) and returns a numbered list of recent conversation records (`question_prompt` and `conversation_candidates`).
2. Present the numbered conversation list to the user and ask which conversation record to continue.
3. Once the user replies with a conversation number (e.g. `1`) or `conversation_id`, call `relay_conversation(conversation_id=..., cwd="<current_working_directory>")`.
4. `relay_conversation` loads that selected conversation's detailed context (recent complete turns + six-element summary + active project memories).
5. **Report to the user that loading is complete (`✅ 已加载对话上下文：《<标题>》`)**, summarize the previous goal and latest progress in 2–3 concise lines, and **stop to wait for the user's instruction on what to do next** (unless the user already included a specific task instruction alongside their selection). Do not redo `do_not_redo`.

## 2. Local CLI / HTTP Fallback (When MCP Tools Are Not Loaded in Current Session)

If `discover_relay_conversations`, `relay_conversation`, `assemble_context`, or `start_handoff` are not visible in the current session's MCP tool list, **NEVER stop and claim that handoff tools are missing**. Immediately query the local ContextCore loopback API via shell (passing `cwd=$PWD` so only the current project's conversations are returned):

1. **Discover & list recent conversations for the current project across agents**:
   ```bash
   curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "consumer=omp" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "consumer=omp" --data-urlencode "cwd=$PWD"
   ```
   Display `question_prompt` / `conversation_candidates` to the user and ask which conversation number (`#1`, `#2`, ...) to continue.
2. **Confirm & load the selected conversation (`<N>` or `<conversation_id>`)**:
   ```bash
   curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=omp" --data-urlencode "cwd=$PWD" || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" --data-urlencode "conversation_id=<N>" --data-urlencode "consumer=omp" --data-urlencode "cwd=$PWD"
   ```
   Read `context_text` and `continuation`, report `✅ 已加载对话上下文：《<标题>》` with a 2–3 line summary of the goal and latest progress, and wait for the user's next instruction before executing tasks.

## 3. Real-Time Implicit Memory Saving

During normal conversations, automatically and implicitly call `save_memory` (passing `cwd` or `scope="global"`) or `auto_summarize_memory` in the background whenever user preferences (`preference`), project background (`background`), architectural decisions (`decision`), task progress (`task`), or debugging pitfalls/lessons (`knowledge`) emerge, without waiting for the user to ask "remember this". Append a concise note at the end of your reply: `💡 已自动记入续境记忆：<记忆标题>`.
