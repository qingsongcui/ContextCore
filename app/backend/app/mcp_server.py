import json
import re
from typing import Any
from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import StrictBool

from app.services import (
    content_service,
    project_service,
    handoff_service,
    conflict_service,
    connection_service,
    telemetry_service,
    working_memory_service,
    governance_guardrails,
)
from app.database import db
from app.security import authenticated_agent


READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


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


def _build_applicability_warning(applicability: dict[str, Any] | None) -> str | None:
    norm = governance_guardrails.normalize_applicability(applicability)
    if not governance_guardrails.has_non_empty_applicability(norm):
        return None
    parts = [f"{k}={v}" for k, v in norm.items()]
    return f"仅在匹配适用条件时参考（{'; '.join(parts)}）；落地前请核对当前环境与版本。"


def _fallback_bootstrap_working_memory(
    *,
    cwd: str | None = None,
    project_id: str | None = None,
    query: str = "",
    task_id: str | None = None,
    consumer_id: str | None = None,
    session_key: str | None = None,
    max_tokens: int = 12000,
) -> dict[str, Any]:
    """Degraded bootstrap path that still honours the protected-budget contract.

    It only changes *how* the sources are gathered (Wave 2A services instead of the
    retrieval service assembly). Projection is lossless, the execution gate and
    task selection come from `retrieval_service`, and the payload goes through
    `retrieval_service.bound_bootstrap_payload`, so safety rules either arrive
    whole or the call is refused with `insufficient_budget`.
    """
    from app.services import retrieval_service

    base_cwd = project_service.canonical_project_path(cwd) if cwd else None
    resolved_pid = project_id
    resolved_project = None
    if resolved_pid:
        try:
            resolved_project = project_service.get_project(resolved_pid)
        except Exception:
            resolved_project = None
    elif base_cwd:
        try:
            matched, is_ambiguous = project_service.resolve_project_by_path(base_cwd)
            if matched and not is_ambiguous:
                resolved_project = matched
                resolved_pid = matched["id"]
        except Exception:
            resolved_pid = None

    def _projection(item: dict[str, Any]) -> dict[str, Any]:
        raw_app = item.get("applicability")
        applicability = raw_app if isinstance(raw_app, dict) else {}
        version = int(item.get("version") or 1)
        tags = [str(tag) for tag in item.get("tags") or []]
        return {
            "id": item["id"],
            "version": version,
            "category": item.get("category"),
            "scope": "project" if item.get("project_id") else "global",
            "project_id": item.get("project_id"),
            "title": item.get("title"),
            # Whole text, never a snippet: applicable rules are safety material.
            "content": item.get("content") or "",
            "pinned": bool(item.get("pinned")) or "pinned" in tags,
            "origin_authority": item.get("origin_authority") or "manual",
            "applicability": applicability,
            "applicability_warning": _build_applicability_warning(applicability),
            "evidence_id": retrieval_service.evidence_id(item["id"], version),
        }

    # 1. Applicable constraints (project + global active decisions & preferences)
    applicable_constraints: list[dict[str, Any]] = []
    seen_cids: set[str] = set()
    for category in ("decision", "preference"):
        gather: list[dict[str, Any]] = []
        if resolved_pid:
            gather = content_service.list_content(
                project_id=resolved_pid, category=category, status="active", pinned_first=True
            )
        gather = [
            *gather,
            *content_service.list_content(
                scope="global", category=category, status="active", pinned_first=True
            ),
        ]
        for item in gather:
            if item["id"] in seen_cids:
                continue
            seen_cids.add(item["id"])
            applicable_constraints.append(_projection(item))

    # 2. Active working tasks, task selection and the shared execution gate
    active_tasks: list[dict[str, Any]] = []
    if resolved_pid:
        active_tasks = working_memory_service.list_working_tasks(
            project_id=resolved_pid, active_only=True, include_events=False
        )
    elif task_id:
        try:
            active_tasks = [working_memory_service.get_working_task(task_id, include_events=False)]
        except Exception:
            active_tasks = []
    checkout = project_service.read_git_checkout(base_cwd) if base_cwd else None
    branch = (checkout or {}).get("branch") if isinstance(checkout, dict) and not checkout.get("detached") else None
    selected_task, task_selection_required = retrieval_service.select_context_task(
        active_tasks, task_id=task_id, query=query, git_branch=branch
    )
    if task_id and selected_task is None:
        try:
            selected_task = working_memory_service.get_working_task(task_id, include_events=False)
        except Exception:
            selected_task = None
        if isinstance(selected_task, dict):
            task_selection_required = False
            if not any(task.get("id") == selected_task.get("id") for task in active_tasks):
                active_tasks = [*active_tasks, selected_task]

    has_unconfirmed_handoff = False
    if resolved_pid and consumer_id and consumer_id in handoff_service.ALLOWED_TARGET_AGENTS:
        with db() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM handoffs
                WHERE project_id = ? AND lower(target_agent) = ? AND status = 'awaiting_confirmation'
                LIMIT 1
                """,
                (resolved_pid, consumer_id.lower()),
            ).fetchone()
            has_unconfirmed_handoff = row is not None
    must_not_execute = retrieval_service.bootstrap_must_not_execute(
        selected_task, task_selection_required, has_unconfirmed_handoff
    )

    # 3. Relevant lessons / background / knowledge (whole text; budget trims them)
    lesson_candidates: list[dict[str, Any]] = []
    if (query or "").strip():
        s_res = retrieval_service.search(query=query.strip(), project_id=resolved_pid, limit=25)
        cand_ids = [
            r["id"]
            for r in (s_res.get("items") or s_res.get("results") or [])
            if r.get("category") in ("lesson", "background", "knowledge")
            and (resolved_pid is None and r.get("project_id") is None or (resolved_pid is not None and r.get("project_id") in (resolved_pid, None)))
        ][:10]
        for cid in cand_ids:
            try:
                lesson_candidates.append(content_service.get_content(cid))
            except Exception:
                pass
    if len(lesson_candidates) < 5 and resolved_pid:
        existing_lids = {item["id"] for item in lesson_candidates}
        for category in ("lesson", "background", "knowledge"):
            for item in content_service.list_content(
                project_id=resolved_pid, category=category, status="active", limit=5
            ):
                if item["id"] not in existing_lids:
                    existing_lids.add(item["id"])
                    lesson_candidates.append(item)
    relevant_lessons = [
        _projection(item)
        for item in lesson_candidates[:8]
        if item.get("status") == "active" and item["id"] not in seen_cids
    ]

    # 4. Pending conflicts (whole entries; budget drops them with a counter)
    pending_conflicts = (
        conflict_service.list_conflicts(project_id=resolved_pid, status="pending")
        if resolved_pid
        else []
    )

    payload: dict[str, Any] = {
        "ok": True,
        "status": "ready",
        "project_id": resolved_pid,
        "project_name": resolved_project["name"] if resolved_project else None,
        "cwd": base_cwd,
        "git_branch": branch,
        "applicable_constraints": applicable_constraints,
        "active_tasks": [
            retrieval_service.bootstrap_compact_task(task, base_cwd=base_cwd) for task in active_tasks
        ],
        "selected_task": (
            retrieval_service.bootstrap_compact_task(selected_task, base_cwd=base_cwd)
            if selected_task else None
        ),
        "task_selection_required": task_selection_required,
        "must_not_execute": must_not_execute,
        "relevant_lessons": relevant_lessons,
        "pending_conflicts": [
            {
                "id": conflict["id"],
                "existing_id": conflict.get("existing_id"),
                "reason": conflict.get("reason"),
                "proposed": conflict.get("proposed"),
            }
            for conflict in pending_conflicts
        ],
        "feedback_summary": "",
        "max_tokens": max_tokens,
        "budget_tokens": max_tokens,
        "estimated_tokens": 0,
        "omitted_constraints_count": 0,
        "omitted_lessons_count": 0,
        "omitted_active_tasks_count": 0,
        "omitted_conflicts_count": 0,
        "tasks_truncated": False,
    }
    fitted = retrieval_service.bound_bootstrap_payload(payload, max_tokens)
    if fitted.get("status") != "ready":
        # Nothing was delivered, so no memory may be reported as provided.
        return fitted
    returned = [
        *(fitted.get("applicable_constraints") or []),
        *(fitted.get("relevant_lessons") or []),
    ]
    if returned and (consumer_id is None or str(consumer_id).strip().lower() != "web"):
        telemetry_service.record_provided(
            [item["id"] for item in returned],
            agent=consumer_id or "mcp",
            project_id=resolved_pid,
            task_id=(selected_task or {}).get("id"),
            session_key=session_key,
            surface="bootstrap_working_memory",
            content_versions={item["id"]: int(item.get("version") or 1) for item in returned},
            dedupe_session=bool(session_key),
        )
    return fitted


contextcore_mcp = FastMCP(
    "ContextCore",
    instructions=(
        "ContextCore (续境) is the cross-agent working memory & continuation engine for Claude Code, Codex, Cursor, OpenCode, OMP, and Antigravity. "
        "All 6 hosts follow the Cross-Agent Working Memory Contract:\n"
        "1. Proactive Scoped Read: Before starting substantive coding/debugging/design work in a project, switching tasks/projects/Agents, "
        "when the user mentions '上次'/'按之前的'/'继续之前', hitting a potential pitfall, or recovering after context compaction, "
        "call `bootstrap_working_memory(cwd=..., query=...)` first. Skip calling it for trivial 1-turn Q&A (e.g. '现在有几个分支', 'git status').\n"
        "2. Read != Execution Permission: If `bootstrap_working_memory`, `relay_conversation`, or `read_handoff` returns `must_not_execute=True` "
        "(`execution_mode='discussion_only'` or unconfirmed handoff), OR if the user is only discussing a plan/architecture without authorizing code edits, "
        "stay strictly in discussion mode — NEVER start modifying code until the user explicitly authorizes execution. "
        "If `task_selection_required=True` (multiple active tasks) or `pending_conflicts` are present, ask the user before assuming one.\n"
        "3. Restrained 4-Event Writes (Zero Noise): Write ONLY on these 4 triggers: "
        "(a) User explicit decision, coding preference, or correction -> `save_memory(..., is_user_correction=...)`; "
        "(b) Task stage completion (with verification evidence), blocker, failed attempt, or one-off constraint ('这次先别...') -> `upsert_working_task(...)`; "
        "(c) Validated reusable conditional lesson/pitfall -> `promote_task_lesson(task_id=..., applicability=...)` or `save_memory(..., category='lesson', applicability=...)`; "
        "(d) User states an old rule/conclusion is obsolete -> `mark_memory_outdated(memory_id=..., reason=...)`. "
        "NEVER save unconfirmed AI proposals, unverified debugging guesses, or raw command/traceback logs as long-term memory.\n"
        "4. Honest Citation & Lightweight Feedback: When loaded memories genuinely guide your implementation or decision, pass `cited_memory_ids=[...]` to "
        "`upsert_working_task` or call `cite_memory(memory_ids=[...], reason=...)`, and briefly mention key referenced constraints/task state to the user.\n"
        "5. Explicit `/contextcore` Cross-Agent Relay: When the user types `/contextcore` or asks to relay a specific chat ('接力', '接着做', 'continue'), "
        "call `relay_conversation(cwd=...)` (or `discover_relay_conversations(cwd=...)`). If `conversation_id` is omitted, present the numbered candidate list. "
        "Once selected, call `relay_conversation(conversation_id=..., cwd=...)`. If `transcript_paging.complete` is false (or `coverage.omitted_messages > 0`), "
        "sequentially call `read_conversation_messages(conversation_id=..., offset=..., limit=30)` from `offset=0` until `next_offset` is null to read the ENTIRE transcript, "
        "then compact the full conversation in context (like `/compact`) and report a clean compacted summary — NEVER present a handoff card or six-element card. "
        "If `status='loaded'` (`must_not_execute=True`), stop and wait for the user's concrete instruction after reporting; only execute immediately when `continuation.status='ready'` (`must_not_execute=False`)."
    ),
    stateless_http=True,
    json_response=True,
)
contextcore_mcp.settings.streamable_http_path = "/"


@contextcore_mcp.tool(
    title="Bootstrap scoped working memory",
    annotations=READ_ONLY_ANNOTATIONS,
)
def bootstrap_working_memory(
    cwd: str | None = None,
    project_id: str | None = None,
    query: str = "",
    task_id: str | None = None,
    session_key: str | None = None,
    max_tokens: int = 12000,
) -> dict[str, Any]:
    """Proactively load minimal project-scoped constraints, active working task state, relevant lessons, and pending conflicts before substantive work."""
    if type(max_tokens) is not int or not 1 <= max_tokens <= 32000:
        raise ValueError("max_tokens must be between 1 and 32000")
    _observe("memory_read", "bootstrap_working_memory")
    consumer = authenticated_agent.get() or "mcp"
    from app.services import retrieval_service

    fn = getattr(retrieval_service, "bootstrap_working_memory", None)
    if callable(fn):
        res = fn(
            cwd=cwd,
            project_id=project_id,
            query=query,
            task_id=task_id,
            consumer_id=consumer,
            session_key=session_key,
            max_tokens=max_tokens,
        )
        if isinstance(res, dict) and "ok" not in res:
            res = {"ok": True, **res}
        return res
    return _fallback_bootstrap_working_memory(
        cwd=cwd,
        project_id=project_id,
        query=query,
        task_id=task_id,
        consumer_id=consumer,
        session_key=session_key,
        max_tokens=max_tokens,
    )


@contextcore_mcp.tool(
    title="Search memory",
    annotations=READ_ONLY_ANNOTATIONS,
)
def search_memory(
    query: str,
    project_id: str | None = None,
    category: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search active ContextCore memories and documents (FTS5/hybrid ranked, zero read_count boost)."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    _observe("memory_read", "search_memory")
    from app.services import retrieval_service

    # 1. Primary ranking via retrieval_service.search (active-only, tombstone-checked, zero popularity boost)
    search_mode = "hybrid" if retrieval_service.get_settings().get("semantic_enabled") else "lexical"
    ranked_pack = retrieval_service.search(
        query=query,
        project_id=project_id,
        limit=min(100, max(limit * 4, limit)),
        mode=search_mode,
    )
    ranked_ids: list[str] = []
    for hit in (ranked_pack.get("items") or ranked_pack.get("results") or []):
        if project_id is not None and hit.get("project_id") != project_id:
            continue
        if category is not None and hit.get("category") != category:
            continue
        hid = hit.get("id")
        if hid and hid not in ranked_ids:
            ranked_ids.append(hid)
        if len(ranked_ids) >= limit:
            break

    # 2. Bounded hydration from content_service.db() with SQL LIMIT, plus exact substring fallback when needed
    hydrated_by_id: dict[str, dict[str, Any]] = {}
    if ranked_ids:
        placeholders = ",".join("?" for _ in ranked_ids[:limit])
        clauses = [f"c.id IN ({placeholders})", "c.status = 'active'"]
        params: list[Any] = list(ranked_ids[:limit])
        if project_id is not None:
            clauses.append("c.project_id = ?")
            params.append(project_id)
        if category is not None:
            clauses.append("c.category = ?")
            params.append(category)
        params.append(limit)
        with content_service.db() as conn:
            rows = conn.execute(
                f"SELECT c.* FROM contents c WHERE {' AND '.join(clauses)} LIMIT ?",
                tuple(params),
            ).fetchall()
            for r in rows:
                d = content_service.row_to_dict(r)
                if not governance_guardrails.is_tombstoned_memory(
                    conn, d.get("project_id"), d.get("category"), d.get("title") or "", d.get("content") or ""
                ):
                    hydrated_by_id[d["id"]] = content_service._enrich_content_item(d)

    ordered_items: list[dict[str, Any]] = [
        hydrated_by_id[cid] for cid in ranked_ids if cid in hydrated_by_id
    ]

    if len(ordered_items) < limit:
        seen_ids = {it["id"] for it in ordered_items}
        supplement = content_service.list_content(
            project_id=project_id,
            category=category,
            query=query,
            status="active",
            limit=limit,
        )
        for it in supplement:
            if it["id"] not in seen_ids:
                seen_ids.add(it["id"])
                ordered_items.append(it)
            if len(ordered_items) >= limit:
                break

    with db() as conn:
        conflicts = conn.execute("SELECT existing_id FROM conflicts WHERE status = 'pending'").fetchall()
        conflicted_ids = {c["existing_id"] for c in conflicts}

    results = []
    for item in ordered_items[:limit]:
        app_cond = item.get("applicability") or {}
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
            "origin_authority": item.get("origin_authority", "manual"),
            "applicability": app_cond,
            "applicability_warning": _build_applicability_warning(app_cond),
            "conflict": item["id"] in conflicted_ids,
        })

    if results:
        telemetry_service.record_provided(
            [r["id"] for r in results],
            agent=authenticated_agent.get() or "mcp",
            project_id=project_id,
            surface="search_memory",
            content_versions={r["id"]: r["version"] for r in results},
        )
    return {"items": results, "count": len(results)}


@contextcore_mcp.tool(
    title="Assemble context",
    annotations=READ_ONLY_ANNOTATIONS,
)
def assemble_context(
    task_query: str,
    project_id: str | None = None,
    max_tokens: int = 24000,
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
               "version": e["version"], "applicability": e.get("applicability", {}),
               "sources": e.get("sources", [])} for e in pack["entries"]]
    continuation = pack.get("continuation")
    result: dict[str, Any] = {
        "id": pack["id"],
        "status": pack["status"],
        "blocks": blocks,
        "working_tasks": pack.get("working_tasks", []),
        "task_selection_required": bool(pack.get("task_selection_required", False)),
        "must_not_execute": bool(pack.get("must_not_execute", False)),
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
    try:
        return retrieval_service.record_injection(pack_id, consumer_id=caller)
    except HTTPException as e:
        return {"ok": False, "error": e.detail}


@contextcore_mcp.tool(
    title="Get project brief",
    annotations=READ_ONLY_ANNOTATIONS,
)
def get_project_brief(project_id: str) -> dict[str, Any]:
    """Retrieve high-level project brief including active working tasks, pending conflicts, and active decisions."""
    _observe("memory_read", "get_project_brief")
    proj = project_service.get_project(project_id)
    working_tasks = working_memory_service.list_working_tasks(project_id=project_id, active_only=True)
    all_working_tasks = working_memory_service.list_working_tasks(project_id=project_id, active_only=False)
    mirrored_legacy_ids = {
        wt.get("legacy_content_id")
        for wt in all_working_tasks
        if wt.get("legacy_content_id")
    }
    legacy_tasks = content_service.list_content(project_id=project_id, category="task", status="active", summary=True)
    conflicts = conflict_service.list_conflicts(project_id=project_id, status="pending")
    decisions = content_service.list_content(project_id=project_id, category="decision", status="active")
    preferences = content_service.list_content(project_id=project_id, category="preference", status="active")

    provided_items = [*decisions, *preferences]
    if provided_items:
        telemetry_service.record_provided(
            [it["id"] for it in provided_items],
            agent=authenticated_agent.get() or "mcp",
            project_id=project_id,
            surface="get_project_brief",
            content_versions={it["id"]: it["version"] for it in provided_items},
        )

    candidate_tombstone_keys: list[str] = []
    for t in legacy_tasks:
        tid = t.get("id")
        if tid:
            candidate_tombstone_keys.append(f"legacy_task:{tid}")
        ck = t.get("canonical_key")
        if ck:
            candidate_tombstone_keys.append(str(ck))
    found_tombstones: set[str] = set()
    if candidate_tombstone_keys:
        unique_keys = list(dict.fromkeys(candidate_tombstone_keys))
        placeholders = ",".join("?" for _ in unique_keys)
        with db() as conn:
            found_tombstones = {
                str(row["dedupe_key"])
                for row in conn.execute(
                    f"SELECT dedupe_key FROM tombstones WHERE entity_type = 'memory' AND dedupe_key IN ({placeholders})",
                    unique_keys,
                ).fetchall()
            }

    merged_task_summaries = [
        {"id": wt["id"], "title": wt["title"], "status": wt["status"]}
        for wt in working_tasks
    ]
    inactive_legacy_tags = {"status:completed", "status:paused", "status:cancelled"}
    for t in legacy_tasks:
        tid = t.get("id")
        if tid in mirrored_legacy_ids or f"legacy_task:{tid}" in found_tombstones:
            continue
        ck = t.get("canonical_key")
        if ck and str(ck) in found_tombstones:
            continue
        tags = {str(tag).strip().lower() for tag in (t.get("tags") or [])}
        if tags & inactive_legacy_tags:
            continue
        merged_task_summaries.append({"id": tid, "title": t["title"], "status": t["status"]})

    return {
        "project": proj,
        "tasks": merged_task_summaries,
        "working_tasks": working_tasks,
        "conflicts": [{"id": c["id"], "reason": c["reason"]} for c in conflicts],
        "decisions": [{"id": d["id"], "title": d["title"], "content": d["content"]} for d in decisions],
        "preferences": [{"id": p["id"], "title": p["title"], "content": p["content"]} for p in preferences],
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
    annotations=READ_ONLY_ANNOTATIONS,
)
def read_handoff(handoff_id: str, language: str = 'zh') -> dict[str, Any]:
    """Read a handoff card addressed to this Agent. Unconfirmed cards return a readback-only payload; never confirms on its own."""
    _observe("memory_read", "read_handoff")
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
    annotations=READ_ONLY_ANNOTATIONS,
)
def read_memory(id: str) -> dict[str, Any]:
    """Read complete content, provenance and current version before editing."""
    _observe("memory_read", "read_memory")
    item = content_service.get_content(id)
    telemetry_service.record_provided(
        [id],
        agent=authenticated_agent.get() or "mcp",
        project_id=item.get("project_id"),
        surface="read_memory",
        content_versions={id: item.get("version", 1)},
    )
    item["usage_summary"] = telemetry_service.get_usage_summary(id)
    return item


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
    applicability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Versioned human-requested edits from Agent conversation, shared with web."""
    _observe("memory_write", "edit_memory")
    return content_service.update_content(
        id,
        title=title,
        content=content,
        category=category,
        tags=tags,
        project_id=project_id,
        applicability=applicability,
        expected_version=expected_version,
        agent=_actor(),
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
    applicability: dict[str, Any] | None = None,
    origin_authority: str | None = None,
    is_user_correction: bool = False,
    cited_memory_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Save a governed long-term memory item. Routes temporary task constraints ('这次先别...') to working_tasks and blocks unconfirmed proposals/guesses/raw logs."""
    caller = _actor()
    _observe("memory_write", "save_memory")
    is_global = str(scope or "").strip().lower() == "global"
    resolved_project_id = None if is_global else project_id
    if not resolved_project_id and cwd and not is_global:
        try:
            resolved_project_id = project_service.reconstruct_or_resolve_project(cwd)
        except Exception:
            resolved_project_id = None
    if not is_global and not resolved_project_id:
        # Never silently promote a single-project rule into the global scope.
        raise HTTPException(
            status_code=422,
            detail=(
                "Cannot resolve a project for save_memory from cwd; pass an explicit "
                "project_id or scope='global' for cross-project preferences"
            ),
        )
    effective_scope = "global" if resolved_project_id is None else "project"

    clean_title = (title or "").strip()
    clean_content = (content or "").strip()
    if not clean_title or not clean_content:
        raise ValueError("Both title and content are required")

    # Short-term tasks belong in working_tasks, never in long-term contents
    if category == "task":
        routed_task = working_memory_service.upsert_working_task(
            title=clean_title,
            project_id=resolved_project_id,
            cwd=cwd,
            goal=clean_content,
            current_state=clean_content,
            agent=caller,
            last_agent=caller,
            cited_memory_ids=cited_memory_ids,
        )
        return {
            "ok": True,
            "action": "routed_to_working_task",
            "status": routed_task["status"],
            "id": routed_task["id"],
            "task_id": routed_task["id"],
            "project_id": resolved_project_id,
            "scope": effective_scope,
            "working_task": routed_task,
            "summary_note": f"💡 短期任务进展已记入当前工作任务：{clean_title}",
        }

    effective_user_correction = bool(is_user_correction) or (
        not ambiguous
        and category == "preference"
        and bool(
            re.search(
                r"(?:改为|改用|换成|切换为|不再使用|不再采用|switched?\s+to\s+.+\s+instead\s+of)",
                clean_content,
                re.IGNORECASE,
            )
        )
    )

    # Prevent unconfirmed agent calls from self-asserting authoritative origin to bypass admission filters
    raw_origin = (origin_authority or "").strip().lower() or None
    effective_origin = raw_origin
    if not effective_user_correction and raw_origin in governance_guardrails.AUTHORITATIVE_ORIGINS:
        precheck = governance_guardrails.classify_write_candidate(
            title=clean_title,
            content=clean_content,
            category=category,
            origin_authority=None,
            is_agent_write=True,
            actor=caller,
            kind="memory",
        )
        if not precheck["allowed"]:
            effective_origin = None

    reconciled = content_service.reconcile_long_term_memory(
        title=clean_title,
        content=clean_content,
        project_id=resolved_project_id,
        category=category,
        tags=tags,
        pinned=pinned,
        scope=effective_scope,
        applicability=applicability,
        origin_authority=effective_origin,
        agent=caller,
        actor=caller,
        is_agent_write=not effective_user_correction,
        is_user_correction=effective_user_correction,
        supersedes_id=supersedes_id,
        ambiguous=ambiguous,
        cwd=cwd,
        auto_route_temp_constraint=True,
    )

    rec_action = reconciled.get("action")
    if rec_action == "ROUTE_TO_WORKING_TASK":
        wt = reconciled.get("working_task")
        if wt is None:
            wt = working_memory_service.upsert_working_task(
                title=clean_title if not working_memory_service.is_trivial_qa(clean_title) else "当前会话任务",
                project_id=resolved_project_id,
                cwd=cwd,
                temp_constraints=[clean_content],
                agent=caller,
                last_agent=caller,
                cited_memory_ids=cited_memory_ids,
            )
        elif cited_memory_ids:
            telemetry_service.record_cited(
                cited_memory_ids,
                agent=caller,
                project_id=resolved_project_id,
                task_id=wt["id"],
                surface="save_memory",
                strict=False,
            )
        return {
            "ok": True,
            "action": "routed_to_working_task",
            "status": "routed_to_working_task",
            "reason": reconciled.get("reason"),
            "id": wt["id"],
            "task_id": wt["id"],
            "project_id": resolved_project_id,
            "scope": effective_scope,
            "working_task": wt,
            "summary_note": f"💡 单次临时约束已记入当前工作任务（未写入长期记忆）：{clean_title}",
        }

    if rec_action == "DROP_TOMBSTONED":
        raise HTTPException(
            status_code=410,
            detail="Memory was previously deleted and tombstoned and cannot be resurrected",
        )

    if rec_action == "REJECTED":
        return {
            "ok": False,
            "action": "rejected",
            "status": reconciled.get("status", "rejected"),
            "reason": reconciled.get("reason"),
            "project_id": resolved_project_id,
            "scope": effective_scope,
        }

    if rec_action == "OPEN_CONFLICT":
        conf = reconciled.get("conflict") or {}
        existing_id = reconciled.get("existing_id") or reconciled.get("id")
        existing_item = content_service.get_content(existing_id) if existing_id else {}
        return {
            "ok": True,
            "id": existing_id,
            "content_id": existing_id,
            "existing_id": existing_id,
            "conflict_id": reconciled.get("conflict_id") or conf.get("id"),
            "project_id": resolved_project_id,
            "scope": effective_scope,
            "pinned": bool(pinned),
            "status": "conflict_pending",
            "version": existing_item.get("version", 1),
            "action": "conflict_recorded",
            "summary_note": f"💡 存在待确认记忆冲突，已收入「待确认」列表：[{category}] {clean_title}",
        }

    mem = reconciled.get("memory") or {}
    mem_id = mem.get("id") or reconciled.get("id")
    if cited_memory_ids:
        telemetry_service.record_cited(
            cited_memory_ids,
            agent=caller,
            project_id=resolved_project_id,
            surface="save_memory",
            strict=False,
        )

    if rec_action == "NOOP":
        return {
            "ok": True,
            "id": mem_id,
            "content_id": mem_id,
            "project_id": resolved_project_id,
            "scope": effective_scope,
            "pinned": bool(mem.get("pinned", pinned)),
            "status": mem.get("status", "active"),
            "version": mem.get("version", 1),
            "action": "unchanged",
            "summary_note": f"💡 已记入续境记忆：[{category}] {clean_title}",
        }

    eff_superseded = (
        reconciled.get("superseded_id")
        or reconciled.get("supersedes_id")
        or mem.get("supersedes_id")
        or supersedes_id
    )
    return {
        "ok": True,
        "id": mem_id,
        "content_id": mem_id,
        "project_id": resolved_project_id,
        "scope": effective_scope,
        "pinned": bool(mem.get("pinned", pinned)),
        "status": mem.get("status", "active"),
        "version": mem.get("version", 1),
        "origin_authority": mem.get("origin_authority"),
        "applicability": mem.get("applicability", {}),
        "action": "updated" if (rec_action == "SUPERSEDE" or eff_superseded) else "created",
        "supersedes_id": eff_superseded,
        "superseded_id": eff_superseded,
        "summary_note": f"💡 已自动记入续境记忆：[{category}] {clean_title}",
    }


@contextcore_mcp.tool(
    title="Upsert short-term working task",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def upsert_working_task(
    title: str,
    project_id: str | None = None,
    task_id: str | None = None,
    cwd: str | None = None,
    git_branch: str | None = None,
    status: str | None = None,
    execution_mode: str | None = None,
    goal: str | None = None,
    completed_steps: list[str] | None = None,
    add_completed_step: str | None = None,
    current_state: str | None = None,
    temp_constraints: list[str] | None = None,
    add_temp_constraint: str | None = None,
    failed_attempts: list[str] | None = None,
    add_failed_attempt: str | None = None,
    blockers: list[str] | None = None,
    add_blocker: str | None = None,
    next_steps: list[str] | None = None,
    source_conversation_id: str | None = None,
    cited_memory_ids: list[str] | None = None,
    citation_note: str | None = None,
    continue_branch_task: bool = False,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Create or update a short-term working task (6 structured slots, execution_mode, and cited_memory_ids)."""
    caller = _actor()
    _observe("memory_write", "upsert_working_task")
    resolved_pid = project_id
    if not resolved_pid and cwd:
        try:
            resolved_pid = project_service.reconstruct_or_resolve_project(cwd)
        except Exception:
            resolved_pid = None

    norm_exec_mode = execution_mode
    if isinstance(norm_exec_mode, str) and norm_exec_mode.strip().lower() in ("executing", "execute", "ready"):
        norm_exec_mode = "approved"

    res = working_memory_service.upsert_working_task(
        title=title,
        project_id=resolved_pid,
        task_id=task_id,
        cwd=cwd,
        git_branch=git_branch,
        status=status,
        execution_mode=norm_exec_mode,
        goal=goal,
        completed_steps=completed_steps,
        add_completed_step=add_completed_step,
        current_state=current_state,
        temp_constraints=temp_constraints,
        add_temp_constraint=add_temp_constraint,
        failed_attempts=failed_attempts,
        add_failed_attempt=add_failed_attempt,
        blockers=blockers,
        add_blocker=add_blocker,
        next_steps=next_steps,
        agent=caller,
        last_agent=caller,
        source_conversation_id=source_conversation_id,
        cited_memory_ids=cited_memory_ids,
        citation_note=citation_note,
        continue_branch_task=continue_branch_task,
        expected_version=expected_version,
        allow_skip_trivial=True,
    )
    if res.get("skipped"):
        return {
            "ok": True,
            "skipped": True,
            "action": "skipped_trivial_qa",
            "reason": res.get("reason"),
        }
    return {
        "ok": True,
        "action": "updated" if res.get("version", 1) > 1 else "created",
        "task_id": res["id"],
        "id": res["id"],
        "project_id": res.get("project_id"),
        "status": res["status"],
        "execution_mode": res["execution_mode"],
        "must_not_execute": res["must_not_execute"],
        "version": res["version"],
        "cited_memory_ids": res.get("cited_memory_ids") or list(cited_memory_ids or []),
        "task": res,
    }


@contextcore_mcp.tool(
    title="Promote validated task conclusion to conditional lesson",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def promote_task_lesson(
    task_id: str,
    title: str,
    content: str,
    applicability: dict[str, Any],
    category: str = "lesson",
    evidence_quote: str | None = None,
    tags: list[str] | None = None,
    pinned: bool = False,
    complete_task: bool = False,
) -> dict[str, Any]:
    """Promote a verified task outcome into conditional long-term memory (requires non-empty applicability).

    The task must already reference an existing same-project source conversation, and
    `evidence_quote` must be copied from its messages and relate to the lesson. Agent
    promotions are recorded as `source_backed`; a conflicting promotion returns
    `promoted=False` and leaves the task unchanged.
    """
    caller = _actor()
    _observe("memory_write", "promote_task_lesson")
    res = working_memory_service.promote_task_conclusion(
        task_id=task_id,
        title=title,
        content=content,
        category=category,
        applicability=applicability,
        tags=tags,
        pinned=pinned,
        evidence_quote=evidence_quote,
        actor=caller,
        complete_task=complete_task,
    )
    mem = res.get("memory")
    conflict = res.get("conflict")
    promoted = bool(res.get("promoted")) and mem is not None
    return {
        "ok": True,
        "action": res.get("action"),
        "promoted": promoted,
        "task_id": task_id,
        "content_id": mem["id"] if mem else None,
        "id": mem["id"] if mem else None,
        "conflict_id": conflict["id"] if conflict else None,
        "conflicting_memory_id": conflict.get("existing_id") if conflict else None,
        "task": res.get("task"),
        "memory": mem,
        "conflict": conflict,
        "summary_note": (
            f"💡 已从任务提炼条件经验：[{category}] {title}"
            if promoted
            else f"💡 提炼经验与现有规则冲突，已转入待确认：{title}"
        ),
    }


@contextcore_mcp.tool(
    title="Cite referenced memories",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def cite_memory(
    memory_ids: list[str],
    reason: str = "",
    task_id: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Explicitly record that loaded memories were genuinely used as basis for work or decisions."""
    caller = _actor()
    _observe("memory_write", "cite_memory")
    resolved_pid: str | None = None
    if task_id and task_id.startswith("tsk_"):
        try:
            wt = working_memory_service.get_working_task(task_id, include_events=False)
            resolved_pid = wt.get("project_id")
        except Exception:
            resolved_pid = None

    events = telemetry_service.record_cited(
        memory_ids,
        agent=caller,
        project_id=resolved_pid,
        task_id=task_id,
        session_key=conversation_id,
        surface="cite_memory",
        note=reason or None,
        strict=True,
    )
    cited_ids = [e["content_id"] for e in events]
    return {
        "ok": True,
        "cited_ids": cited_ids,
        "count": len(cited_ids),
        "summaries": telemetry_service.get_usage_summaries(cited_ids),
    }


@contextcore_mcp.tool(
    title="Mark memory as outdated",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def mark_memory_outdated(
    memory_id: str,
    reason: str = "",
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Mark an obsolete memory item as outdated so it stops being injected while remaining traceable."""
    caller = _actor()
    _observe("memory_write", "mark_memory_outdated")
    updated = content_service.mark_outdated(
        memory_id,
        reason=reason or None,
        actor=caller,
        expected_version=expected_version,
    )
    return {
        "ok": True,
        "action": "marked_outdated",
        "id": updated["id"],
        "content_id": updated["id"],
        "status": updated["status"],
        "version": updated["version"],
        "outdated_reason": updated.get("outdated_reason"),
        "memory": updated,
        "summary_note": f"💡 已将旧记忆标记为过时：{updated['title']}",
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
    """Update a working task or legacy task while preserving CAS semantics."""
    caller = _actor()
    _observe("memory_write", "update_task_progress")
    if task_id.startswith("tsk_"):
        if status not in {"pending", "active", "in_progress", "blocked", "paused", "completed", "cancelled"}:
            raise ValueError("Task status must be pending, active, in_progress, blocked, paused, completed, or cancelled")
        mapped_status = "active" if status == "pending" else status
        updated_wt = working_memory_service.patch_working_task(
            task_id,
            status=mapped_status,
            current_state=detail,
            expected_version=expected_version,
            agent=caller,
            last_agent=caller,
        )
        return {
            "ok": True,
            "task_id": task_id,
            "status": status,
            "version": updated_wt["version"],
            "working_task": updated_wt,
        }

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
        agent=caller,
    )
    # Mirror legacy task update into working_tasks so both views stay converged
    try:
        working_memory_service.ensure_legacy_tasks_synced(task.get("project_id"))
        with db() as conn:
            wt_row = conn.execute(
                "SELECT id FROM working_tasks WHERE legacy_content_id = ? LIMIT 1",
                (task_id,),
            ).fetchone()
        if wt_row:
            mapped_status = "active" if status == "pending" else status
            working_memory_service.patch_working_task(
                wt_row["id"],
                status=mapped_status,
                current_state=detail,
                agent=caller,
                last_agent=caller,
            )
    except Exception:
        pass
    return {"ok": True, "task_id": task_id, "status": status, "version": updated["version"]}


@contextcore_mcp.tool(
    title="Resolve conflict",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
)
def resolve_conflict(
    conflict_id: str,
    resolution: str,
    expected_version: int | None = None,
    confirmed: StrictBool = False,
) -> dict[str, Any]:
    """Resolve a conflict between existing and proposed memory (keep, replace, merge, or both)."""
    if confirmed is not True:
        return {"ok": False, "error": "Confirmation requires confirmed=True to resolve a conflict"}
    caller = _actor()
    resolved = conflict_service.resolve_conflict(
        conflict_id=conflict_id,
        resolution=resolution,
        expected_version=expected_version,
        actor=caller,
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
    annotations=READ_ONLY_ANNOTATIONS,
)
def get_system_status() -> dict[str, Any]:
    """Retrieve honest status of ContextCore services, adapters, and background capture."""
    _observe("memory_read", "get_system_status")
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


@contextcore_mcp.tool(
    title="Read context evidence",
    annotations=READ_ONLY_ANNOTATIONS,
)
def read_evidence(evidence_id: str, offset: int = 0, limit: int = 2000) -> dict[str, Any]:
    """Read a versioned evidence passage; stale or deleted sources cannot be revived."""
    from app.services import retrieval_service
    _observe("memory_read", "read_evidence")
    return retrieval_service.read_evidence(evidence_id, offset, limit)


_MAX_MCP_TRANSCRIPT_PAGE_BYTES = 48_000
_MAX_MCP_SINGLE_ITEM_BYTES = 24_000


def _clip_utf8_head_tail(text: str, max_bytes: int) -> tuple[str, int]:
    """Trim an oversized text field by keeping head and tail within max_bytes UTF-8."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, 0
    keep_each = max(256, (max_bytes - 96) // 2)
    head = raw[:keep_each].decode("utf-8", errors="ignore")
    tail = raw[-keep_each:].decode("utf-8", errors="ignore")
    omitted = max(0, len(raw) - len(head.encode("utf-8")) - len(tail.encode("utf-8")))
    return f"{head}\n... [truncated {omitted} bytes] ...\n{tail}", omitted


def _bound_mcp_transcript_item(item: dict[str, Any]) -> dict[str, Any]:
    """Bound an oversized single transcript item so one giant message never breaks MCP paging."""
    encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= _MAX_MCP_SINGLE_ITEM_BYTES:
        return item
    bounded = dict(item)
    omitted_total = 0
    if isinstance(bounded.get("tool_calls"), list):
        compact_calls = []
        for call in bounded["tool_calls"]:
            call_bytes = len(json.dumps(call, ensure_ascii=False).encode("utf-8"))
            if call_bytes > 2048 and isinstance(call, dict):
                copy_call = dict(call)
                for key in ("arguments", "result", "output", "content"):
                    val = copy_call.get(key)
                    if isinstance(val, str) and len(val.encode("utf-8")) > 1024:
                        clipped, omitted = _clip_utf8_head_tail(val, 1024)
                        copy_call[key] = clipped
                        omitted_total += omitted
                if isinstance(copy_call.get("function"), dict) and isinstance(copy_call["function"].get("arguments"), str):
                    fn = dict(copy_call["function"])
                    clipped, omitted = _clip_utf8_head_tail(fn["arguments"], 1024)
                    fn["arguments"] = clipped
                    omitted_total += omitted
                    copy_call["function"] = fn
                compact_calls.append(copy_call)
            else:
                compact_calls.append(call)
        bounded["tool_calls"] = compact_calls
    if isinstance(bounded.get("content"), str):
        remaining = max(1024, _MAX_MCP_SINGLE_ITEM_BYTES - 2048)
        clipped, omitted = _clip_utf8_head_tail(bounded["content"], remaining)
        bounded["content"] = clipped
        omitted_total += omitted
    if omitted_total > 0:
        bounded["truncated_bytes"] = omitted_total
    return bounded


def _bound_mcp_transcript_page(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pack as many sequential items as fit inside _MAX_MCP_TRANSCRIPT_PAGE_BYTES (always >= 1 if non-empty)."""
    packed: list[dict[str, Any]] = []
    running_bytes = 512  # JSON envelope headroom
    for raw_item in items:
        candidate = _bound_mcp_transcript_item(raw_item)
        item_bytes = len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) + 4
        if packed and running_bytes + item_bytes > _MAX_MCP_TRANSCRIPT_PAGE_BYTES:
            break
        packed.append(candidate)
        running_bytes += item_bytes
    return packed


@contextcore_mcp.tool(
    title="Read paged conversation transcript messages",
    annotations=READ_ONLY_ANNOTATIONS,
)
def read_conversation_messages(
    conversation_id: str,
    offset: int = 0,
    limit: int = 30,
) -> dict[str, Any]:
    """Page stored full-transcript messages (0-based ordinal offset) for a conversation identified by exact conversation_id or unambiguous native external_id/UUID."""
    from app.services import conversation_service

    raw = str(conversation_id or "").strip()
    if not raw or raw.isdigit():
        raise ValueError("Pass an exact conversation_id or unambiguous native external_id/UUID")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be nonnegative")
    _observe("memory_read", "read_conversation_messages")

    with db() as conn:
        row = conn.execute("SELECT id FROM conversations WHERE id = ?", (raw,)).fetchone()
        if row:
            resolved_id = row["id"]
        else:
            native_uuid = bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", raw))
            if native_uuid:
                rows = conn.execute(
                    "SELECT id, external_id, agent, title FROM conversations "
                    "WHERE lower(external_id) = lower(?) OR lower(external_id) LIKE ? ESCAPE '\\'",
                    (raw, "%:" + conversation_service.escape_like(raw.lower())),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, external_id, agent, title FROM conversations WHERE external_id = ?",
                    (raw,),
                ).fetchall()
            if len(rows) > 1:
                raise HTTPException(
                    422,
                    {
                        "code": handoff_service.AMBIGUOUS_CONVERSATION_CODE,
                        "message": "Multiple conversations match; pass an exact conversation_id.",
                        "candidates": [
                            {
                                "conversation_id": r["id"],
                                "external_id": r["external_id"],
                                "agent": r["agent"],
                                "title": r["title"],
                            }
                            for r in rows
                        ],
                    },
                )
            resolved_id = rows[0]["id"] if rows else raw

    page = conversation_service.conversation_messages_page(resolved_id, limit=limit, offset=offset)
    bounded_items = _bound_mcp_transcript_page(page["items"])
    next_offset = (
        offset + len(bounded_items)
        if offset + len(bounded_items) < page["total"]
        else None
    )
    return {
        "ok": True,
        "conversation_id": resolved_id,
        "items": bounded_items,
        "total": page["total"],
        "limit": page["limit"],
        "offset": page["offset"],
        "returned_count": len(bounded_items),
        "next_offset": next_offset,
        "has_more": next_offset is not None,
    }


@contextcore_mcp.tool(
    title="Discover relay conversations across agents",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
def discover_relay_conversations(
    limit: int = 15,
    language: str = 'zh',
    cwd: str | None = None,
    project_id: str | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Request background refresh and page indexed conversations, newest first. sync_pending means newer sessions may still be importing; unlinked records are labeled separately. Use next_offset and exact conversation_id/native external_id to select."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    _observe("memory_read", "discover_relay_conversations")
    consumer = authenticated_agent.get()
    return handoff_service.discover_relay_conversations(
        consumer=consumer if consumer in handoff_service.ALLOWED_TARGET_AGENTS else None,
        limit=limit,
        offset=offset,
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
    offset: int = 0,
) -> dict[str, Any]:
    """Page stored conversation metadata with exact total_candidates/omitted_count while background refresh proceeds. sync_pending is not an empty native history; unknown-project sessions are labeled separately."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    _observe("memory_read", "list_relay_conversations")
    consumer = authenticated_agent.get()
    return handoff_service.discover_relay_conversations(
        consumer=consumer if consumer in handoff_service.ALLOWED_TARGET_AGENTS else None,
        limit=limit,
        offset=offset,
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
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """
    One-step conversation-first relay for `/contextcore`:
    - Always pass `cwd` (current working directory) so candidate conversations are scoped to the current project.
    - If `conversation_id` is omitted, requests background refresh and pages stored conversation metadata.
      If `sync_pending=True`, newer native sessions may still be importing; retry the list rather than claim no history exists.
    - Use `next_offset` to page the list; prefer its stable conversation_id or native external_id over a number.
    - Once selected, refreshes only that source without calling any background distillation model, returning metadata,
      initial/recent turns, and `transcript_paging`. When `transcript_paging.complete` is false (or `coverage.omitted_messages > 0`),
      sequentially call `read_conversation_messages(conversation_id=..., offset=0, limit=30)` until `next_offset` is null to read
      the ENTIRE transcript, then compact the whole conversation in context without presenting a handoff card.
      A bare native UUID is accepted only when unambiguous. Without a concrete task in `query`, returns `status='loaded'`
      (`must_not_execute=True`) so the Agent reports the compacted full-conversation context and waits for the user's next
      instruction; when a concrete task instruction is provided in `query`, returns `continuation.status='ready'`
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
        limit=limit,
        offset=offset,
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
    max_tokens: int = 32000,
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
