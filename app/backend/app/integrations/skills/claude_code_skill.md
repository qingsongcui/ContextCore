---
name: contextcore
description: "ContextCore working memory and conversation relay for Claude Code. Use before substantive project work to retrieve applicable memories, save lasting decisions, and cite memories used. Activate on /contextcore or a request to continue a previous conversation."
---

# ContextCore Working Memory and Relay Skill for Claude Code

Use ContextCore MCP tools for daily working memory and explicit conversation relay. Lifecycle hooks capture events silently without rewriting messages.

## Conversation language

- Use the language explicitly requested for the reply. Otherwise, follow the latest substantive user message. A bare command, conversation ID, number, or code snippet inherits the language already used in the conversation.
- Use `language="en"` for English and `language="zh"` for Chinese when calling localized MCP tools or relay endpoints. For other reply languages, request English tool feedback and write the user-facing reply in the requested language.
- Match status updates, selectors, headings, and summaries to the reply language. Give one version unless the user asks for a bilingual reply. The language of stored memories, raw transcripts, and tool results does not set the reply language. Preserve commands, IDs, code, fixed identifiers, and any verbatim quotes. Summarize source material in the reply language without modifying the original.

## 1. Daily working memory

1. **Read before starting project work**
   - Call `bootstrap_working_memory(cwd="<current_working_directory>", query="<current_task_summary>", language=...)` before substantive coding, debugging, refactoring, or design, when switching tasks, projects, or agents, when the user refers to prior work, when checking for known pitfalls, or when recovering after context compaction.
   - Set `new_task=true` for clearly separate work to load project and personal rules without the previous task's temporary constraints. For continuation or an unclear task relationship, leave the default and honor task selection.
   - Skip trivial one-turn questions, such as asking for the current Git branch. Use `search_memory` or `read_memory` for a deeper lookup.
2. **Respect execution gates**
   - When `must_not_execute=true`, the handoff is unconfirmed, or the user is only discussing a plan, stay in discussion mode until the user explicitly authorizes execution.
   - Ask the user to resolve `task_selection_required=true` or non-empty `pending_conflicts` before proceeding. Verify each lesson's `applicability` and any `applicability_warning` against the current environment and version.
3. **Write when there is something durable or actionable**
   - An explicit user decision, lasting preference, or correction goes to `save_memory(cwd=..., category="decision"|"preference", title=..., content=..., is_user_correction=...)`. Use global scope only for cross-project preferences. Take these actions during ordinary work without waiting for the user to ask you to remember.
   - Verified task progress, blockers, failed attempts, and one-off constraints go to `upsert_working_task(cwd=..., title=..., goal=..., add_completed_step=..., current_state=..., add_temp_constraint=..., add_failed_attempt=..., blockers=..., next_steps=..., execution_mode=...)`. Keep one-off constraints out of long-term memory.
   - A verified, reusable lesson goes to `promote_task_lesson(task_id=..., title=..., content=..., applicability={"when": "...", "env": "..."})`.
   - A rule the user declares obsolete goes to `mark_memory_outdated(memory_id=..., reason=...)`.
   - Keep unconfirmed suggestions, unverified guesses, and raw command or traceback logs out of memory.
4. **Cite memory when it changes your work**
   - Pass `cited_memory_ids=[...]` to `upsert_working_task` or call `cite_memory(memory_ids=[...], reason=...)` for previously retrieved memories that materially guide your actions or answer.
   - Briefly describe the applicable rule or update in the reply language after the tool succeeds. For a final-only deliverable, leave the citation in the tool record.

## 2. Explicit conversation relay

1. Treat `/contextcore` as an explicit relay invocation. Natural-language aliases include `continue the previous conversation`, `relay`, `接力`, `接着做`, and `接棒`. Without a specific conversation, call `discover_relay_conversations(cwd=..., language=...)` or `relay_conversation(cwd=..., language=...)`. For an explicitly requested project-level relay, use `relay_project(language=...)`.
2. Show a numbered candidate list and ask which conversation to continue. Each row includes the title, brief summary of the work and stopping point, source agent, last update time, and conversation ID or native external ID. Use the corresponding `title`, `brief_summary`, `agent`, `updated_at`, and `conversation_id` or `external_id` fields. Use `next_offset` for additional results. Write selector labels and summaries in the reply language. If a title is untitled or in another language, provide a short descriptive label for the selector so the user can still identify it.
3. Resolve the user's selection to the stable ID and call `relay_conversation(conversation_id=..., cwd=..., language=...)`. Pass a concrete task in `query` only when the user supplied one.
4. If `transcript_paging.complete` is false or `coverage.omitted_messages > 0`, start at `read_conversation_messages(conversation_id=..., offset=0, limit=30)` and follow `next_offset` until it is null. Read the full conversation before saying it is complete.
5. Compact the full conversation in context. Include the goal, work completed, affected files or commands, key decisions and corrections, what was verified, open issues, and the stopping point. Retain the final one or two turns verbatim in context. Report the readback and message count in the reply language. Present the concise recap without a formal handoff card.
6. If `status="sync_pending"`, retry once the history refresh completes. When `status="loaded"` or `must_not_execute=true`, recap the available context and wait for the user's concrete next instruction. Continue execution only when `must_not_execute=false` and the selected relay is confirmed or `continuation.status="ready"`.

## 3. Local HTTP fallback

If the host hasn't loaded the MCP tools yet, use the local loopback relay API. Set `relay_language` for the reply. Use the same project, selection, and execution gates as the MCP workflow.

```bash
relay_language=en  # Set to zh for Chinese replies.

# Discover conversations for the current project.
curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" \
  --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD" --data-urlencode "language=$relay_language" \
  || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" \
    --data-urlencode "consumer=claude-code" --data-urlencode "cwd=$PWD" --data-urlencode "language=$relay_language"

# Resolve the selection to its conversation ID before running this command.
curl -sS -G "http://127.0.0.1:8420/api/handoffs/relay/conversation" \
  --data-urlencode "conversation_id=<conversation_id>" --data-urlencode "consumer=claude-code" \
  --data-urlencode "cwd=$PWD" --data-urlencode "language=$relay_language" \
  || curl -sS -G "http://127.0.0.1:18420/api/handoffs/relay/conversation" \
    --data-urlencode "conversation_id=<conversation_id>" --data-urlencode "consumer=claude-code" \
    --data-urlencode "cwd=$PWD" --data-urlencode "language=$relay_language"

# Start at offset=0 and follow next_offset until it is null.
curl -sS "http://127.0.0.1:8420/api/conversations/<conversation_id>/messages?offset=0&limit=30" \
  || curl -sS "http://127.0.0.1:18420/api/conversations/<conversation_id>/messages?offset=0&limit=30"
```
