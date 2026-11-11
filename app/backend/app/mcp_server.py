import json
from typing import Any
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import StrictBool

from app.services import (
    content_service,
    project_service,
    handoff_service,
    conflict_service,
    connection_service,
)
from app.database import db
from app.security import authenticated_agent


def _actor() -> str:
    agent_id = authenticated_agent.get()
    if agent_id not in handoff_service.ALLOWED_TARGET_AGENTS:
        raise ValueError("Authenticated Agent identity is required")
    return agent_id


def _observe(capability: str, tool_name: str) -> None:
    agent_id = authenticated_agent.get()
    if agent_id:
        from app.services.host_registry import record_observation
        record_observation(agent_id, capability, detail={"tool_name": tool_name, "transport": "mcp"})

contextcore_mcp = FastMCP(
    "ContextCore",
    instructions=(
        "ContextCore is the local background context base for desktop agents. "
        "Context is opt-in: host lifecycle hooks do not inject memory or handoff cards. "
        "When the user types `/contextcore` or asks to continue/relay ('接力', '接着做', 'continue'), "
        "call `relay_conversation()` (or `discover_relay_conversations()` / `list_relay_conversations()`). "

        "If no conversation_id is specified, it syncs recent chat histories across all authorized agents and returns a numbered list "
        "of recent conversation records (`#index · [agent] · project · time · title — summary`); present this list to the user. "
        "Once the user replies with a conversation number (e.g. '1') or `conversation_id`, call `relay_conversation(conversation_id=..., cwd=...)`. "
        "It loads that selected conversation's detailed context (recent complete turns + six-element summary + active project memories, auto-truncating older turns if over budget). "
        "When the user only selects a number/ID without a concrete task instruction, `relay_conversation` returns `status='loaded'` (`must_not_execute=True`): "
        "report `✅ 已加载对话上下文：《<标题>》` with a brief 2-3 line summary of the goal and latest progress, and stop to wait for the user's specific next instruction. "
        "Only if the user explicitly includes a concrete task instruction alongside their selection (`query`) does it return `status='ready'` (`must_not_execute=False`) to execute immediately. "
        "During normal conversations, implicitly and automatically call `save_memory` (passing `cwd` or `scope='global'`) "
        "or `auto_summarize_memory` whenever you learn stable user preferences, project background, architectural decisions, "
        "task progress/completions, or debugging pitfalls/lessons, without waiting for the user to ask you to remember, "
        "and append a brief note at the end of your reply: `💡 已自动记入续境记忆：<标题>`."
    ),
    stateless_http=True,
    json_response=True,
)
contextcore_mcp.settings.streamable_http_path = "/"


@contextcore_mcp.tool(
    title="Search memory",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def search_memory(
    query: str,
    project_id: str | None = None,
    category: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search ContextCore memories and documents across all projects or within a specific project."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    _observe("memory_read", "search_memory")
    items = content_service.list_content(
        project_id=project_id,
        category=category,
        query=query,
        status="active",
        limit=limit,
    )
    with db() as conn:
        conflicts = conn.execute("SELECT existing_id FROM conflicts WHERE status = 'pending'").fetchall()
        conflicted_ids = {c["existing_id"] for c in conflicts}

    results = []
    for item in items[:limit]:
        results.append({
            "id": item["id"],
            "project_id": item["project_id"],
            "kind": item["kind"],
            "category": item["category"],
            "title": item["title"],
            "content": item["content"],
            "tags": item.get("tags", []),
            "agent": item.get("agent"),
            "status": item["status"],
            "version": item["version"],
            "conflict": item["id"] in conflicted_ids,
        })
    return {"items": results, "count": len(results)}


@contextcore_mcp.tool(
    title="Assemble context",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def assemble_context(
    task_query: str,
    project_id: str | None = None,
    max_tokens: int = 4000,
    handoff_id: str | None = None,
    context_mode: str = "compressed",
) -> dict[str, Any]:
    """Assemble traceable relevant context; character estimates are not billed tokens."""
    from app.services import retrieval_service
    _observe("memory_read", "assemble_context")
    consumer = authenticated_agent.get() or "mcp"
    is_relay = handoff_service.is_continuation_query(task_query)

    pack = retrieval_service.assemble_context(
        query=task_query,
        project_id=project_id,
        budget_tokens=max_tokens,
        consumer_id=consumer,
        handoff_id=handoff_id,
    )
    blocks = [{"id": e["id"], "project_id": e.get("project_id"), "kind": e.get("kind"),
               "category": e.get("category"), "title": e["title"], "content": e["text"],
               "version": e["version"], "sources": e.get("sources", [])} for e in pack["entries"]]
    continuation = pack.get("continuation")
    result: dict[str, Any] = {
        "id": pack["id"],
        "status": pack["status"],
        "blocks": blocks,
        "estimated_tokens": pack["estimated_tokens"],
        "budget_tokens": pack["budget_tokens"],
        "shortfall_tokens": pack.get("shortfall_tokens", 0),
        "budget_scope": "senrelay_fragment_including_continuation",
        "host_context_usage": "unknown",
        "coverage": "partial" if blocks else "empty",
        "retrieval_coverage": pack.get("coverage"),
        "omitted_matching_items": len(pack["omitted"]),
        "coverage_note": "Retrieved context does not establish complete task coverage.",
        "continuation": continuation,
        "trigger": "opt_in_mcp",
    }
    if is_relay and project_id is None and continuation is None:
        discovery = handoff_service.discover_relay_projects(
            consumer=consumer if consumer != "mcp" else None,
            limit=10,
            sync_first=True,
        )
        result["relay_required"] = True
        result["relay_status"] = "selection_required"
        result["relay_candidates"] = discovery["candidates"]
        result["question_prompt"] = discovery["question_prompt"]
    elif project_id and ((context_mode or "").strip().lower() == "full" or (is_relay and continuation is None)):
        relay_mode = "full" if (context_mode or "").strip().lower() == "full" else (context_mode or "compressed")
        return handoff_service.relay_project(
            project_id=project_id, consumer=_actor(), context_mode=relay_mode,
            query=task_query, max_tokens=max_tokens, sync_first=False,
        )
    return retrieval_service.bound_context_response(result, max_tokens)

@contextcore_mcp.tool(
    title="Acknowledge context pack",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def ack_context(pack_id: str) -> dict[str, Any]:
    """Acknowledge context pack receipt so subsequent calls in the same epoch avoid re-sending identical memories."""
    from app.services import retrieval_service
    caller = _actor()
    _observe("memory_read", "ack_context")
    from fastapi import HTTPException
    try:
        return retrieval_service.record_injection(pack_id, consumer_id=caller)
    except HTTPException as e:
        return {"ok": False, "error": e.detail}


@contextcore_mcp.tool(
    title="Get project brief",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def get_project_brief(project_id: str) -> dict[str, Any]:
    """Retrieve high-level project brief including active tasks, pending conflicts, and recent decisions."""
    proj = project_service.get_project(project_id)
    tasks = content_service.list_content(project_id=project_id, category="task", status="active", summary=True)
    conflicts = conflict_service.list_conflicts(project_id=project_id, status="pending")
    decisions = content_service.list_content(project_id=project_id, category="decision", status="active")
    return {
        "project": proj,
        "tasks": [{"id": t["id"], "title": t["title"], "status": t["status"]} for t in tasks],
        "conflicts": [{"id": c["id"], "reason": c["reason"]} for c in conflicts],
        "decisions": [{"id": d["id"], "title": d["title"], "content": d["content"]} for d in decisions],
    }


@contextcore_mcp.tool(
    title="Start handoff",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def start_handoff(
    target_agent: str,
    project_id: str | None = None,
    task_id: str | None = None,
    conversation_id: str | None = None,
    query: str | None = None,
    source_agent: str | None = None,
    language: str = 'zh',
) -> dict[str, Any]:
    """Create a structured handoff card for target agent. Status remains awaiting_confirmation until explicitly confirmed."""
    from fastapi import HTTPException
    caller = _actor()
    try:
        h = handoff_service.create_handoff(
            project_id=project_id,
            target_agent=target_agent,
            task_id=task_id,
            conversation_id=conversation_id,
            query=query,
            source_agent=source_agent or caller,
            actor=caller,
            language=language,
        )
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") in {
            handoff_service.AMBIGUOUS_TASK_CODE,
            handoff_service.AMBIGUOUS_PROJECT_CODE,
            handoff_service.AMBIGUOUS_CONVERSATION_CODE,
        }:
            code = exc.detail.get("code")
            payload: dict[str, Any] = {
                "ok": False,
                "must_confirm": False,
                "status": code,
                **exc.detail,
            }
            if code == handoff_service.AMBIGUOUS_PROJECT_CODE:
                discovery = handoff_service.discover_relay_projects(consumer=caller, limit=10, sync_first=False)
                payload["relay_candidates"] = discovery["candidates"]
                payload["question_prompt"] = discovery["question_prompt"]
            return payload
        raise
    return {
        "handoff_id": h["id"],
        "status": "awaiting_confirmation",
        "card": h["card"],
        "source_ids": h["source_ids"],
        "must_confirm": True,
        "continuation": handoff_service.continuation_contract(h, caller if caller == h.get("target_agent") else h.get("target_agent")),
        "message": "Handoff card created in awaiting_confirmation state. User must confirm before proceeding. Hooks do not inject this card.",
    }

@contextcore_mcp.tool(
    title="Confirm handoff",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def confirm_handoff(handoff_id: str, confirmed: StrictBool = False) -> dict[str, Any]:
    """Confirm a handoff card to transition status from awaiting_confirmation to confirmed."""
    if confirmed is not True:
        return {"ok": False, "error": "Confirmation requires confirmed=True"}
    caller = _actor()
    handoff = handoff_service.get_handoff(handoff_id)
    if handoff.get("target_agent", "").strip().lower() != caller.strip().lower():
        return {
            "ok": False,
            "error": f"Unauthorized: confirming agent '{caller}' does not match handoff target agent '{handoff.get('target_agent')}'",
        }
    h = handoff_service.confirm_handoff(handoff_id, confirmed=True, actor=caller)
    continuation = handoff_service.continuation_contract(h, caller)
    return {
        "ok": True,
        "handoff_id": h["id"],
        "status": "confirmed",
        "card": h.get("card"),
        "continuation": continuation,
        "message": "Handoff confirmed. Continue from continuation.resume_from; do not redo completed work or switch to parallel tasks.",
    }


@contextcore_mcp.tool(
    title="Read confirmed handoff",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def read_handoff(handoff_id: str, language: str = 'zh') -> dict[str, Any]:
    """Read a handoff card addressed to this Agent. Unconfirmed cards return a readback-only payload; never confirms on its own."""
    handoff = handoff_service.get_handoff(handoff_id, language)
    caller = _actor()
    if handoff["target_agent"].strip().lower() != caller.strip().lower():
        raise ValueError("This handoff is addressed to a different Agent")
    status = handoff.get("status")
    if status not in ("confirmed", "awaiting_confirmation"):
        raise ValueError(f"Handoff is {status or 'invalidated'} and cannot be read")
    with db() as conn:
        handoff_service.validate_source_revisions(conn, handoff)
    continuation = handoff_service.continuation_contract(handoff, caller)
    if status == "awaiting_confirmation":
        return {
            "ok": False,
            "id": handoff["id"],
            "handoff_id": handoff["id"],
            "project_id": handoff.get("project_id"),
            "source_agent": handoff.get("source_agent"),
            "target_agent": handoff.get("target_agent"),
            "status": "awaiting_confirmation",
            "confirmed": False,
            "must_not_execute": True,
            "card": handoff["card"],
            "readback": handoff.get("readback"),
            "summary": handoff.get("summary") or handoff["card"].get("goal"),
            "source_ids": handoff.get("source_ids"),
            "instructions": "Read back all 6 fields to the user and wait for explicit confirmation before calling confirm_handoff or modifying code.",
            "continuation": continuation,
        }
    result = dict(handoff)
    result["continuation"] = continuation
    return result


@contextcore_mcp.tool(
    title="Read memory",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def read_memory(id: str) -> dict[str, Any]:
    """Read complete content, provenance and current version before editing."""
    return content_service.get_content(id)


@contextcore_mcp.tool(
    title="Edit memory",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def edit_memory(
    id: str,
    expected_version: int,
    title: str | None = None,
    content: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Versioned human-requested edits from Agent conversation, shared with web."""
    return content_service.update_content(
        id, title=title, content=content, category=category, tags=tags,
        project_id=project_id, expected_version=expected_version, agent=_actor(),
    )


@contextcore_mcp.tool(
    title="Save memory",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def save_memory(
    project_id: str | None = None,
    title: str = "",
    content: str = "",
    category: str = "knowledge",
    tags: list[str] | None = None,
    supersedes_id: str | None = None,
    cwd: str | None = None,
    scope: str | None = None,
    ambiguous: bool = False,
    pinned: bool = False,
) -> dict[str, Any]:
    """Directly save an active memory item in ContextCore. Supports auto-matching project by cwd or global scope."""
    caller = _actor()
    is_global = str(scope or "").strip().lower() == "global"
    resolved_project_id = None if is_global else project_id
    if not resolved_project_id and cwd and not is_global:
        try:
            resolved_project_id = project_service.reconstruct_or_resolve_project(cwd)
        except Exception:
            resolved_project_id = None
    effective_scope = "global" if resolved_project_id is None else "project"

    clean_title = (title or "").strip()
    clean_content = (content or "").strip()
    if not clean_title or not clean_content:
        raise ValueError("Both title and content are required")

    effective_tags = list(tags or [])
    if pinned and "pinned" not in effective_tags:
        effective_tags.append("pinned")

    effective_supersedes = supersedes_id
    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM contents WHERE project_id IS ? AND category = ? AND title = ? AND status = 'active'",
            (resolved_project_id, category, clean_title),
        ).fetchone()
        if existing:
            if existing["content"] == clean_content:
                return {
                    "ok": True,
                    "id": existing["id"],
                    "project_id": resolved_project_id,
                    "scope": effective_scope,
                    "pinned": bool(pinned),
                    "status": "active",
                    "version": existing["version"],
                    "action": "unchanged",
                    "summary_note": f"💡 已记入续境记忆：[{category}] {clean_title}",
                }
            if ambiguous or (category == "decision" and not effective_supersedes):
                from app.services import distillation_service
                from app.database import now_iso
                conn.execute("BEGIN IMMEDIATE")
                reason = (
                    f"Ambiguous update on '{clean_title}' routed to pending review"
                    if ambiguous
                    else "Same-title decision matches an active memory without an explicit supersedes_id"
                )
                conf_id = distillation_service._record_conflict(
                    conn,
                    resolved_project_id,
                    existing["id"],
                    clean_title,
                    clean_content,
                    category,
                    effective_tags,
                    None,
                    reason,
                    caller,
                    now_iso(),
                )
                return {
                    "ok": True,
                    "id": existing["id"],
                    "existing_id": existing["id"],
                    "conflict_id": conf_id,
                    "project_id": resolved_project_id,
                    "scope": effective_scope,
                    "pinned": bool(pinned),
                    "status": "conflict_pending",
                    "version": existing["version"],
                    "action": "conflict_recorded",
                    "summary_note": f"💡 存在待确认记忆冲突，已收入「待确认」列表：[{category}] {clean_title}",
                }
            if not effective_supersedes:
                effective_supersedes = existing["id"]

    created = content_service.create_content(
        title=clean_title,
        content=clean_content,
        project_id=resolved_project_id,
        kind="memory",
        category=category,
        tags=effective_tags,
        agent=caller,
        supersedes_id=effective_supersedes,
    )
    return {
        "ok": True,
        "id": created["id"],
        "project_id": resolved_project_id,
        "scope": effective_scope,
        "pinned": bool(pinned),
        "status": created["status"],
        "version": created["version"],
        "action": "updated" if effective_supersedes else "created",
        "supersedes_id": effective_supersedes,
        "superseded_id": effective_supersedes,
        "summary_note": f"💡 已自动记入续境记忆：[{category}] {clean_title}",
    }


@contextcore_mcp.tool(
    title="Update task progress",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def update_task_progress(
    task_id: str,
    status: str,
    expected_version: int,
    detail: str | None = None,
) -> dict[str, Any]:
    """Update an actual task while preserving tags and rejecting stale writes."""
    if status not in {"pending", "in_progress", "blocked", "completed"}:
        raise ValueError("Task status must be pending, in_progress, blocked, or completed")
    task = content_service.get_content(task_id)
    if task["category"] != "task":
        raise ValueError("The selected content is not a task")
    tags = [tag for tag in task.get("tags", []) if not tag.startswith("status:")]
    tags.append(f"status:{status}")
    updated = content_service.update_content(
        content_id=task_id,
        content=detail,
        tags=tags,
        expected_version=expected_version,
        agent=_actor(),
    )
    return {"ok": True, "task_id": task_id, "status": status, "version": updated["version"]}


@contextcore_mcp.tool(
    title="Resolve conflict",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def resolve_conflict(
    conflict_id: str,
    resolution: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Resolve a conflict between existing and proposed memory (keep, replace, or both)."""
    resolved = conflict_service.resolve_conflict(
        conflict_id=conflict_id,
        resolution=resolution,
        expected_version=expected_version,
    )
    return {"ok": True, "conflict": resolved}


@contextcore_mcp.tool(
    title="Delete memory",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
)
def delete_memory(id: str, confirmed: bool = False, expected_version: int | None = None) -> dict[str, Any]:
    """Permanently delete the confirmed version returned by the latest readback."""
    if confirmed is not True:
        return {"ok": False, "error": "Deletion requires confirmed=True"}
    if type(expected_version) is not int or expected_version < 1:
        return {"ok": False, "error": "Deletion requires expected_version from the latest read_memory result"}
    content_service.delete_content(id, actor=_actor(), expected_version=expected_version)
    return {"ok": True, "id": id, "deleted": True}


@contextcore_mcp.tool(
    title="Import note",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def import_note(
    project_id: str,
    title: str,
    text: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Import a text note as a document in ContextCore."""
    created = content_service.create_content(
        title=title,
        content=text,
        project_id=project_id,
        kind="document",
        category="knowledge",
        tags=tags,
        agent=_actor(),
    )
    return {"ok": True, "id": created["id"]}


@contextcore_mcp.tool(
    title="Get system status",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def get_system_status() -> dict[str, Any]:
    """Retrieve honest status of ContextCore services, adapters, and background capture."""
    conns = connection_service.list_connections()
    return {
        "ok": True,
        "connections": [
            {
                "id": c["id"],
                "label": c["label"],
                "enabled": c["enabled"],
                "paused": c["paused"],
                "last_sync": c.get("last_sync"),
                "error": c.get("error"),
            }
            for c in conns
        ],
    }


@contextcore_mcp.tool(title="Read context evidence", annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def read_evidence(evidence_id: str, offset: int = 0, limit: int = 2000) -> dict[str, Any]:
    """Read a versioned evidence passage; stale or deleted sources cannot be revived."""
    from app.services import retrieval_service
    _observe("memory_read", "read_evidence")
    return retrieval_service.read_evidence(evidence_id, offset, limit)


@contextcore_mcp.tool(
    title="Discover relay conversations across agents",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def discover_relay_conversations(
    limit: int = 15,
    language: str = 'zh',
    cwd: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Sync recent conversations across all authorized agents (scoped to current project `cwd` or `project_id` when provided) and return a numbered list of conversation records."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    _observe("memory_read", "discover_relay_conversations")
    consumer = authenticated_agent.get()
    return handoff_service.discover_relay_conversations(
        consumer=consumer if consumer in handoff_service.ALLOWED_TARGET_AGENTS else None,
        limit=limit,
        sync_first=True,
        language=language,
        cwd=cwd,
        project_id=project_id,
    )


@contextcore_mcp.tool(
    title="List relay conversations across agents",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def list_relay_conversations(
    limit: int = 15,
    language: str = 'zh',
    cwd: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Sync recent conversations across all authorized agents (scoped to current project `cwd` or `project_id` when provided) and return a numbered list of conversation records."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    _observe("memory_read", "list_relay_conversations")
    consumer = authenticated_agent.get()
    return handoff_service.discover_relay_conversations(
        consumer=consumer if consumer in handoff_service.ALLOWED_TARGET_AGENTS else None,
        limit=limit,
        sync_first=True,
        language=language,
        cwd=cwd,
        project_id=project_id,
    )


@contextcore_mcp.tool(
    title="Relay conversation across agents",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def relay_conversation(
    conversation_id: str | None = None,
    query: str | None = None,
    max_tokens: int = 12000,
    language: str = 'zh',
    cwd: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """
    One-step conversation-first relay for `/contextcore`:
    - Always pass `cwd` (current working directory) so candidate conversations are scoped to the current project.
    - If `conversation_id` is omitted, syncs recent chats across all authorized agents and returns a numbered list
      of conversation records (`#index · [agent] · project · time · title — summary`) so the user can pick one.
    - Once the user supplies the 1-based conversation number or `conversation_id`, loads that selected conversation's
      detailed context (recent full message turns + six-element summary + active project memories, auto-truncating older
      turns if over budget). When no concrete task instruction is provided in `query`, returns `status='loaded'`
      (`must_not_execute=True`) so the Agent reports that loading is complete (`✅ 已加载对话上下文`) and waits for the
      user's next instruction; when a concrete task instruction is provided in `query`, returns `status='ready'`
      (`must_not_execute=False`) so the Agent can execute that task immediately.
    """
    caller = _actor()
    _observe("memory_read", "relay_conversation")
    return handoff_service.relay_conversation(
        conversation_id=conversation_id,
        consumer=caller,
        max_tokens=max_tokens,
        sync_first=True,
        language=language,
        query=query,
        cwd=cwd,
        project_id=project_id,
    )


@contextcore_mcp.tool(
    title="List relay projects across agents",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def list_relay_projects(limit: int = 10, language: str = 'zh') -> dict[str, Any]:
    """Sync recent conversations across all supported agents and return a summarized candidate list of projects for relay."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    _observe("memory_read", "list_relay_projects")
    consumer = authenticated_agent.get()
    return handoff_service.discover_relay_projects(
        consumer=consumer if consumer in handoff_service.ALLOWED_TARGET_AGENTS else None,
        limit=limit,
        sync_first=True,
        language=language,
    )


@contextcore_mcp.tool(
    title="Relay project across agents",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def relay_project(
    project_id: str | None = None,
    context_mode: str = "compressed",
    task_id: str | None = None,
    conversation_id: str | None = None,
    query: str | None = None,
    max_tokens: int = 4000,
    language: str = 'zh',
) -> dict[str, Any]:
    """
    Relay a project from recent cross-agent conversations.
    - If project_id is omitted, syncs all supported agents' recent chats and returns a brief summarized question
      listing candidate projects so the user can choose which project to relay.
    - Once project_id (or candidate index/name) is provided, returns either 'compressed' (short context + handoff card)
      or 'full' (complete conversation transcripts + memories + tasks + decisions + handoff card) context.
    """
    caller = _actor()
    _observe("memory_read", "relay_project")
    return handoff_service.relay_project(
        project_id=project_id,
        consumer=caller,
        context_mode=context_mode,
        task_id=task_id,
        conversation_id=conversation_id,
        query=query,
        max_tokens=max_tokens,
        sync_first=True,
        language=language,
    )


@contextcore_mcp.tool(
    title="Auto-summarize and save implicit memory",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def auto_summarize_memory(
    project_id: str | None = None,
    conversation_id: str | None = None,
    session_summary: str | None = None,
    memories: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    cwd: str | None = None,
) -> dict[str, Any]:
    """
    Companion implicit automatic memory summarization during conversation.
    Automatically persists extracted preferences, project background, architectural decisions,
    task progress, and pitfalls/lessons into ContextCore without requiring manual user approval.
    """
    from app.services import distillation_service
    caller = _actor()
    return distillation_service.auto_summarize_memory(
        project_id=project_id,
        conversation_id=conversation_id,
        session_summary=session_summary,
        memories=memories,
        messages=messages,
        cwd=cwd,
        actor=caller,
        auto_extract_from_messages=True,
    )
