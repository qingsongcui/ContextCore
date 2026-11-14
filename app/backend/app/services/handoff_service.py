import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from app.config import settings
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode, decode
from app.integrations.constants import SUPPORTED_HOSTS
from app.services import project_service
from app.services.handoff_text import CardText, system, render, check_language

ALLOWED_TARGET_AGENTS = frozenset(SUPPORTED_HOSTS)
STALE_HANDOFF_DETAIL = "Source data changed or is unavailable; please regenerate this handoff card."
STALE_HANDOFF_MESSAGE = "来源已变化或不可用，请重新生成接棒卡。"
CODE_STATE_STALE_DETAIL = (
    "Cannot confirm or read handoff: git commit or uncommitted changes drifted since card creation; "
    "please regenerate this handoff card."
)
HANDOFF_CARD_FIELDS = ("goal", "progress", "decisions", "evidence", "blockers", "next_steps")
# D005 names stay on the public readback. Stored slots keep decisions/evidence.
D005_FIELD_MAP = (
    ("goal", "goal"),
    ("progress", "progress"),
    ("constraints", "decisions"),
    ("scene", "evidence"),
    ("blockers", "blockers"),
    ("next", "next_steps"),
)
D005_READBACK_FIELDS = tuple(name for name, _slot in D005_FIELD_MAP)
AMBIGUOUS_TASK_CODE = "ambiguous_task"
AMBIGUOUS_PROJECT_CODE = "ambiguous_project"
AMBIGUOUS_CONVERSATION_CODE = "ambiguous_conversation"


def _branch_label(conv: dict[str, Any], language: str = "zh") -> str:
    """Branch recorded for the conversation, or a labeled live-checkout fallback for legacy rows."""
    stored = str(conv.get("git_branch") or "").strip()
    if stored:
        return stored
    head = str(conv.get("git_head") or "").strip()
    if head:
        return f"HEAD@{head[:12]}"
    cwd = str(conv.get("cwd") or "").strip()
    checkout = project_service.read_git_checkout(cwd) if cwd else None
    suffix = " (current checkout)" if language == "en" else "（当前目录）"
    if checkout and not checkout.get("detached") and checkout.get("branch"):
        return f"{checkout['branch']}{suffix}"
    if checkout and checkout.get("detached"):
        shown = str(checkout.get("head") or "")[:12]
        return f"{f'HEAD@{shown}' if shown else 'HEAD'}{suffix}"
    return ""


def _session_directory_lines(local, session_cwd: str | None, project_dirs: list[str]) -> list[str]:
    """The working directory of a loaded conversation is that conversation's folder, not every project folder."""
    session = str(session_cwd or "").strip()
    dirs = [str(item) for item in project_dirs if str(item or "").strip()]
    if session:
        lines = [local("- 工作目录: ", "- Working directory: ") + session]
        session_key = project_service.cwd_identity(session)
        others = [
            item for item in dirs
            if project_service.cwd_identity(item) != session_key
        ]
        if others:
            lines.append(local(
                "- 项目其他已登记目录（不是这个会话的目录）: ",
                "- Other registered project directories (not this conversation): ",
            ) + ", ".join(others))
        return lines
    if dirs:
        return [local(
            "- 项目已登记目录（未选定会话目录）: ",
            "- Registered project directories (no conversation directory selected): ",
        ) + ", ".join(dirs)]
    return [local("- 工作目录: ", "- Working directory: ") + local("未记录", "Not recorded")]


def _code_state_text(state: dict[str, Any]) -> CardText:
    where = str(state.get("cwd") or "")
    kind = state.get("kind")
    if kind == "not_git":
        return system("代码状态: 目录不是 git 仓库：", "Code state: directory is not a git repository: ") + where
    if kind != "git" or not state.get("complete"):
        return system(
            "代码状态: 提交或未提交改动未能核验：",
            "Code state: commits or uncommitted changes could not be checked: ",
        ) + where
    head = str(state.get("head") or "")[:12]
    if state.get("detached"):
        ref = system("游离提交 ", "detached commit ") + head
    elif state.get("branch"):
        ref = system("分支 ", "branch ") + str(state["branch"]) + " @ " + head
    else:
        ref = system("分支未记录 @ ", "branch not recorded @ ") + head
    dirt = (
        system("，工作区干净", ", clean worktree")
        if not state.get("dirty")
        else system("，有未提交改动", ", uncommitted changes")
    )
    return system("代码状态: ", "Code state: ") + where + " " + ref + dirt


def d005_readback(card: dict[str, Any] | None) -> dict[str, str]:
    source = card if isinstance(card, dict) else {}
    return {name: str(source.get(slot) or "") for name, slot in D005_FIELD_MAP}


def has_explicit_confirmation(handoff: dict[str, Any], conn) -> bool:
    card = handoff.get('card') or {}
    proof = handoff.get('confirmation') or (card.get('_confirmation') if isinstance(card, dict) else None)
    valid = bool(isinstance(proof, dict) and proof.get('protocol') == 2
                and proof.get('actor') == handoff.get('target_agent')
                and proof.get('at') == handoff.get('confirmed_at')
                and proof.get('audit_id'))
    return valid and bool(conn.execute(
        "SELECT 1 FROM audit_logs WHERE id=? AND action='handoff.confirm' AND actor=? AND target=?",
        (proof['audit_id'], handoff.get('target_agent'), handoff.get('id')),
    ).fetchone())


def present_handoff(handoff: dict[str, Any], conn, language: str = 'zh') -> dict[str, Any]:
    check_language(language)
    card = handoff.get("card") if isinstance(handoff.get("card"), dict) else {}
    if '_confirmation' in card:
        handoff['confirmation'] = card['_confirmation']
        card = {key: value for key, value in card.items() if key != '_confirmation'}
        handoff['card'] = card
    translations = card.get('_system_text')
    handoff['system_text_language'] = language if isinstance(translations, dict) else 'original'
    handoff['card'] = card = {
        key: render(value, translations.get(key, []) if isinstance(translations, dict) else [], language)
        for key, value in card.items() if key != '_system_text'
    }
    handoff["readback"] = d005_readback(card)
    if handoff.get('status') == 'confirmed' and not has_explicit_confirmation(handoff, conn):
        handoff['stored_status'] = 'confirmed'
        handoff['status'] = 'awaiting_confirmation'
        handoff['confirmation_review_required'] = True
    return handoff


def budget_blocked_continuation(continuation, language: str = 'en'):
    if not continuation:
        return None
    check_language(language)
    notice = ("预算不足，当前内容未知；增加预算后重新回读完整接棒卡。不要确认或执行。"
              if language == 'zh' else
              "The budget is too small and the current content is unknown. Increase the budget and read the complete card again. Do not confirm or execute.")
    card = {field: notice for field in HANDOFF_CARD_FIELDS}
    return {
        "status": "insufficient_budget",
        "handoff_id": continuation.get("handoff_id"),
        "project_id": continuation.get("project_id"),
        "target_agent": continuation.get("target_agent"),
        "must_not_execute": True,
        "resume_from": None,
        "card": card,
        "readback": d005_readback(card),
        "message": notice,
    }


def _tags_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        parsed = decode(value, [])
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []


def _synthesize_working_task_body(wt: dict[str, Any]) -> str:
    parts: list[str] = []
    goal = str(wt.get("goal") or "").strip()
    state = str(wt.get("current_state") or "").strip()
    if goal:
        parts.append(goal)
    if state and state != goal:
        parts.append(f"当前状态: {state}")
    steps = wt.get("completed_steps") or []
    if steps:
        step_labels = [
            str(s.get("step") if isinstance(s, dict) else s).strip()
            for s in steps[:5]
            if str(s.get("step") if isinstance(s, dict) else s).strip()
        ]
        if step_labels:
            parts.append(f"已完成: {'; '.join(step_labels)}")
    tcs = wt.get("temp_constraints") or []
    if tcs:
        tc_labels = [str(c).strip() for c in tcs[:5] if str(c).strip()]
        if tc_labels:
            parts.append(f"临时约束: {'; '.join(tc_labels)}")
    return " | ".join(parts) or str(wt.get("title") or "")


def _working_task_to_handoff_item(
    wt: dict[str, Any],
    legacy_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    exec_mode = wt.get("execution_mode") if wt.get("execution_mode") in ("discussion_only", "approved") else "approved"
    wt_status = str(wt.get("status") or "active")
    if legacy_row is not None:
        merged = dict(legacy_row)
        merged["_working_task_id"] = wt["id"]
        merged["_is_working_task"] = False
        merged["_wt_status"] = wt_status
        merged["execution_mode"] = exec_mode
        merged["must_not_execute"] = exec_mode == "discussion_only"
        merged["goal"] = wt.get("goal") or merged.get("content") or ""
        merged["current_state"] = wt.get("current_state") or ""
        merged["completed_steps"] = list(wt.get("completed_steps") or [])
        merged["temp_constraints"] = list(wt.get("temp_constraints") or [])
        merged["failed_attempts"] = list(wt.get("failed_attempts") or [])
        merged["blockers"] = list(wt.get("blockers") or [])
        merged["next_steps"] = list(wt.get("next_steps") or [])
        merged["source_conversation_id"] = wt.get("source_conversation_id")
        merged["cwd"] = wt.get("cwd")
        merged["git_branch"] = wt.get("git_branch")
        if not str(merged.get("content") or "").strip() and wt.get("current_state"):
            merged["content"] = str(wt["current_state"])
        return merged

    mapped_tag_status = "pending" if wt_status == "active" else wt_status
    return {
        "id": wt["id"],
        "_working_task_id": wt["id"],
        "_is_working_task": True,
        "_wt_status": wt_status,
        "project_id": wt["project_id"],
        "kind": "memory",
        "category": "task",
        "title": wt["title"],
        "content": _synthesize_working_task_body(wt),
        "tags": [f"status:{mapped_tag_status}"],
        "agent": wt.get("last_agent"),
        "status": "active" if wt_status in ("active", "in_progress", "blocked") else wt_status,
        "execution_mode": exec_mode,
        "must_not_execute": exec_mode == "discussion_only",
        "goal": wt.get("goal") or "",
        "current_state": wt.get("current_state") or "",
        "completed_steps": list(wt.get("completed_steps") or []),
        "temp_constraints": list(wt.get("temp_constraints") or []),
        "failed_attempts": list(wt.get("failed_attempts") or []),
        "blockers": list(wt.get("blockers") or []),
        "next_steps": list(wt.get("next_steps") or []),
        "cwd": wt.get("cwd"),
        "git_branch": wt.get("git_branch"),
        "source_conversation_id": wt.get("source_conversation_id"),
        "legacy_content_id": None,
        "version": int(wt.get("version") or 1),
        "created_at": wt.get("created_at"),
        "updated_at": wt.get("updated_at"),
        "completed_at": wt.get("completed_at"),
    }


def task_progress_status(item: dict[str, Any] | None) -> str:
    if not item:
        return "pending"
    wt_st = item.get("_wt_status")
    if wt_st:
        if wt_st == "active" and not item.get("_is_working_task"):
            for tag in _tags_list(item.get("tags")):
                if tag.startswith("status:") and tag.split(":", 1)[1]:
                    return tag.split(":", 1)[1]
            return "pending"
        return str(wt_st)
    for tag in _tags_list(item.get("tags")):
        if tag.startswith("status:") and tag.split(":", 1)[1]:
            return tag.split(":", 1)[1]
    return "pending"


def is_open_task(item: dict[str, Any] | None) -> bool:
    if not item:
        return False
    if item.get("category") not in (None, "task"):
        return False
    wt_st = item.get("_wt_status")
    if wt_st:
        return wt_st in ("active", "in_progress", "blocked")
    if item.get("status") != "active":
        return False
    return task_progress_status(item) not in ("completed", "paused", "cancelled")


def list_project_tasks(conn, project_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from app.services import working_memory_service

    working_memory_service.ensure_schema(conn)
    working_memory_service.ensure_legacy_tasks_synced(project_id, conn=conn)
    wt_items = working_memory_service.list_working_tasks(
        project_id,
        active_only=False,
        include_events=False,
        limit=200,
        conn=conn,
    )
    legacy_rows = conn.execute(
        """
        SELECT *
        FROM contents
        WHERE project_id = ? AND category = 'task'
        ORDER BY updated_at DESC
        """,
        (project_id,),
    ).fetchall()
    legacy_by_id = {row["id"]: row_to_dict(row) for row in legacy_rows}

    open_tasks: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    for wt in wt_items:
        legacy_id = wt.get("legacy_content_id")
        legacy_row = legacy_by_id.get(legacy_id) if legacy_id else None
        if legacy_id and legacy_row is None:
            continue
        if legacy_row is not None and legacy_row.get("status") != "active" and wt["status"] in ("active", "in_progress", "blocked"):
            # Retired/outdated legacy task row is no longer open
            if wt["status"] == "completed":
                completed.append(_working_task_to_handoff_item(wt, legacy_row))
            continue
        unified = _working_task_to_handoff_item(wt, legacy_row)
        if wt["status"] in ("active", "in_progress", "blocked"):
            open_tasks.append(unified)
        elif wt["status"] == "completed":
            completed.append(unified)
        # 'paused' and 'cancelled' working_tasks exit active todo without triggering ambiguous_task blockers.

    return open_tasks, completed


def _task_candidate(item: dict[str, Any]) -> dict[str, Any]:
    exec_mode = item.get("execution_mode") if item.get("execution_mode") in ("discussion_only", "approved") else "approved"
    return {
        "id": item["id"],
        "title": str(item.get("title") or ""),
        "status": task_progress_status(item),
        "execution_mode": exec_mode,
        "must_not_execute": exec_mode == "discussion_only",
    }


def _ambiguous_task_detail(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "code": AMBIGUOUS_TASK_CODE,
        "message": "Multiple open tasks in this project; select task_id before creating a handoff card.",
        "candidates": [_task_candidate(item) for item in candidates],
    }


def _current_source_messages(conversation):
    messages = [m for m in (conversation or {}).get("messages", []) if isinstance(m, dict)]
    # A working scene is the current user turn and its subsequent observations,
    # not obsolete instructions from earlier turns.
    start = next((index for index in range(len(messages) - 1, -1, -1)
                  if messages[index].get("role") == "user"), 0)
    return messages[start:]


def _tool_identity(tool: dict[str, Any]) -> tuple[str, str, str]:
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    name = str(function.get("name") or tool.get("name") or tool.get("command") or "")
    arguments = function.get("arguments")
    if arguments is None:
        arguments = tool.get("input", tool.get("arguments", ""))
    if not isinstance(arguments, str):
        arguments = encode(arguments) if arguments not in (None, "") else ""
    return str(tool.get("id") or tool.get("tool_call_id") or ""), name, arguments


def assemble_tool_pairs(messages, process_summary=None) -> dict[str, list]:
    """Keep each tool call with its own result. Never slice either side.

    A result with no preceding call is omitted from the pair list. Its text is
    not rewritten into a finished outcome. A call with no result stays visible
    with result None.
    """
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    omitted: list[dict[str, str]] = []

    def add_call(ident: str, name: str, arguments: str) -> dict[str, Any]:
        item = {"id": ident, "name": name, "arguments": arguments, "result": None}
        calls.append(item)
        if ident:
            by_id.setdefault(ident, item)
        return item

    def add_result(ident: str, result: Any) -> None:
        text = "" if result is None else str(result)
        if ident and ident in by_id and by_id[ident]["result"] is None:
            by_id[ident]["result"] = text
            return
        omitted.append({"id": ident, "reason": "unpaired_tool_result"})

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        for tool in message.get("tool_calls") or []:
            if isinstance(tool, dict):
                add_call(*_tool_identity(tool))
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_use":
                    add_call(*_tool_identity(block))
                elif kind == "tool_result":
                    add_result(str(block.get("tool_use_id") or ""), block.get("content"))
        if str(message.get("role") or "") == "tool":
            add_result(str(message.get("tool_call_id") or ""), message.get("content"))

    # A stored summary list is used only when the messages did not already
    # carry structured calls. Re-reading both would duplicate the same pair.
    summary = process_summary
    if not calls and isinstance(summary, str):
        parsed = decode(summary, None)
        summary = parsed if isinstance(parsed, list) else None
    if not calls and isinstance(summary, list):
        for step in summary:
            if not isinstance(step, dict):
                continue
            ident, name, arguments = _tool_identity(step)
            has_outcome = "outcome" in step or "result" in step
            if name and has_outcome:
                item = add_call(ident, name, arguments)
                item["result"] = "" if step.get("outcome", step.get("result")) is None else str(step.get("outcome", step.get("result")))
            else:
                omitted.append({"id": ident, "reason": "incomplete_tool_record"})

    for item in calls:
        if item["result"] is None:
            omitted.append({"id": item["id"], "name": item["name"], "reason": "unpaired_tool_call"})
    return {"pairs": calls, "omitted": omitted}


def _cap_card_excerpt(text: str, *, max_chars: int = 4000, max_bytes: int = 4000) -> str:
    """Bound individual conversation excerpts in handoff cards so serialized cards stay within MCP budgets."""
    if not text:
        return ""
    out = text
    truncated = False
    if len(out) > max_chars:
        out = out[:max_chars]
        truncated = True
    encoded = out.encode("utf-8")
    if len(encoded) > max_bytes:
        out = encoded[: max(0, max_bytes - 4)].decode("utf-8", errors="ignore")
        truncated = True
    if truncated:
        out = out.rstrip() + "…"
    return out


def _current_source_messages_with_meta(conversation) -> list[tuple[int, dict[str, Any]]]:
    messages = [m for m in (conversation or {}).get("messages", []) if isinstance(m, dict)]
    start = next((index for index in range(len(messages) - 1, -1, -1)
                  if messages[index].get("role") == "user"), 0)
    return [(idx + 1, messages[idx]) for idx in range(start, len(messages))]


def _render_tool_pairs(
    extracted: dict[str, list],
    *,
    max_piece_chars: int | None = None,
    max_piece_bytes: int | None = None,
) -> CardText | None:
    pieces: list[Any] = []
    was_truncated = False
    for pair in extracted["pairs"]:
        args_str = str(pair["arguments"] or "")
        if max_piece_chars is not None and max_piece_bytes is not None:
            capped_args = _cap_card_excerpt(args_str, max_chars=max_piece_chars, max_bytes=max_piece_bytes)
            if capped_args != args_str:
                was_truncated = True
            args_str = capped_args
        prefix = f"{pair['name'] or 'tool'} {args_str} -> "
        if pair["result"] is None:
            pieces.append(CardText(prefix) + system("结果未记录", "result not recorded"))
        else:
            res_str = str(pair["result"])
            if max_piece_chars is not None and max_piece_bytes is not None:
                capped_res = _cap_card_excerpt(res_str, max_chars=max_piece_chars, max_bytes=max_piece_bytes)
                if capped_res != res_str:
                    was_truncated = True
                res_str = capped_res
            pieces.append(prefix + res_str)
    if pieces:
        header = (
            system("工具调用与结果（节选）: ", "Tool calls and results (excerpt): ")
            if was_truncated
            else system("工具调用与结果（未截断）: ", "Tool calls and results (not truncated): ")
        )
        rendered = header + CardText("; ").join(pieces)
    else:
        rendered = CardText("")
    if extracted["omitted"]:
        reasons = ", ".join(sorted({str(item.get("reason") or "") for item in extracted["omitted"]}))
        notice = system(
            "未配对的工具记录未写成已完成结果: ",
            "Unpaired tool records were not written as completed results: ",
        ) + reasons
        rendered = (rendered + CardText(" | ") + notice) if pieces else notice
    return rendered or None


def _raw_source_quotes(
    conversation: dict[str, Any] | None,
    pattern: str,
    *,
    max_quote_chars: int = 1000,
    max_quote_bytes: int = 1000,
    max_quotes: int = 1,
) -> list[str]:
    """Retain explicit source statements without promoting them to verified facts.

    Raw-history handoff must work with compute disabled. This is bounded quotation,
    not model extraction: unlabeled/implicit intentions remain unknown. Only the
    selected predecessor is eligible; source revisions already cover its body.
    """
    if not conversation:
        return []
    sources = [
        (str(m.get("role")), str(m.get("content") or ""))
        for m in reversed(_current_source_messages(conversation))
        if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
    ]
    summary = conversation.get("process_summary")
    if isinstance(summary, str) and summary.strip():
        sources.append((system("过程摘要", "process summary"), summary))
    quotes = []
    seen = set()
    for role, text in sources:
        statement = text.strip()
        if not statement or statement in seen or not re.search(pattern, statement, re.I):
            continue
        seen.add(statement)
        bounded = _cap_card_excerpt(statement, max_chars=max_quote_chars, max_bytes=max_quote_bytes)
        quotes.append(system("来源 ", "Source ") + role + system(" 原话（待核对）: ", " quotation (unverified): ") + bounded)
        if len(quotes) >= max_quotes:
            break
    return quotes


def _applicable_memories(conn, project_id, task, conversation, categories):
    """Project-wide memories apply unless explicitly tied to other work.

    Shared access is not global applicability: unscoped memories need an
    existing relation to this project/task/source before becoming card rules.
    """
    from app.services import governance_guardrails

    selected_ids = {value for value in (
        project_id,
        (task or {}).get("id"),
        (task or {}).get("_working_task_id"),
        (conversation or {}).get("id"),
    ) if value}
    category_slots = ",".join("?" for _ in categories)
    selected_conversation = (conversation or {}).get("id")
    rows = conn.execute(
        f"""SELECT * FROM contents c WHERE status='active'
            AND category IN ({category_slots})
            AND (project_id = ? OR (project_id IS NULL AND (
                EXISTS (SELECT 1 FROM json_each(c.related_ids) r
                        WHERE r.value IN (SELECT value FROM json_each(?)))
                OR c.id IN (SELECT content_id FROM content_sources
                            WHERE source_kind='conversation' AND source_id=? AND deleted=0)
            ))) ORDER BY updated_at DESC,id""",
        (*sorted(categories), project_id, encode(sorted(selected_ids)), selected_conversation),
    )
    result = []
    for row in rows:
        item = row_to_dict(row)
        if governance_guardrails.is_tombstoned_memory(
            conn,
            project_id=item.get("project_id"),
            category=str(item.get("category") or ""),
            title=str(item.get("title") or ""),
            content=str(item.get("content") or ""),
            include_cross_category=True,
        ):
            continue
        relations = set(_tags_list(item.get("related_ids")))
        related_tasks = {
            related["id"] for related in conn.execute(
                """SELECT id FROM contents WHERE category='task'
                   AND id IN (SELECT value FROM json_each(?))""", (encode(sorted(relations)),),
            )
        }
        related_tasks.update(
            related["id"] for related in conn.execute(
                """SELECT id FROM working_tasks
                   WHERE id IN (SELECT value FROM json_each(?))""", (encode(sorted(relations)),),
            )
        )
        task_ids = {
            value for value in ((task or {}).get("id"), (task or {}).get("_working_task_id")) if value
        }
        if related_tasks and not (task_ids & related_tasks):
            continue
        source_matches = bool(selected_conversation and conn.execute(
            """SELECT 1 FROM content_sources WHERE content_id=? AND source_id=?
               AND source_kind='conversation' AND deleted=0 LIMIT 1""",
            (item["id"], selected_conversation),
        ).fetchone())
        linked_tasks = {
            linked["id"] for linked in conn.execute(
                """SELECT DISTINCT t.id FROM content_sources s JOIN content_sources ts
                   ON ts.source_id=s.source_id AND ts.source_kind=s.source_kind
                   JOIN contents t ON t.id=ts.content_id AND t.category='task'
                   WHERE s.content_id=? AND s.deleted=0 AND ts.deleted=0""", (item["id"],),
            )
        }
        if linked_tasks and (not task or task["id"] not in linked_tasks) and not relations & selected_ids and not source_matches:
            continue
        result.append(item)
    return result


def _memory_text(item):
    return f"[{item['id']} v{item['version']}] {item['title']}: {item['content']}"


def _project_scene(conn, project_id):
    project = conn.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        return None
    return {
        "name": project["name"],
        "directories": [row["path"] for row in conn.execute(
            "SELECT path FROM project_directories WHERE project_id=? ORDER BY path", (project_id,),
        )],
    }


_SELECTION_PUNCT_RE = re.compile(r"^[\s,，.。:：;；!！?？、\-—~`'\"()（）\[\]【】]+|[\s,，.。:：;；!！?？、\-—~`'\"()（）\[\]【】]+$")
_LEADING_TRIGGER_RE = re.compile(
    r"^(?:/contextcore(?:-handoff)?|/handoff|/continue|contextcore(?:-handoff)?|"
    r"继续接力|项目接力|上下文接力|接着做吧|继续做吧|接着干吧|接着做|继续做|接着干|"
    r"继续推进|继续进行|接手任务|从头继续|接棒|接力|继续|"
    r"please\s+continue|continue\s+please|continue|resume|handoff|take\s+over)"
    r"(?=[\s,，.。:：;；#\[\(\d]|$|[\u4e00-\u9fff])",
    re.I,
)
_TRAILING_TRIGGER_RE = re.compile(
    r"(?:继续接力|项目接力|上下文接力|接着做吧|继续做吧|接着干吧|接着做|继续做|接着干|"
    r"接棒|接力|继续|please\s+continue|continue\s+please|continue|resume|handoff|take\s+over)$",
    re.I,
)
_PURE_SELECTION_RE = re.compile(
    r"^(?:(?:请|帮我)\s*)*(?:(?:选择|选|第|select|choose|pick|option|item|number|no\.?)\s*)*"
    r"(?:"
    r"#?\s*[1-9]\d*(?:\s*(?:个|条|号|项))?(?:\s*(?:对话|会话|项目|任务|conversation|session|item|option))?"
    r"|\[\s*[1-9]\d*\s*\]"
    r"|\(\s*[1-9]\d*\s*\)"
    r"|conv_[A-Za-z0-9_.-]+"
    r"|(?:antigravity|codex|claude-code|cursor|omp|opencode):[A-Za-z0-9_.:-]+"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r")"
    r"(?:\s*(?:个|条|号|项))?(?:\s*(?:对话|会话|项目|任务|conversation|session|item|option))?$",
    re.I,
)


def _relay_has_task_instruction(query: str | None) -> bool:
    """A numbered/index/ID selection alone is not permission to edit. A concrete task is."""
    text = str(query or "").strip()
    if not text or _is_continuation_prompt(text):
        return False
    rem = re.sub(r"^\[\$?contextcore(?:-handoff)?\]\([^)]+\)\s*", "", text, flags=re.I).strip()
    # Preserve standalone [1] / #1 before outer bracket stripping if already pure
    if _PURE_SELECTION_RE.match(rem):
        return False
    for _ in range(3):
        prev = rem
        rem = _SELECTION_PUNCT_RE.sub("", rem)
        if _PURE_SELECTION_RE.match(rem):
            return False
        rem = _LEADING_TRIGGER_RE.sub("", rem).strip()
        rem = _TRAILING_TRIGGER_RE.sub("", rem).strip()
        rem = _SELECTION_PUNCT_RE.sub("", rem)
        if not rem or _is_continuation_prompt(rem) or _PURE_SELECTION_RE.match(rem):
            return False
        if rem == prev:
            break
    return bool(rem)


def _is_continuation_prompt(text: str | None) -> bool:
    if not text:
        return False
    raw = str(text).strip()
    if re.match(r"^\[\$?contextcore(?:-handoff)?\]\([^)]+\)$", raw, re.I):
        return True
    clean = re.sub(r"[^\w\s\u4e00-\u9fff/-]", "", raw.lower()).strip()
    generic_triggers = {
        "接力", "继续接力", "项目接力",
        "接着做", "继续做", "接棒", "上下文接力", "切换agent", "切换 agent",
        "接手任务", "继续推进", "继续进行", "从头继续", "接着干", "继续",
        "/handoff", "/continue", "/contextcore", "/contextcore-handoff",
        "contextcore", "contextcore-handoff", "continue", "resume", "handoff", "take over",
        "接着做吧", "继续做吧", "接着干吧", "please continue", "continue please",
    }
    return clean in generic_triggers


def _is_continuation_only_session(msgs: list[Any]) -> bool:
    """Return True if a conversation consists solely of continuation triggers and host system context blocks."""
    if not msgs:
        return False
    from app.adapters.codex import _extract_codex_title_candidate

    dict_msgs = [m for m in msgs if isinstance(m, dict)]
    if not dict_msgs:
        return False
    if len(dict_msgs) <= 1 and all(_is_continuation_prompt(str(m.get("content") or "")) for m in dict_msgs):
        return True
    user_contents = [str(m.get("content") or "").strip() for m in dict_msgs if str(m.get("role") or "").lower() == "user"]
    assistant_contents = [str(m.get("content") or "").strip() for m in dict_msgs if str(m.get("role") or "").lower() == "assistant"]
    if not user_contents or len(assistant_contents) > 6:
        return False
    has_continuation_trigger = False
    for u in user_contents:
        if _is_continuation_prompt(u) or "<name>contextcore</name>" in u or "<name>contextcore-handoff</name>" in u:
            has_continuation_trigger = True
            continue
        cleaned = _extract_codex_title_candidate(u)
        if not cleaned or _is_continuation_prompt(cleaned):
            if _is_continuation_prompt(cleaned):
                has_continuation_trigger = True
            continue
        return False
    return has_continuation_trigger

def invalidate_handoffs(conn, source_id: str) -> None:
    """Deletion invalidates copied context as well as the original source."""
    card = {key: "来源已永久删除，请重新生成接棒卡。" for key in (
        "goal", "progress", "decisions", "evidence", "blockers", "next_steps"
    )}
    conn.execute(
        """UPDATE handoffs SET status = 'cancelled', card = ?, confirmed_at = NULL
           WHERE id IN (
               SELECT handoffs.id FROM handoffs, json_each(handoffs.source_ids)
               WHERE json_each.value = ?
           )""",
        (encode(card), source_id),
    )


def list_handoffs(project_id: str | None = None, language: str = 'zh') -> list[dict[str, Any]]:
    with db() as conn:
        if project_id:
            rows = conn.execute(
                "SELECT * FROM handoffs WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM handoffs ORDER BY created_at DESC").fetchall()
        handoffs = [row_to_dict(r) for r in rows]
        result = []
        for handoff in handoffs:
            try:
                validate_source_revisions(conn, handoff)
            except HTTPException:
                # A list remains usable when one card is stale, but never return
                # its copied source text as if it were current or confirmed.
                handoff["status"] = "stale"
                handoff["stale_reason"] = STALE_HANDOFF_MESSAGE
                handoff["card"] = {field: STALE_HANDOFF_MESSAGE for field in HANDOFF_CARD_FIELDS}
                handoff["source_ids"] = []
                handoff["source_revisions"] = {}
                handoff["confirmed_at"] = None
            else:
                handoff.setdefault("stale_reason", None)
            result.append(present_handoff(handoff, conn, language))
        return result


def get_handoff(handoff_id: str, language: str = 'zh') -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Handoff not found")
        return present_handoff(row_to_dict(row), conn, language)


def get_handoff_for_rest(handoff_id: str, language: str = 'zh') -> dict[str, Any]:
    """Return only a source-current card to the REST detail endpoint."""
    handoff = get_handoff(handoff_id, language)
    try:
        with db() as conn:
            validate_source_revisions(conn, handoff)
    except HTTPException:
        # Keep source identifiers and body fragments out of the error response.
        raise HTTPException(status_code=409, detail=STALE_HANDOFF_DETAIL) from None
    handoff.setdefault("stale_reason", None)
    return handoff


def create_handoff(
    project_id: str | None = None,
    target_agent: str = "",
    task_id: str | None = None,
    conversation_id: str | None = None,
    query: str | None = None,
    source_agent: str | None = None,
    actor: str = "user",
    language: str = 'zh',
) -> dict[str, Any]:
    check_language(language)
    clean_target = target_agent.strip().lower()
    if clean_target not in ALLOWED_TARGET_AGENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid target_agent '{target_agent}'. Allowed: {', '.join(sorted(ALLOWED_TARGET_AGENTS))}",
        )

    hid = new_id("hnd")
    now = now_iso()
    source_ids = []

    with db() as conn:
        # A handoff card copies source text before it stores source revisions.
        # Claim the writer slot before the first source read so another writer
        # cannot change a source between card construction and its revision
        # snapshot (which could make stale text appear current).
        conn.execute("BEGIN IMMEDIATE")
        resolved_project_id = None
        resolved_task = None
        resolved_conv = None
        source_candidate_only = False
        source_selection_note = None

        # 1. Explicit project validation
        if project_id:
            p_row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not p_row:
                raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
            resolved_project_id = project_id

        # 2. Explicit task validation
        if task_id:
            from app.services import working_memory_service

            working_memory_service.ensure_schema(conn)
            task_row = conn.execute("SELECT * FROM contents WHERE id = ?", (task_id,)).fetchone()
            if task_row:
                resolved_task = row_to_dict(task_row)
                if resolved_task.get("category") != "task" or resolved_task.get("status") != "active":
                    raise HTTPException(status_code=422, detail="task_id must reference an active task")
                if resolved_Task_pid := resolved_task.get("project_id"):
                    working_memory_service.ensure_legacy_tasks_synced(resolved_Task_pid, conn=conn)
                wt_mirror = conn.execute(
                    "SELECT * FROM working_tasks WHERE legacy_content_id = ?",
                    (task_id,),
                ).fetchone()
                if wt_mirror:
                    resolved_task = _working_task_to_handoff_item(
                        working_memory_service._row_to_task(wt_mirror, []),
                        resolved_task,
                    )
            else:
                wt_row = conn.execute("SELECT * FROM working_tasks WHERE id = ?", (task_id,)).fetchone()
                if not wt_row:
                    raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
                resolved_task = _working_task_to_handoff_item(
                    working_memory_service._row_to_task(wt_row, []),
                    None,
                )

            if resolved_project_id and resolved_task.get("project_id") and resolved_task["project_id"] != resolved_project_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"Task '{task_id}' does not belong to project '{resolved_project_id}'",
                )
            if not is_open_task(resolved_task):
                raise HTTPException(status_code=422, detail="Selected task must be an open task in this project")
            resolved_project_id = resolved_task.get("project_id") or resolved_project_id
            source_ids.append(resolved_task["id"])
            if (
                resolved_task.get("_working_task_id")
                and resolved_task["_working_task_id"] != resolved_task["id"]
                and (
                    resolved_task.get("execution_mode") == "discussion_only"
                    or resolved_task.get("temp_constraints")
                )
            ):
                source_ids.append(resolved_task["_working_task_id"])

        # 3. Explicit conversation validation
        if conversation_id:
            conv_row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not conv_row:
                raise HTTPException(status_code=404, detail=f"Conversation '{conversation_id}' not found")
            resolved_conv = row_to_dict(conv_row)
            if resolved_project_id and resolved_conv.get("project_id") and resolved_conv["project_id"] != resolved_project_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"Conversation '{conversation_id}' does not belong to project '{resolved_project_id}'",
                )
            if resolved_task and resolved_task.get("project_id") and resolved_conv.get("project_id") and resolved_task["project_id"] != resolved_conv["project_id"]:
                raise HTTPException(status_code=422, detail="Task and conversation belong to different projects")
            resolved_project_id = resolved_conv.get("project_id") or resolved_project_id
            source_ids.append(conversation_id)

        # 4. Locate a project, then bind a task only when the choice is unique or explicit.
        # Never pick the latest global task across projects, and never auto-select
        # among parallel open tasks in the same project.
        if not resolved_project_id:
            active_projects = conn.execute(
                "SELECT id, name FROM projects WHERE status = 'active' ORDER BY name"
            ).fetchall()
            if len(active_projects) == 1:
                resolved_project_id = active_projects[0]["id"]
            elif len(active_projects) > 1:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "ambiguous_project",
                        "message": "Multiple active projects exist; project_id must be specified explicitly.",
                        "candidates": [{"id": p["id"], "name": p["name"]} for p in active_projects],
                    },
                )
        open_tasks: list[dict[str, Any]] = []
        completed_tasks: list[dict[str, Any]] = []
        if resolved_project_id:
            open_tasks, completed_tasks = list_project_tasks(conn, resolved_project_id)

        if not resolved_task and resolved_conv:
            linked_tasks = [
                item for item in open_tasks
                if item.get("source_conversation_id") == resolved_conv["id"]
                or conn.execute(
                    """SELECT 1 FROM content_sources WHERE content_id=? AND source_id=?
                       AND source_kind='conversation' AND deleted=0 LIMIT 1""",
                    (item["id"], resolved_conv["id"]),
                ).fetchone()
            ]
            if len(linked_tasks) > 1:
                raise HTTPException(status_code=422, detail=_ambiguous_task_detail(linked_tasks))
            if len(linked_tasks) == 1:
                resolved_task = linked_tasks[0]
                source_ids.append(resolved_task["id"])
            elif len(open_tasks) == 1 and open_tasks[0].get("_is_working_task") and (
                open_tasks[0].get("execution_mode") == "discussion_only"
                or (
                    open_tasks[0].get("agent")
                    and resolved_conv.get("agent")
                    and str(open_tasks[0]["agent"]).strip().lower() == str(resolved_conv["agent"]).strip().lower()
                )
            ):
                resolved_task = open_tasks[0]
                source_ids.append(resolved_task["id"])
            elif len(open_tasks) > 1:
                raise HTTPException(status_code=422, detail=_ambiguous_task_detail(open_tasks))
        elif not resolved_task and resolved_project_id:
            if len(open_tasks) > 1 and (not query or _is_continuation_prompt(query)):
                raise HTTPException(status_code=422, detail=_ambiguous_task_detail(open_tasks))
            if len(open_tasks) == 1 and (not query or _is_continuation_prompt(query)):
                resolved_task = open_tasks[0]
                source_ids.append(resolved_task["id"])
                if (
                    resolved_task.get("_working_task_id")
                    and resolved_task["_working_task_id"] != resolved_task["id"]
                    and (
                        resolved_task.get("execution_mode") == "discussion_only"
                        or resolved_task.get("temp_constraints")
                    )
                ):
                    source_ids.append(resolved_task["_working_task_id"])


        # A selected task may identify its captured predecessor through existing
        # provenance. Recency alone cannot connect a parallel conversation.
        if resolved_task and not resolved_conv:
            wt_conv_id = resolved_task.get("source_conversation_id")
            if wt_conv_id:
                wt_conv_row = conn.execute(
                    "SELECT * FROM conversations WHERE id = ? AND lower(agent) != ?",
                    (wt_conv_id, clean_target),
                ).fetchone()
                if wt_conv_row and wt_conv_row["project_id"] == resolved_project_id:
                    resolved_conv = row_to_dict(wt_conv_row)
                    source_ids.append(resolved_conv["id"])
            if not resolved_conv:
                linked = conn.execute(
                    """SELECT DISTINCT c.id,c.project_id FROM conversations c JOIN content_sources s
                       ON s.source_id=c.id AND s.source_kind='conversation'
                       WHERE s.content_id=? AND s.deleted=0 AND lower(c.agent) != ?
                       ORDER BY c.id LIMIT 2""",
                    (resolved_task["id"], clean_target),
                ).fetchall()
                if len(linked) == 1:
                    if linked[0]["project_id"] == resolved_project_id:
                        resolved_conv = row_to_dict(conn.execute(
                            "SELECT * FROM conversations WHERE id=?", (linked[0]["id"],),
                        ).fetchone())
                        source_ids.append(resolved_conv["id"])

        clean_source = (source_agent or "").strip().lower()
        # A unique explicitly named predecessor is useful captured evidence even
        # without extracted task provenance. Never choose it by recency, cross
        # an existing task-source association, or use another task's source.
        has_task_sources = bool(resolved_task and (
            resolved_task.get("source_conversation_id")
            or conn.execute(
                """SELECT 1 FROM content_sources WHERE content_id=?
                   AND source_kind='conversation' LIMIT 1""", (resolved_task["id"],),
            ).fetchone()
        ))
        if (not resolved_conv and resolved_project_id and clean_source and clean_source != clean_target
                and not has_task_sources and (resolved_task or not open_tasks)):
            selected_task_id = (resolved_task or {}).get("id")
            candidates = conn.execute(
                """SELECT c.id FROM conversations c
                   WHERE c.project_id=? AND lower(c.agent)=?
                   AND (? IS NULL OR NOT EXISTS (
                       SELECT 1 FROM content_sources s JOIN contents t ON t.id=s.content_id
                       WHERE s.source_id=c.id AND s.source_kind='conversation' AND s.deleted=0
                       AND t.category='task' AND t.id != ?
                   )) ORDER BY c.id LIMIT 2""",
                (resolved_project_id, clean_source, selected_task_id, selected_task_id),
            ).fetchall()
            if len(candidates) == 1:
                resolved_conv = row_to_dict(conn.execute(
                    "SELECT * FROM conversations WHERE id=?", (candidates[0]["id"],),
                ).fetchone())
                source_ids.append(resolved_conv["id"])
                source_candidate_only = resolved_task is not None
                source_selection_note = (
                    f"[{resolved_conv['id']}] " + system("唯一显式来源 Agent 会话候选", "Unique conversation candidate from the explicit source Agent")
                    + (system("；与选定任务的关联未记录，现场仅供核对，不作为新约束或执行计划", "; its link to the selected task is unrecorded; use this context for review, not as a new constraint or execution plan") if source_candidate_only
                       else system("；项目内无待选任务，按捕获原话回读，仍须用户确认", "; no project task requires selection; read back the captured quotation and await user confirmation"))
                )
            elif candidates:
                source_selection_note = system("多个显式来源 Agent 会话候选；工作现场未知，请指定 conversation_id，不按时间代选", "Multiple conversations match the explicit source Agent; specify conversation_id rather than choosing by recency")
        # 5. Resolve source_agent
        final_source_agent = None

        if clean_source and clean_source != clean_target:
            # Explicit outgoing source agent provided
            final_source_agent = clean_source
        else:
            # Deriving from predecessor (conversation -> task -> recent cross-agent conversation)
            if resolved_conv and resolved_conv.get("agent"):
                cand = resolved_conv["agent"].strip().lower()
                if cand != clean_target:
                    final_source_agent = cand
            if not final_source_agent and resolved_task and resolved_task.get("agent"):
                cand = resolved_task["agent"].strip().lower()
                if cand != clean_target:
                    final_source_agent = cand
        if not final_source_agent:
            final_source_agent = clean_source or (actor.strip().lower() if actor and actor.strip().lower() != clean_target else "unknown")

        if final_source_agent == clean_target:
            raise HTTPException(
                status_code=422,
                detail=f"Source agent and target agent cannot be the same ('{clean_target}')",
            )

        # 6. Build Card: Goal
        if resolved_task:
            t_title = resolved_task.get("title", "").strip()
            t_content = resolved_task.get("content", "").strip()
            goal_str = f"{t_title}: {t_content}" if t_content else t_title
        elif resolved_conv:
            c_msgs = resolved_conv.get("messages", [])
            user_msgs = [m for m in c_msgs if isinstance(m, dict) and m.get("role") == "user" and m.get("content")]
            if user_msgs:
                goal_str = (resolved_conv.get("title") or system("对话任务", "Conversation task")) + ": " + _cap_card_excerpt(str(user_msgs[0]["content"]).strip(), max_chars=800, max_bytes=800)
            else:
                goal_str = resolved_conv.get("title") or system("未记录", "Not recorded")
        elif query and _relay_has_task_instruction(query):
            goal_str = _cap_card_excerpt(query.strip(), max_chars=800, max_bytes=800)
        else:
            goal_str = system("未记录", "Not recorded")

        is_discussion_only = bool(resolved_task and resolved_task.get("execution_mode") == "discussion_only")
        if (
            resolved_task
            and resolved_task.get("_working_task_id")
            and resolved_task["_working_task_id"] != resolved_task["id"]
            and (
                is_discussion_only
                or resolved_task.get("temp_constraints")
                or resolved_task.get("blockers")
                or resolved_task.get("next_steps")
            )
            and resolved_task["_working_task_id"] not in source_ids
        ):
            source_ids.append(resolved_task["_working_task_id"])

        # 7. Build Card: Progress
        # Only the selected task is a source. Parallel open work is disclosed as
        # unselected; completed work is a do-not-redo list, not a resume target.
        progress_parts = []
        if is_discussion_only:
            progress_parts.append(
                system(
                    "【仅讨论方案/尚未授权改代码(discussion_only)】",
                    "[DISCUSSION ONLY / Code edits NOT authorized (discussion_only)]",
                )
            )
        if resolved_task:
            body = resolved_task.get("content")
            desc = f": {body}" if isinstance(body, str) and body else ""
            progress_parts.append(
                system("选定任务 ", "Selected task ") + f"[{task_progress_status(resolved_task)}] {resolved_task.get('title', '')}{desc}"
            )
        sibling_open = [
            item for item in open_tasks if not resolved_task or item["id"] != resolved_task["id"]
        ]
        if sibling_open:
            progress_parts.append(
                system("并行任务未选定: ", "Unselected parallel tasks: ") + CardText("; ").join(item.get("title") or item["id"] for item in sibling_open)
            )
        if completed_tasks:
            progress_parts.append(
                system("已完成勿重做: ", "Completed; do not repeat: ") + CardText("; ").join(item.get("title") or item["id"] for item in completed_tasks)
            )

        if progress_parts:
            progress_str = CardText("; ").join(progress_parts)
        elif resolved_conv:
            c_msgs = resolved_conv.get("messages", [])
            asst_msgs = [m for m in c_msgs if isinstance(m, dict) and m.get("role") == "assistant" and m.get("content")]
            summary = resolved_conv.get("process_summary")
            parts = []
            extracted = assemble_tool_pairs(resolved_conv.get("messages"), summary)
            rendered_pairs = _render_tool_pairs(extracted, max_piece_chars=800, max_piece_bytes=800)
            if rendered_pairs:
                parts.append(rendered_pairs)
            if summary and isinstance(summary, str) and summary.strip():
                parts.append(system("执行过程: ", "Execution record: ") + _cap_card_excerpt(summary.strip(), max_chars=800, max_bytes=800))
            if asst_msgs and isinstance(asst_msgs[-1].get("content"), str):
                last_reply = _cap_card_excerpt(asst_msgs[-1]["content"].strip(), max_chars=800, max_bytes=800)
                parts.append(system("最新回复(未确认完成): ", "Latest reply (completion unverified): ") + last_reply)
            progress_str = CardText(" | ").join(parts) if parts else system("未记录", "Not recorded")
        else:
            progress_str = system("未记录", "Not recorded")

        # Six-slot vocabulary stays stable: decisions contains current rules
        # and preferences; evidence contains the selected working scene.
        decisions_list = []
        if is_discussion_only:
            decisions_list.append(
                system(
                    "【执行门禁：尚未授权改代码(discussion_only)】仅讨论方案，禁改代码",
                    "[Gate: discussion_only / Code edits NOT authorized] Discuss plan only",
                )
            )
        if resolved_task and resolved_task.get("temp_constraints"):
            for tc in resolved_task.get("temp_constraints") or []:
                tc_str = str(tc).strip()
                if tc_str:
                    decisions_list.append(
                        system("当前任务临时约束（仅限本任务）: ", "Current task temporary constraint (task-scoped): ")
                        + _cap_card_excerpt(tc_str, max_chars=400, max_bytes=400)
                    )
        for item in _applicable_memories(
            conn, resolved_project_id, resolved_task, None if source_candidate_only else resolved_conv, {"decision", "preference"},
        ):
            decisions_list.append(_memory_text(item))
            source_ids.append(item["id"])

        user_messages = [
            m for m in (resolved_conv or {}).get("messages", [])
            if isinstance(m, dict) and m.get("role") == "user" and m.get("content")
        ]
        if user_messages and not source_candidate_only:
            # Quote, never interpret an assistant's proposed rule as permission.
            # Earlier turns are not automatically current constraints.
            decisions_list.append(
                f"[{resolved_conv['id']}] " + system("最新用户指令原话（回读待确认，不扩展授权）: ", "Latest user instruction, quoted (read back for confirmation; no additional authority): ")
                + _cap_card_excerpt(str(user_messages[-1]["content"]), max_chars=800, max_bytes=800)
            )
        decisions_str = CardText(" | ").join(decisions_list) if decisions_list else system("当前明确约束与偏好未知（未记录）", "Current explicit constraints and preferences are unknown (not recorded)")

        evidence_snippets = []
        if source_selection_note:
            evidence_snippets.append(source_selection_note)
        if resolved_task:
            evidence_snippets.append(system("选定任务现场 ", "Selected task context ") + _memory_text(resolved_task))
        if resolved_project_id:
            scene = _project_scene(conn, resolved_project_id)
            source_ids.append(resolved_project_id)
            evidence_snippets.append(
                f"[{resolved_project_id}] " + system("项目目录（登记值，未验证磁盘）: ", "Project directories (registered, not verified on disk): ")
                + (CardText("; ").join(scene["directories"]) if scene["directories"] else system("未知（未登记）", "Unknown (not registered)"))
            )
        if resolved_conv:
            source_ids.append(resolved_conv["id"])
            evidence_snippets.append(
                f"[{resolved_conv['id']}] [{resolved_conv.get('agent', 'source')}] "
                + (resolved_conv.get('title') or system('会话', 'Conversation'))
                + system('；工作目录：', '; working directory: ')
                + (resolved_conv.get('cwd') or system('未知', 'Unknown'))
                + system('；来源路径：', '; source path: ')
                + (resolved_conv.get('source_path') or system('未知', 'Unknown'))
            )
            # Bound individual turn excerpts (<=4000 chars and bounded total UTF-8 bytes)
            # while retaining full turn reference metadata so card + readback stay within 32KB MCP assembly budget.
            remaining_turn_bytes = 2200
            source_turns = _current_source_messages_with_meta(resolved_conv)
            for idx_in_slice, (turn_no, message) in enumerate(source_turns):
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, str) and content:
                    turns_left = max(1, len(source_turns) - idx_in_slice)
                    per_turn_bytes = max(320, remaining_turn_bytes // turns_left) if len(source_turns) > 1 else max(320, remaining_turn_bytes)
                    excerpt = _cap_card_excerpt(content, max_chars=4000, max_bytes=min(4000, per_turn_bytes))
                    remaining_turn_bytes = max(320, remaining_turn_bytes - len(excerpt.encode("utf-8")))
                    meta_bits = [f"turn={turn_no}"]
                    if message.get("id"):
                        meta_bits.append(f"id={message['id']}")
                    if message.get("timestamp"):
                        meta_bits.append(f"at={message['timestamp']}")
                    turn_meta = f"[{' '.join(meta_bits)}] "
                    evidence_snippets.append(
                        turn_meta + system("来源 ", "Source ") + str(message.get('role', 'unknown')) + system(" 原话（非当前决定或已验证结果）: ", " quotation (not a current decision or verified result): ")
                        + excerpt
                    )
            extracted = assemble_tool_pairs(resolved_conv.get("messages"), resolved_conv.get("process_summary"))
            rendered_pairs = _render_tool_pairs(extracted, max_piece_chars=800, max_piece_bytes=800)
            if rendered_pairs:
                evidence_snippets.append(rendered_pairs)
            summary = resolved_conv.get("process_summary")
            if summary:
                raw_sum = summary if isinstance(summary, str) else encode(summary)
                evidence_snippets.append(system("来源过程记录（未独立验证）: ", "Source process record (not independently verified): ") + _cap_card_excerpt(raw_sum, max_chars=800, max_bytes=800))
        else:
            evidence_snippets.append(system("选定任务的来源会话、工作目录及验证现场未知；未采用最新并行会话", "The selected task has no identified source conversation, working directory or verified state; an unrelated recent conversation was not substituted"))
        for item in _applicable_memories(
            conn, resolved_project_id, resolved_task, None if source_candidate_only else resolved_conv, {"background", "lesson", "knowledge"},
        ):
            evidence_snippets.append(system("记忆 ", "Memory ") + _memory_text(item))
            source_ids.append(item["id"])
        code_cache: dict[str, dict[str, Any]] = {}
        if resolved_conv:
            code_notes = [project_service.worktree_code_state(resolved_conv.get("cwd"), code_cache)]
        elif resolved_project_id:
            code_notes = [
                project_service.worktree_code_state(directory, code_cache)
                for directory in (_project_scene(conn, resolved_project_id) or {}).get("directories") or []
            ]
        else:
            code_notes = []
        if code_notes:
            evidence_snippets.append(CardText(" | ").join(_code_state_text(item) for item in code_notes))
        evidence_str = CardText(" // ").join(evidence_snippets)

        # 10. Build Card: Blockers
        blockers_list = []
        if is_discussion_only:
            blockers_list.append(
                system(
                    "尚未授权改代码(discussion_only)，须待用户明确授权后执行",
                    "Code edits not authorized (discussion_only); await explicit user approval",
                )
            )
        if resolved_task and resolved_task.get("blockers"):
            for blk in resolved_task.get("blockers") or []:
                blk_str = str(blk).strip()
                if blk_str:
                    blockers_list.append(
                        system("当前任务阻塞: ", "Current task blocker: ") + _cap_card_excerpt(blk_str, max_chars=400, max_bytes=400)
                    )
        if source_selection_note:
            blockers_list.append(source_selection_note)
        if len(user_messages) > 1:
            blockers_list.append(system("更早用户原话中的约束是否仍有效未知；以当前有效记忆及本次用户确认核对，不自动恢复历史规则", "Earlier user constraints may no longer apply; check current memories and obtain confirmation, without restoring historical rules automatically"))
        if resolved_project_id:
            conflicts = conn.execute(
                "SELECT id, reason FROM conflicts WHERE project_id = ? AND status = 'pending' ORDER BY id",
                (resolved_project_id,),
            ).fetchall()
            for cf in conflicts:
                cf_dict = row_to_dict(cf)
                blockers_list.append(cf_dict["reason"])
                source_ids.append(cf_dict["id"])
        blockers_list.extend(_raw_source_quotes(
            None if source_candidate_only else resolved_conv,
            r"阻塞|阻碍|未确定|尚未确定|待确认|未验证|尚未验证|不确定|\bblock(?:ed|er)?\b|\bunverified\b|\buncertain\b",
        ))
        blockers_str = CardText("; ").join(blockers_list) if blockers_list else system("未记录", "Not recorded")

        # 11. Build Card: Next Steps
        raw_next_steps = _raw_source_quotes(
            None if source_candidate_only else resolved_conv, r"下一步|接下来|\bnext\s+(?:steps?|actions?)\b",
        )
        task_next_steps = [
            str(ns).strip() for ns in ((resolved_task or {}).get("next_steps") or []) if str(ns).strip()
        ]
        if is_discussion_only:
            next_steps_str = (
                system("【尚未授权改代码(discussion_only)】", "[DISCUSSION ONLY] ")
                + clean_target
                + system(
                    " 仅继续方案讨论，禁止直接改代码",
                    " continues plan discussion only without editing code",
                )
            )
            if task_next_steps:
                next_steps_str = next_steps_str + system("：", ": ") + "; ".join(task_next_steps[:2])
        elif query and _relay_has_task_instruction(query):
            next_steps_str = (system("目标助手 ", "Target assistant ") + clean_target
                              + system(" 执行：", " executes: ") + _cap_card_excerpt(query.strip(), max_chars=1200, max_bytes=1200))
        elif raw_next_steps:
            next_steps_str = CardText(" | ").join(raw_next_steps)
        elif resolved_task:
            next_steps_str = (system("目标助手 ", "Target assistant ") + clean_target
                              + system(" 继续任务「", " continues task “")
                              + str(resolved_task.get("title") or "") + system("」", "”"))
            if task_next_steps:
                next_steps_str = next_steps_str + system("：", ": ") + "; ".join(task_next_steps[:3])
        elif len(sibling_open) > 1:
            next_steps_str = system("有多个未完成任务；继续前必须由用户选定一个。",
                                    "Multiple open tasks; the user must select one before continuation.")
        elif len(sibling_open) == 1:
            next_steps_str = (system("目标助手 ", "Target assistant ") + clean_target
                              + system(" 继续任务「", " continues task “")
                              + str(sibling_open[0].get("title") or "") + system("」", "”"))
        else:
            next_steps_str = system("无法判断", "Unknown")

        fields = dict(goal=goal_str, progress=progress_str, decisions=decisions_str,
                      evidence=evidence_str, blockers=blockers_str, next_steps=next_steps_str)
        card = {key: str(value) for key, value in fields.items()}
        card['_system_text'] = {key: getattr(value, 'spans', []) for key, value in fields.items()}

        source_revisions = {}
        for sid in set(source_ids):
            if sid == resolved_project_id:
                scene = _project_scene(conn, sid)
                source_revisions[sid] = {"type": "project", **scene}
                if not resolved_conv:
                    source_revisions[sid]["code_states"] = [
                        project_service.worktree_code_state(directory, code_cache)
                        for directory in scene["directories"]
                    ]
                continue
            c_row = conn.execute("SELECT version, content_hash, status, project_id, tags, related_ids FROM contents WHERE id = ?", (sid,)).fetchone()
            if c_row:
                source_revisions[sid] = {
                    "type": "content",
                    "version": c_row["version"],
                    "content_hash": c_row["content_hash"],
                    "status": c_row["status"],
                    "project_id": c_row["project_id"],
                    "tags": c_row["tags"],
                    "related_ids": c_row["related_ids"],
                }
                continue
            cv_row = conn.execute("SELECT content_hash, project_id, cwd, source_path, title, process_summary FROM conversations WHERE id = ?", (sid,)).fetchone()
            if cv_row:
                source_revisions[sid] = {
                    "type": "conversation",
                    "content_hash": cv_row["content_hash"],
                    "project_id": cv_row["project_id"],
                    "cwd": cv_row["cwd"],
                    "source_path": cv_row["source_path"],
                    "title": cv_row["title"],
                    "process_summary": cv_row["process_summary"],
                    "code_state": project_service.worktree_code_state(cv_row["cwd"], code_cache),
                }
                continue
            cf_row = conn.execute("SELECT status, resolution, proposed FROM conflicts WHERE id = ?", (sid,)).fetchone()
            if cf_row:
                source_revisions[sid] = {
                    "type": "conflict",
                    "status": cf_row["status"],
                    "resolution": cf_row["resolution"],
                    "proposed": cf_row["proposed"],
                }
                continue
            wt_row = conn.execute(
                "SELECT version, status, execution_mode, project_id, title, current_state, temp_constraints FROM working_tasks WHERE id = ?",
                (sid,),
            ).fetchone()
            if wt_row:
                source_revisions[sid] = {
                    "type": "working_task",
                    "version": wt_row["version"],
                    "status": wt_row["status"],
                    "execution_mode": wt_row["execution_mode"],
                    "project_id": wt_row["project_id"],
                    "title": wt_row["title"],
                    "current_state": wt_row["current_state"],
                    "temp_constraints": wt_row["temp_constraints"],
                }

        conn.execute(
            """
            INSERT INTO handoffs (
                id, project_id, source_agent, target_agent, status, card, source_ids, source_revisions, created_at, confirmed_at
            ) VALUES (?, ?, ?, ?, 'awaiting_confirmation', ?, ?, ?, ?, NULL)
            """,
            (hid, resolved_project_id, final_source_agent, clean_target, encode(card), encode(list(set(source_ids))), encode(source_revisions), now),
        )

        log_audit(actor=actor, action="handoff.create", target=hid, conn=conn)

    return get_handoff(hid, language)


def validate_source_revisions(conn, current_handoff: dict[str, Any]) -> None:
    source_ids = current_handoff.get("source_ids", [])
    if isinstance(source_ids, str):
        source_ids = decode(source_ids, [])
    if not isinstance(source_ids, list):
        raise HTTPException(
            status_code=409,
            detail="Cannot confirm or read handoff with invalid source identifiers; please regenerate handoff card.",
        )
    if not source_ids:
        return

    source_revisions = current_handoff.get("source_revisions")
    if isinstance(source_revisions, str):
        source_revisions = decode(source_revisions, {})
    if not isinstance(source_revisions, dict) or not source_revisions:
        # Legacy cards missing revisions must fail closed require regenerate
        raise HTTPException(
            status_code=409,
            detail="Cannot confirm or read legacy handoff without source revisions; please regenerate handoff card.",
        )

    for sid in source_ids:
        expected = source_revisions.get(sid)
        if not isinstance(expected, dict):
            raise HTTPException(
                status_code=409,
                detail=f"Handoff missing stored revision for source '{sid}'; please regenerate handoff card.",
            )

        stype = expected.get("type")
        if stype == "content":
            row = conn.execute("SELECT version, content_hash, status, project_id, tags, related_ids FROM contents WHERE id = ?", (sid,)).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail=f"Cannot confirm handoff: referenced source item '{sid}' was deleted")
            if (
                row["version"] != expected.get("version")
                or row["content_hash"] != expected.get("content_hash")
                or row["status"] != expected.get("status")
                or row["project_id"] != expected.get("project_id")
                or any(key in expected and row[key] != expected[key] for key in ("tags", "related_ids"))
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot confirm or read handoff: referenced content '{sid}' was modified since card creation; please regenerate handoff card.",
                )
        elif stype == "project":
            if _project_scene(conn, sid) != {key: expected[key] for key in ("name", "directories")}:
                raise HTTPException(status_code=409, detail=STALE_HANDOFF_DETAIL)
            conversation_sources = [
                item for item in source_revisions.values()
                if isinstance(item, dict) and item.get("type") == "conversation"
            ]
            if not conversation_sources:
                directories = list(expected.get("directories") or [])
                expected_states = expected.get("code_states")
                code_cache: dict[str, dict[str, Any]] = {}
                if not isinstance(expected_states, list):
                    if any(
                        project_service.worktree_code_state(directory, code_cache).get("kind") == "git"
                        for directory in directories
                    ):
                        raise HTTPException(status_code=409, detail=CODE_STATE_STALE_DETAIL)
                elif len(expected_states) != len(directories) or any(
                    not isinstance(item, dict) or not project_service.code_states_compatible(
                        item, project_service.worktree_code_state(directory, code_cache),
                    )
                    for item, directory in zip(expected_states, directories)
                ):
                    raise HTTPException(status_code=409, detail=CODE_STATE_STALE_DETAIL)
        elif stype == "conversation":
            row = conn.execute("SELECT content_hash, project_id, cwd, source_path, title, process_summary FROM conversations WHERE id = ?", (sid,)).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail=f"Cannot confirm handoff: referenced source item '{sid}' was deleted")
            if any(key not in expected for key in ("title", "process_summary")):
                raise HTTPException(
                    status_code=409,
                    detail="Cannot confirm or read handoff without a complete conversation revision; please regenerate handoff card.",
                )
            if (
                row["content_hash"] != expected.get("content_hash")
                or row["project_id"] != expected.get("project_id")
                or any(key in expected and row[key] != expected[key] for key in ("cwd", "source_path", "title", "process_summary"))
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot confirm or read handoff: referenced conversation '{sid}' was modified or appended since card creation; please regenerate handoff card.",
                )
            expected_code = expected.get("code_state")
            current_code = project_service.worktree_code_state(row["cwd"])
            if not isinstance(expected_code, dict):
                if current_code.get("kind") == "git":
                    raise HTTPException(status_code=409, detail=CODE_STATE_STALE_DETAIL)
            elif not project_service.code_states_compatible(expected_code, current_code):
                raise HTTPException(status_code=409, detail=CODE_STATE_STALE_DETAIL)
        elif stype == "conflict":
            row = conn.execute("SELECT status, resolution, proposed FROM conflicts WHERE id = ?", (sid,)).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail=f"Cannot confirm handoff: referenced source item '{sid}' was deleted")
            if (
                row["status"] != expected.get("status")
                or row["resolution"] != expected.get("resolution")
                or row["proposed"] != expected.get("proposed")
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot confirm or read handoff: referenced conflict '{sid}' was modified since card creation; please regenerate handoff card.",
                )
        elif stype == "working_task":
            row = conn.execute(
                "SELECT version, status, execution_mode, project_id, title, current_state, temp_constraints FROM working_tasks WHERE id = ?",
                (sid,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail=f"Cannot confirm handoff: referenced source item '{sid}' was deleted")
            if (
                row["version"] != expected.get("version")
                or row["status"] != expected.get("status")
                or row["execution_mode"] != expected.get("execution_mode")
                or row["project_id"] != expected.get("project_id")
                or any(key in expected and row[key] != expected[key] for key in ("title", "current_state", "temp_constraints"))
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"Cannot confirm or read handoff: referenced working task '{sid}' was modified since card creation; please regenerate handoff card.",
                )
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Cannot confirm or read handoff: unknown source type '{stype}' for source '{sid}'; please regenerate handoff card.",
            )


def confirm_handoff(handoff_id: str, confirmed: bool = False, actor: str = "user") -> dict[str, Any]:
    if confirmed is not True:
        raise HTTPException(status_code=422, detail="Handoff confirmation requires confirmed=True")

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Handoff not found")

        current = row_to_dict(row)
        if str(actor).strip().lower() != str(current['target_agent']).strip().lower():
            raise HTTPException(status_code=403, detail='Only the target Agent may confirm after explicit user approval')
        actor = current['target_agent']
        if current["status"] == "cancelled":
            raise HTTPException(status_code=400, detail="Cannot confirm a cancelled handoff")

        # Strict validation: checks deleted (400) and stale revisions (409)
        validate_source_revisions(conn, current)

        now = now_iso()
        audit = log_audit(actor=actor, action="handoff.confirm", target=handoff_id, conn=conn)
        card = current['card']
        card['_confirmation'] = {'protocol': 2, 'actor': current['target_agent'], 'at': now, 'audit_id': audit['id']}
        conn.execute(
            "UPDATE handoffs SET status = 'confirmed', confirmed_at = ?, card = ? WHERE id = ?",
            (now, encode(card), handoff_id),
        )

    return get_handoff(handoff_id)


def cancel_handoff(handoff_id: str) -> dict[str, Any]:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Handoff not found")
        if row["status"] != "awaiting_confirmation" and not (row['status'] == 'confirmed' and not has_explicit_confirmation(row_to_dict(row), conn)):
            raise HTTPException(status_code=409, detail="Only a handoff waiting for confirmation can be cancelled")

        conn.execute("UPDATE handoffs SET status = 'cancelled' WHERE id = ?", (handoff_id,))
        log_audit(actor="user", action="handoff.cancel", target=handoff_id, conn=conn)

    # Outside with db() block
    return get_handoff(handoff_id)


def continuation_contract(handoff: dict[str, Any], consumer_id: str | None = None) -> dict[str, Any]:
    """Machine-readable resume cursor after a six-field card exists.

    Confirmation still only changes status; this contract is what a successor
    must consume so it continues the selected task, skips parallel work,
    ignores historical decisions, and does not redo completed tasks.
    """
    target = str(handoff.get("target_agent") or "").strip().lower()
    consumer = str(consumer_id or "").strip().lower()
    card = handoff.get("card") if isinstance(handoff.get("card"), dict) else {}
    payload = {
        "handoff_id": handoff.get("id"),
        "project_id": handoff.get("project_id"),
        "target_agent": target,
        "card": {field: str(card.get(field) or "") for field in HANDOFF_CARD_FIELDS},
        "readback": d005_readback(card),
        "source_ids": handoff.get("source_ids") or [],
        "source_revisions": handoff.get("source_revisions") or {},
        "must_not_execute": True,
        "execution_mode": "approved",
        "selected_task_id": None,
        "resume_from": None,
        "parallel_open_tasks": [],
        "do_not_redo": [],
        "active_decisions": [],
        "excluded_historical_decisions": [],
    }
    if consumer and consumer != target:
        payload["status"] = "not_addressed"
        payload["message"] = f"This handoff is addressed to '{target}', not '{consumer}'."
        return payload

    source_ids = payload["source_ids"]
    if isinstance(source_ids, str):
        source_ids = decode(source_ids, [])
        payload["source_ids"] = source_ids

    is_discussion_only = False
    with db() as conn:
        proof_valid = has_explicit_confirmation(handoff, conn)
        try:
            validate_source_revisions(conn, handoff)
        except HTTPException:
            payload["status"] = "stale"
            payload["message"] = STALE_HANDOFF_MESSAGE
            payload["card"] = {field: STALE_HANDOFF_MESSAGE for field in HANDOFF_CARD_FIELDS}
            payload["readback"] = d005_readback(payload["card"])
            return payload

        open_tasks, completed_tasks = list_project_tasks(conn, handoff["project_id"]) if handoff.get("project_id") else ([], [])
        selected = [
            item for item in open_tasks
            if item["id"] in source_ids or (item.get("_working_task_id") and item["_working_task_id"] in source_ids)
        ]
        selected_task_obj = selected[0] if len(selected) == 1 else None
        payload["selected_task_id"] = selected_task_obj["id"] if selected_task_obj else None
        is_discussion_only = bool(selected_task_obj and selected_task_obj.get("execution_mode") == "discussion_only")
        payload["execution_mode"] = "discussion_only" if is_discussion_only else "approved"
        payload["parallel_open_tasks"] = [
            _task_candidate(item)
            for item in open_tasks
            if item["id"] != payload["selected_task_id"]
        ]
        payload["do_not_redo"] = [_task_candidate(item) for item in completed_tasks]
        payload["active_decisions"] = [
            {"id": row["id"], "title": row["title"], "content": row["content"], "version": row["version"]}
            for row in conn.execute(
                """SELECT id,title,content,version FROM contents WHERE status='active'
                   AND category IN ('decision','preference')
                   AND id IN (SELECT value FROM json_each(?)) ORDER BY id""", (encode(source_ids),),
            )
        ]
        payload["excluded_historical_decisions"] = [
            {"id": row["id"], "title": row["title"], "content": row["content"]}
            for row in conn.execute(
                """SELECT id,title,content FROM contents WHERE project_id=?
                   AND category IN ('decision','preference') AND status='historical' ORDER BY id""",
                (handoff.get("project_id"),),
            )
        ]

    status = handoff.get("status")
    if status == "cancelled":
        payload["status"] = "cancelled"
        payload["must_not_execute"] = True
        payload["message"] = "Handoff was cancelled; cannot execute."
        return payload
    if status != "confirmed":
        payload["status"] = "awaiting_confirmation"
        payload["message"] = "Handoff is not confirmed; obtain explicit user confirmation before executing."
        return payload

    if not proof_valid:
        payload['status'] = 'confirmation_review_required'
        payload['message'] = 'Historical confirmation provenance is unknown. Read back the card and obtain explicit user confirmation again.'
        return payload

    if is_discussion_only:
        payload["status"] = "discussion_only"
        payload["execution_mode"] = "discussion_only"
        payload["must_not_execute"] = True
        payload["resume_from"] = None
        payload["message"] = (
            "Task is in discussion-only mode (尚未授权改代码 / discussion_only); "
            "continue design discussion only and do NOT edit files until user explicitly authorizes execution."
        )
        return payload

    payload["status"] = "ready"
    payload["must_not_execute"] = False
    payload["resume_from"] = str(card.get("next_steps") or "")
    payload["message"] = "Handoff confirmed. Continue from resume_from; do not redo completed work or switch to parallel tasks."
    return payload


def lookup_continuation(
    consumer_id: str | None,
    project_id: str | None = None,
    query: str | None = None,
    handoff_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the candidate or confirmed continuation contract for this consumer.

    Multiple plausible cards fail closed as explicit candidates, never silently
    picking the latest card. Hooks stay silent. Successors receive this only when
    they call MCP.
    """
    consumer = str(consumer_id or "").strip().lower()
    if consumer not in ALLOWED_TARGET_AGENTS:
        return None

    with db() as conn:
        # 1. Explicit handoff_id (argument or regex in query)
        target_hid = handoff_id
        if not target_hid and query:
            match = re.search(r"\b(hnd_[a-zA-Z0-9_]+)\b", query)
            if match:
                target_hid = match.group(1)

        if target_hid:
            row = conn.execute(
                "SELECT * FROM handoffs WHERE id = ? AND lower(target_agent) = ? AND status IN ('awaiting_confirmation', 'confirmed')",
                (target_hid, consumer),
            ).fetchone()
            if row:
                if project_id and row["project_id"] != project_id:
                    return None
                return continuation_contract(row_to_dict(row), consumer)
            return None

        # 2. Query matching cards
        sql = """
            SELECT * FROM handoffs
            WHERE lower(target_agent) = ? AND status IN ('awaiting_confirmation', 'confirmed')
        """
        params: list[Any] = [consumer]
        if project_id:
            sql += " AND project_id = ?"
            params.append(project_id)
        sql += " ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return None

        if len(rows) == 1:
            return continuation_contract(row_to_dict(rows[0]), consumer)

        # 3. Multiple cards: check if query uniquely identifies one card
        if query and not _is_continuation_prompt(query):
            q_lower = query.lower()
            matching_cards = []
            for r in rows:
                h = row_to_dict(r)
                card = h.get("card") if isinstance(h.get("card"), dict) else {}
                goal = str(card.get("goal") or "").lower()
                source_ids = h.get("source_ids") or []
                if isinstance(source_ids, str):
                    source_ids = decode(source_ids, [])
                if (q_lower in goal) or any(isinstance(sid, str) and sid.lower() in q_lower for sid in source_ids):
                    matching_cards.append(h)
            if len(matching_cards) == 1:
                return continuation_contract(matching_cards[0], consumer)

        # 4. Ambiguous continuation: fail closed, return candidates
        candidates = []
        for r in rows:
            h = row_to_dict(r)
            card = h.get("card") if isinstance(h.get("card"), dict) else {}
            candidates.append({
                "handoff_id": h["id"],
                "project_id": h.get("project_id"),
                "status": h["status"],
                "target_agent": h["target_agent"],
                "source_agent": h.get("source_agent"),
                "created_at": h["created_at"],
                "goal": str(card.get("goal") or ""),
                "next_steps": str(card.get("next_steps") or ""),
            })

        return {
            "status": "ambiguous_continuation",
            "code": "ambiguous_continuation",
            "handoff_id": None,
            "project_id": project_id,
            "target_agent": consumer,
            "must_not_execute": True,
            "candidates": candidates,
            "continuation_candidates": candidates,
            "message": "Multiple handoff cards found; user must select a specific card before continuation.",
            "selected_task_id": None,
            "resume_from": None,
            "parallel_open_tasks": [],
            "do_not_redo": [],
            "active_decisions": [],
            "excluded_historical_decisions": [],
        }


def is_continuation_query(query: str | None) -> bool:
    """Public helper checking whether a query/prompt is a relay/continuation trigger."""
    if not query:
        return False
    q = str(query).strip()
    if _is_continuation_prompt(q):
        return True
    q_low = q.lower()
    return any(
        kw in q_low
        for kw in (
            "接力",
            "项目接力",
            "接着做",
            "继续做",
            "继续上次",
            "继续之前",
            "接棒",
            "relay",
            "handoff",
            "continue project",
            "resume project",
        )
    )


def sync_recent_agent_chats_for_relay(*, respect_budget: bool = False) -> list[dict[str, Any]]:
    """Refresh a bounded recent-source batch without host execution or waiting for locks."""
    from app.services import connection_service

    try:
        return connection_service.sync_recent_connections_for_relay()
    except Exception:
        return [{"agent_id": None, "status": "error", "error": "History discovery failed"}]


def _link_unlinked_conversations_to_projects() -> int:
    """Link conversations that have a cwd. Returns how many attempts failed."""
    failed = 0
    try:
        with db() as conn:
            unlinked = conn.execute(
                """
                SELECT id, cwd, title FROM conversations
                WHERE (project_id IS NULL OR project_id = '') AND cwd IS NOT NULL AND trim(cwd) != ''
                ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 50
                """
            ).fetchall()
        for row in unlinked:
            cwd_val = str(row["cwd"] or "").strip()
            if not cwd_val:
                continue
            try:
                matched, ambiguous = project_service.resolve_project_by_path(cwd_val)
                pid = matched["id"] if (matched and not ambiguous) else None
                if not pid and not ambiguous:
                    pid = project_service.reconstruct_or_resolve_project(cwd_val)
                if pid:
                    with db() as conn:
                        conn.execute(
                            "UPDATE conversations SET project_id = ? WHERE id = ? AND (project_id IS NULL OR project_id = '')",
                            (pid, row["id"]),
                        )
            except Exception:
                failed += 1
                continue
    except Exception:
        failed += 1
    return failed


def _clean_line(text: str, max_len: int = 280) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    if len(cleaned) > max_len:
        return cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def _summarize_conversation_for_relay(
    conv: dict[str, Any], language: str = 'zh', *, include_tools: bool = True,
) -> dict[str, Any]:
    """Produce a concise, structured summary of a conversation for project relay selection & digest."""
    from app.adapters.codex import _extract_codex_title_candidate

    messages = conv.get("messages") or []
    if isinstance(messages, str):
        messages = decode(messages, [])
    ag = str(conv.get("agent") or "").strip().lower()
    user_msgs: list[str] = []
    assistant_msgs: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").lower()
        raw_content = str(msg.get("content") or "") if isinstance(msg.get("content"), str) else ""
        raw_content = re.sub(
            r"<(?:system-reminder|system-notice|irc)\b[^>]*>.*?</(?:system-reminder|system-notice|irc)>",
            " ",
            raw_content,
            flags=re.S,
        )
        content = " ".join(raw_content.split()).strip()
        if not content:
            continue
        if role == "user":
            if _is_continuation_prompt(content):
                continue
            if ag == "codex" and (
                content.startswith("<")
                or "## My request" in raw_content
                or "# AGENTS.md instructions" in raw_content
                or "# Files mentioned by the user:" in raw_content
            ):
                extracted_u = _extract_codex_title_candidate(raw_content)
                if not extracted_u or _is_continuation_prompt(extracted_u):
                    continue
                content = extracted_u
            elif "[Assistant Rules]" in raw_content and "[User Request]" in raw_content:
                after_req = raw_content.split("[User Request]", 1)[1].strip()
                if after_req:
                    content = " ".join(after_req.split()).strip()
            user_msgs.append(content)
        elif role == "assistant":
            assistant_msgs.append(content)

    local = lambda zh, en: en if language == 'en' else zh

    def _is_generic_opener_msg(text: str) -> bool:
        cleaned = re.sub(r"[。！!？?，,\s]+$", "", (text or "").strip())
        if not cleaned or cleaned.isdigit() or len(cleaned) <= 4:
            return True
        if re.fullmatch(r"@?\[(?:Quote|Paste|Image)\]", cleaned):
            return True
        if cleaned in {
            "了解一下项目",
            "全面了解一下项目",
            "全面了解项目",
            "了解一下当前项目",
            "了解项目",
            "了解一下这个分支",
            "了解一下项目刚刚的改动",
            "帮我了解一下项目",
            "先了解一下项目",
            "继续",
            "请继续",
        }:
            return True
        return bool(re.fullmatch(r"(?:请|先|帮我)?(?:全面)?了解(?:一下)?(?:当前|这个)?(?:项目|仓库|分支)(?:刚刚的改动)?", cleaned))

    def _clean_prose(text: str, limit_chars: int) -> str:
        s = re.sub(r"```.*?```", " ", text or "", flags=re.S)
        s = re.sub(r"\|[-:\s|]+\|", " ", s)
        s = re.sub(r"[#*`>|]+", " ", s)
        s = " ".join(s.split()).strip(" -—:：;；")
        return _clean_line(s, limit_chars)

    primary_user_msg = user_msgs[0] if user_msgs else ""
    if len(user_msgs) > 1 and _is_generic_opener_msg(primary_user_msg):
        for candidate_msg in user_msgs[1:]:
            if not _is_generic_opener_msg(candidate_msg) and len(candidate_msg.strip()) > 4:
                primary_user_msg = candidate_msg
                break

    raw_title = _clean_line(str(conv.get("title") or ""), 120)
    is_placeholder_title = (
        not raw_title
        or raw_title.lower() in {"untitled session", "untitled", "new chat", "未命名会话"}
        or raw_title.lower().startswith("new session - ")
        or (_is_generic_opener_msg(raw_title) and bool(primary_user_msg) and not _is_generic_opener_msg(primary_user_msg))
        or raw_title.startswith("<")
        or raw_title.startswith("# Files mentioned by the user:")
        or raw_title.startswith("# AGENTS.md instructions")
        or _is_continuation_prompt(raw_title)
        # Host-generated identifiers and tool artifacts are not human titles.
        or bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw_title))
        or bool(re.match(r"^(?:Claude Code|OpenCode|Codex|Cursor)\s+[0-9a-zA-Z_-]{8,}$", raw_title))
        or bool(re.fullmatch(r"@?\[(?:Quote|Paste|Image)\]", raw_title))
        or raw_title.endswith("：") or raw_title.endswith(":")
    )
    if is_placeholder_title:
        raw_title = (
            _clean_prose(primary_user_msg, 96)
            if primary_user_msg
            else (
                _clean_prose(assistant_msgs[0], 96)
                if assistant_msgs
                else (local("Codex 子任务会话", "Codex subtask session") if ag == "codex" else local("未命名会话", "Untitled conversation"))
            )
        )
        if (
            assistant_msgs
            and (
                bool(re.fullmatch(r"@?\[(?:Quote|Paste|Image)\]", raw_title))
                or bool(re.match(r"^(?:Claude Code|OpenCode|Codex|Cursor)\s+[0-9a-zA-Z_-]{8,}$", raw_title))
                or len(raw_title.strip()) <= 3
            )
        ):
            raw_title = _clean_prose(assistant_msgs[-1], 96) or raw_title
    title = raw_title or local("未命名会话", "Untitled conversation")
    process_summary = " ".join(str(conv.get("process_summary") or "").split()).strip()
    user_goal = primary_user_msg if primary_user_msg else title
    latest_user = user_msgs[-1] if user_msgs else user_goal
    latest_assistant = assistant_msgs[-1] if assistant_msgs else ""
    short_goal = _clean_prose(user_goal, 110) or _clean_line(user_goal, 110)
    if user_msgs or assistant_msgs:
        parts = [local("聊了什么：", "Topic: ") + short_goal]
        if (
            len(user_msgs) > 1
            and latest_user != user_goal
            and not _is_generic_opener_msg(latest_user)
            and len(latest_user.strip()) > 3
        ):
            parts.append(local("最新提问：", "Latest ask: ") + _clean_prose(latest_user, 90))
        if latest_assistant:
            parts.append(local("停在哪：", "Latest progress: ") + _clean_prose(latest_assistant, 150))
        brief = local(" ｜ ", " | ").join(parts)
    elif process_summary:
        brief = _clean_prose(process_summary, 240)
    else:
        brief = local("暂无正文记录", "No message body recorded")

    extracted = assemble_tool_pairs(messages, conv.get("process_summary")) if include_tools else {"pairs": [], "omitted": []}
    return {
        "id": conv.get("id"),
        "agent": conv.get("agent") or "unknown",
        "title": title,
        "cwd": conv.get("cwd") or "",
        "updated_at": conv.get("last_message_at") or conv.get("updated_at") or conv.get("created_at") or "",
        "message_count": len(messages),
        "user_goal": user_goal,
        "latest_user_message": latest_user,
        "latest_assistant_reply": latest_assistant,
        "process_summary": process_summary,
        "tool_pairs": extracted["pairs"],
        "tool_pair_omissions": extracted["omitted"],
        "brief_summary": brief,
    }


def discover_relay_projects(
    *,
    consumer: str | None = None,
    limit: int = 10,
    sync_first: bool = True,
    language: str = 'zh',
    link_unlinked: bool = True,
) -> dict[str, Any]:
    """Sync recent chats across all supported agents and return summarized project relay candidates."""
    check_language(language)
    local = lambda zh, en: en if language == 'en' else zh
    synced_agents = sync_recent_agent_chats_for_relay(respect_budget=False) if sync_first else []
    if link_unlinked:
        link_failures = _link_unlinked_conversations_to_projects()
        if link_failures:
            synced_agents.append({"agent_id": None, "status": "error", "error": "Some conversations stayed unlinked after history sync"})
    consumer_norm = str(consumer or "").strip().lower()

    with db() as conn:
        proj_rows = conn.execute(
            "SELECT * FROM projects WHERE status = 'active' ORDER BY updated_at DESC"
        ).fetchall()
        projects = [row_to_dict(r) for r in proj_rows]

        candidates: list[dict[str, Any]] = []
        for proj in projects:
            pid = proj["id"]
            pname = proj.get("name") or local("未命名项目", "Untitled project")
            pdirs = proj.get("directories") or []
            if isinstance(pdirs, str):
                pdirs = decode(pdirs, [])
            if not pdirs:
                pdirs = project_service._get_project_directories(conn, pid)

            conv_rows = conn.execute(
                """
                SELECT * FROM conversations
                WHERE project_id = ?
                ORDER BY COALESCE(updated_at, created_at) DESC
                LIMIT 50
                """,
                (pid,),
            ).fetchall()
            convs = [row_to_dict(r) for r in conv_rows]
            convs.sort(
                key=lambda c: (
                    project_service._ts_epoch_ms(c.get("updated_at") or c.get("created_at")),
                    str(c.get("updated_at") or c.get("created_at") or ""),
                    str(c.get("id") or ""),
                ),
                reverse=True,
            )

            # Filter out empty/pure-trigger turns from the current consumer if other conversations exist
            meaningful_convs: list[dict[str, Any]] = []
            for c in convs:
                msgs = c.get("messages") or []
                if isinstance(msgs, str):
                    msgs = decode(msgs, [])
                if (
                    str(c.get("agent") or "").lower() == consumer_norm
                    and _is_continuation_only_session(msgs)
                ):
                    continue
                meaningful_convs.append(c)

            summarized_convs = [_summarize_conversation_for_relay(c, language) for c in meaningful_convs[:5]]
            open_tasks, completed_tasks = list_project_tasks(conn, pid)
            total_conv_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM conversations WHERE project_id = ?",
                (pid,),
            ).fetchone()
            total_conv_count = int(total_conv_row["cnt"] or 0) if total_conv_row else len(meaningful_convs)
            filtered_out_count = max(0, len(convs) - len(meaningful_convs))
            effective_conv_count = max(len(meaningful_convs), total_conv_count - filtered_out_count)

            mem_count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM contents WHERE project_id = ? AND kind = 'memory' AND category != 'task' AND status = 'active'",
                (pid,),
            ).fetchone()
            memory_count = int(mem_count_row["cnt"] or 0) if mem_count_row else 0

            recent_agents: list[str] = []
            for sc in summarized_convs:
                ag = str(sc.get("agent") or "").strip().lower()
                if ag and ag not in recent_agents:
                    recent_agents.append(ag)
            for t in open_tasks:
                ag = str(t.get("agent") or "").strip().lower()
                if ag and ag not in recent_agents:
                    recent_agents.append(ag)

            last_active_at = (
                project_service._latest_timestamp(
                    summarized_convs[0]["updated_at"] if summarized_convs else None,
                    *(t.get("updated_at") for t in open_tasks),
                    proj.get("updated_at"),
                    proj.get("created_at"),
                )
                or ""
            )
            last_agent = recent_agents[0] if recent_agents else ""

            top_c = summarized_convs[0] if summarized_convs else None
            latest_conversation_title = str(top_c.get("title") or "") if top_c else ""
            latest_conversation_summary = str(top_c.get("brief_summary") or "") if top_c else ""

            summary_bits: list[str] = []
            if top_c:
                summary_bits.append(f"[{top_c['agent']}] {top_c['title']} — {top_c['brief_summary']}")
            if open_tasks:
                shown = open_tasks[:3]
                task_titles = "、".join(str(t.get("title") or t["id"]) for t in shown)
                extra = len(open_tasks) - len(shown)
                suffix = local(f"；另有 {extra} 个，完整标题在 open_tasks", f"; {extra} more, full titles are in open_tasks") if extra else ""
                summary_bits.append(local("进行中任务", "Open tasks") + f"({len(open_tasks)}): {task_titles}{suffix}")
            if not summary_bits:
                summary_bits.append(proj.get("description") or local("暂无近期跨 Agent 聊天摘要", "No recent cross-agent conversation summary"))

            candidates.append(
                {
                    "project_id": pid,
                    "project_name": pname,
                    "description": proj.get("description") or "",
                    "directories": pdirs,
                    "recent_agents": recent_agents,
                    "last_agent": last_agent,
                    "last_active_at": last_active_at,
                    "conversation_count": effective_conv_count,
                    "memory_count": memory_count,
                    "latest_conversation_title": latest_conversation_title,
                    "latest_conversation_summary": latest_conversation_summary,
                    "recent_conversations": summarized_convs,
                    "open_tasks": [_task_candidate(t) for t in open_tasks],
                    "completed_task_count": len(completed_tasks),
                    "brief_summary": " | ".join(summary_bits),
                }
            )

    # Sort projects with actual conversations/tasks first by last_active_at descending
    candidates.sort(
        key=lambda item: (
            1 if (item["conversation_count"] > 0 or len(item["open_tasks"]) > 0) else 0,
            project_service._ts_epoch_ms(item.get("last_active_at")),
            str(item.get("last_active_at") or ""),
        ),
        reverse=True,
    )
    selected_candidates = candidates[: max(1, min(limit, 50))]
    for idx, cand in enumerate(selected_candidates, 1):
        cand["index"] = idx

    if selected_candidates:
        lines = [
            local("以下项目来自本机已有记录；本次同步仅覆盖已授权且未暂停的宿主。", "Projects below come from existing local records. Sync covers only authorized, unpaused hosts."),
        ]
        for cand in selected_candidates:
            agents_str = ", ".join(cand["recent_agents"]) if cand["recent_agents"] else local("暂无会话记录", "No conversations")
            time_str = str(cand.get("last_active_at") or "")[:19]
            lines.append(
                f"{cand['index']}. 【{cand['project_name']}】 (ID: `{cand['project_id']}`"
                + local(" · 最近 Agent: ", " · Recent agents: ") + agents_str
                + (local(" · 更新: ", " · Updated: ") + time_str if time_str else "")
                + local(")\n   - 简短概括：", ")\n   - Summary: ") + cand['brief_summary']
            )
        lines.append("")
        lines.append(
            local("请问你要接力的项目是哪一个？（可回复项目编号或名称，并可选择获取「压缩短上下文 (compressed)」或「完整上下文 (full)」后继续接力）", "Which project would you like to continue? Specify its number or name, and choose compressed or full context.")
        )
        question_prompt = "\n".join(lines)
    else:
        question_prompt = local("本机现有记录中尚未找到可接力的项目或历史会话。请指定项目或任务，并检查所需宿主的读取授权。", "No relay project or conversation was found in local records. Specify a project or task and check history access for the required host.")

    conv_discovery = discover_relay_conversations(
        consumer=consumer_norm or None,
        limit=max(10, min(limit * 2, 50)),
        sync_first=False,
        language=language,
        link_unlinked=False,
    )

    return {
        "ok": True,
        "status": "selection_required",
        "consumer": consumer_norm or None,
        "synced_agents": synced_agents,
        "sync_failed": any(item.get('status') == 'error' for item in synced_agents),
        "supported_agents": list(ALLOWED_TARGET_AGENTS),
        "supported_context_modes": ["compressed", "full"],
        "total": len(selected_candidates),
        "candidates": selected_candidates,
        "relay_candidates": selected_candidates,
        "conversation_candidates": conv_discovery.get("candidates", []),
        "conversation_question_prompt": conv_discovery.get("question_prompt", ""),
        "question_prompt": question_prompt,
    }



_RELAY_SNAPSHOT_TTL_MINUTES = 30
_RELAY_SNAPSHOT_CACHE_LOCK = threading.Lock()
_RELAY_SNAPSHOT_MEM_CACHE: dict[tuple[str, str], tuple[dict[str, str], str]] = {}


def _ensure_relay_snapshot_schema(conn) -> None:
    """A printed number must keep pointing at the conversation the user saw."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relay_discovery_snapshots'"
    ).fetchone()
    if exists is not None:
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS relay_discovery_snapshots (
            scope_key TEXT PRIMARY KEY,
            mapping_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )


def _relay_snapshot_cutoff() -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=_RELAY_SNAPSHOT_TTL_MINUTES)).isoformat()


def _relay_scope_keys(consumer_norm: str, scoped_project_id: str | None,
                      scoped_cwd: str | None) -> list[str]:
    """Numbering belongs to the consumer that received it, never another host."""
    scope = scoped_project_id or scoped_cwd or "__global__"
    return [f"{consumer_norm or '*'}::{scope}"]


def _update_relay_snapshot_mem_cache(
    scope_keys: list[str],
    mapping: dict[str, str],
    offset: int,
    stamp: str,
) -> None:
    db_key = str(settings.db_path)
    cutoff_ms = project_service._ts_epoch_ms(_relay_snapshot_cutoff())
    with _RELAY_SNAPSHOT_CACHE_LOCK:
        stale_keys = [
            cache_key
            for cache_key, (_m, updated_at) in _RELAY_SNAPSHOT_MEM_CACHE.items()
            if project_service._ts_epoch_ms(updated_at) < cutoff_ms
        ]
        for cache_key in stale_keys:
            _RELAY_SNAPSHOT_MEM_CACHE.pop(cache_key, None)
        for key in scope_keys:
            cache_key = (db_key, key)
            payload = dict(mapping)
            if offset > 0 and cache_key in _RELAY_SNAPSHOT_MEM_CACHE:
                prior, prior_ts = _RELAY_SNAPSHOT_MEM_CACHE[cache_key]
                if (
                    isinstance(prior, dict)
                    and prior
                    and project_service._ts_epoch_ms(prior_ts) >= cutoff_ms
                ):
                    payload = {**prior, **mapping}
            _RELAY_SNAPSHOT_MEM_CACHE[cache_key] = (payload, stamp)


def _clear_relay_snapshot_mem_cache(scope_keys: list[str]) -> None:
    db_key = str(settings.db_path)
    with _RELAY_SNAPSHOT_CACHE_LOCK:
        for key in scope_keys:
            _RELAY_SNAPSHOT_MEM_CACHE.pop((db_key, key), None)


def _save_relay_discovery_snapshot(conn, scope_keys: list[str], mapping: dict[str, str],
                                   offset: int) -> None:
    """Remember index -> conversation_id for one page of a listing."""
    if not scope_keys:
        return
    _ensure_relay_snapshot_schema(conn)
    cutoff = _relay_snapshot_cutoff()
    cutoff_ms = project_service._ts_epoch_ms(cutoff)
    db_key = str(settings.db_path)
    # An expired page must never resurrect an old numbering.
    conn.execute("DELETE FROM relay_discovery_snapshots WHERE updated_at < ?", (cutoff,))
    stamp = now_iso()
    for key in scope_keys:
        payload = mapping
        if offset > 0:
            row = conn.execute(
                "SELECT mapping_json FROM relay_discovery_snapshots WHERE scope_key = ?", (key,)
            ).fetchone()
            prior = decode(row["mapping_json"], {}) if row is not None else {}
            if not (isinstance(prior, dict) and prior):
                with _RELAY_SNAPSHOT_CACHE_LOCK:
                    mem_entry = _RELAY_SNAPSHOT_MEM_CACHE.get((db_key, key))
                if (
                    mem_entry is not None
                    and project_service._ts_epoch_ms(mem_entry[1]) >= cutoff_ms
                    and isinstance(mem_entry[0], dict)
                ):
                    prior = mem_entry[0]
            if isinstance(prior, dict) and prior:
                # A later page must not erase earlier pages of the same listing.
                payload = {**prior, **mapping}
        conn.execute(
            """INSERT INTO relay_discovery_snapshots(scope_key,mapping_json,updated_at)
               VALUES(?,?,?) ON CONFLICT(scope_key) DO UPDATE SET
               mapping_json=excluded.mapping_json,updated_at=excluded.updated_at""",
            (key, encode(payload), stamp),
        )


def _lookup_relay_discovery_snapshot(conn, index: str, scope_keys: list[str]) -> str:
    cutoff = _relay_snapshot_cutoff()
    cutoff_ms = project_service._ts_epoch_ms(cutoff)
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='relay_discovery_snapshots'"
    ).fetchone() is not None
    db_key = str(settings.db_path)
    for key in scope_keys:
        best_mapping: dict[str, Any] | None = None
        best_ms = -1
        if table_exists:
            try:
                row = conn.execute(
                    "SELECT mapping_json, updated_at FROM relay_discovery_snapshots WHERE scope_key = ?",
                    (key,),
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
            if row is not None:
                row_ms = project_service._ts_epoch_ms(row["updated_at"])
                if row_ms >= cutoff_ms:
                    decoded = decode(row["mapping_json"], {})
                    if isinstance(decoded, dict):
                        best_mapping = decoded
                        best_ms = row_ms
        with _RELAY_SNAPSHOT_CACHE_LOCK:
            mem_entry = _RELAY_SNAPSHOT_MEM_CACHE.get((db_key, key))
        if mem_entry is not None:
            mem_mapping, mem_updated_at = mem_entry
            mem_ms = project_service._ts_epoch_ms(mem_updated_at)
            if mem_ms >= cutoff_ms and mem_ms > best_ms and isinstance(mem_mapping, dict):
                best_mapping = mem_mapping
                best_ms = mem_ms
        if best_mapping is None:
            continue
        conv_id = str(best_mapping.get(index) or "").strip()
        if not conv_id:
            raise HTTPException(409, {
                "code": "relay_selection_unavailable",
                "message": "This number was not in the displayed list. Refresh the list or use the exact conversation ID.",
                "must_not_execute": True,
            })
        if not conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv_id,)).fetchone():
            raise HTTPException(410, {
                "code": "relay_selection_deleted",
                "message": "The selected conversation was deleted. Refresh the list; no other conversation was loaded.",
                "must_not_execute": True,
            })
        return conv_id
    raise HTTPException(409, {
        "code": "relay_selection_expired",
        "message": "The displayed list is missing or expired. Refresh the list or use the exact conversation ID.",
        "must_not_execute": True,
    })


def _relay_project_names(conn, local) -> dict[str, str]:
    return {
        row["id"]: (row["name"] or local("未命名项目", "Untitled project"))
        for row in conn.execute("SELECT id, name FROM projects WHERE status = 'active'").fetchall()
    }


def _resolve_relay_scope(conn, *, consumer_norm: str, cwd: str | None, project_id: str | None,
                         local) -> tuple[str | None, str, str | None]:
    """Shared listing/selection scope so a numbered page keeps its meaning."""
    from app.services import conversation_service

    scoped_project_id: str | None = None
    scoped_project_name: str = ""
    scoped_cwd: str | None = None
    home_canon = project_service.canonical_project_path(str(Path.home()))
    project_names = _relay_project_names(conn, local)

    if project_id and str(project_id).strip():
        raw_pid = str(project_id).strip()
        matches = conn.execute(
            "SELECT id, name FROM projects WHERE (id = ? OR lower(name) = lower(?)) AND status = 'active'",
            (raw_pid, raw_pid),
        ).fetchall()
        exact = next((row for row in matches if row["id"] == raw_pid), None)
        if not matches:
            raise HTTPException(404, "Project not found")
        if not exact and len(matches) > 1:
            raise HTTPException(422, {"code": AMBIGUOUS_PROJECT_CODE, "candidates": [dict(row) for row in matches]})
        prow = exact or matches[0]
        scoped_project_id = prow["id"]
        scoped_project_name = prow["name"] or ""

    if cwd and str(cwd).strip():
        try:
            scoped_cwd = project_service.canonical_project_path(str(cwd).strip())
        except Exception:
            scoped_cwd = str(cwd).strip()
        if not scoped_project_id and scoped_cwd:
            matched, ambiguous = project_service.resolve_project_by_path(scoped_cwd)
            if matched and not ambiguous:
                m_dirs = [project_service.canonical_project_path(d) for d in (matched.get("directories") or [])]
                if scoped_cwd == home_canon or m_dirs != [home_canon]:
                    scoped_project_id = matched["id"]
                    scoped_project_name = matched.get("name") or ""

    # Automatic workspace inference when caller did not pass cwd/project_id:
    # scope discovery to the newest consumer continuation-trigger session.
    if not scoped_project_id and not scoped_cwd and consumer_norm:
        columns = ",".join(f"c.{name}" for name in conversation_service._CONVERSATION_INDEX_COLUMNS.split(","))
        source_join = (
            "FROM conversations c LEFT JOIN conversation_relay_index r "
            f"ON r.conversation_id=c.id AND r.source_hash IN ('v2:' || coalesce(c.content_hash,''), '{conversation_service.RELAY_INDEX_VERSION_PREFIX}' || coalesce(c.content_hash,''))"
        )
        latest_consumer_rows = conn.execute(
            f"""SELECT {columns}, r.continuation_only {source_join}
                WHERE lower(c.agent) = ?
                ORDER BY c.updated_at DESC,c.id DESC LIMIT 1""",
            (consumer_norm,),
        ).fetchall()
        if latest_consumer_rows:
            latest_c = dict(latest_consumer_rows[0])
            if latest_c.get("continuation_only"):
                if latest_c.get("cwd"):
                    try:
                        cand_cwd = project_service.canonical_project_path(str(latest_c["cwd"]))
                        if cand_cwd != home_canon:
                            scoped_cwd = cand_cwd
                    except Exception:
                        pass
                if latest_c.get("project_id"):
                    pid_val = str(latest_c["project_id"])
                    p_dirs = [
                        project_service.canonical_project_path(row["path"])
                        for row in conn.execute(
                            "SELECT path FROM project_directories WHERE project_id = ?", (pid_val,)
                        ).fetchall()
                    ]
                    if p_dirs != [home_canon]:
                        scoped_project_id = pid_val
                        scoped_project_name = project_names.get(pid_val, "")
    return scoped_project_id, scoped_project_name, scoped_cwd


def discover_relay_conversations(
    *,
    consumer: str | None = None,
    limit: int = 15,
    sync_first: bool = True,
    language: str = 'zh',
    link_unlinked: bool = True,
    cwd: str | None = None,
    project_id: str | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Refresh a bounded recent batch, then page only indexed conversation metadata."""
    check_language(language)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise HTTPException(422, "limit must be between 1 and 50")
    if type(offset) is not int or offset < 0:
        raise HTTPException(422, "offset must be nonnegative")
    local = lambda zh, en: en if language == 'en' else zh
    from app.services import conversation_service
    synced_agents = sync_recent_agent_chats_for_relay(respect_budget=False) if sync_first else []
    if link_unlinked:
        link_failures = _link_unlinked_conversations_to_projects()
        if link_failures:
            synced_agents.append({"agent_id": None, "status": "error", "error": "Some conversations stayed unlinked after history sync"})
    sync_pending = any(item.get("status") in ("pending", "partial", "waiting") for item in synced_agents)
    sync_unavailable = any(item.get("reason") == "background_refresh_unavailable" for item in synced_agents)
    consumer_norm = str(consumer or "").strip().lower()

    with db() as conn:
        conversation_service._ensure_relay_index(conn)
        conn.execute("BEGIN")
        scoped_project_id, scoped_project_name, scoped_cwd = _resolve_relay_scope(
            conn, consumer_norm=consumer_norm, cwd=cwd, project_id=project_id, local=local,
        )
        columns = ",".join(f"c.{name}" for name in conversation_service._CONVERSATION_INDEX_COLUMNS.split(","))
        source_join = (
            "FROM conversations c LEFT JOIN conversation_relay_index r "
            f"ON r.conversation_id=c.id AND r.source_hash IN ('v2:' || coalesce(c.content_hash,''), '{conversation_service.RELAY_INDEX_VERSION_PREFIX}' || coalesce(c.content_hash,''))"
        )
        project_names = _relay_project_names(conn, local)

        clauses = [
            "coalesce(r.continuation_only,0)=0",
            "coalesce(r.has_human_turn,1)=1",
            "(r.message_count IS NULL OR r.message_count > 0)",
            "NOT (lower(coalesce(c.agent,''))='omp' AND coalesce(c.source_path,'') GLOB '*/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*_*/*.jsonl')",
        ]
        params: tuple = ()
        if scoped_project_id:
            # The project's own chats, plus any still-unlinked chat that is newer than
            # this project's latest activity (a session that just finished and has not
            # been assigned yet). Older unlinked chats are other projects' noise.
            clauses.append(
                "(c.project_id=? OR ((c.project_id IS NULL OR c.project_id='') AND c.updated_at > "
                "coalesce((SELECT max(updated_at) FROM conversations WHERE project_id=?), '')))"
            )
            params = (scoped_project_id, scoped_project_id)
        elif scoped_cwd:
            clauses.append(
                "(c.cwd=? OR c.cwd LIKE ? ESCAPE '\\' OR ((c.project_id IS NULL OR c.project_id='') AND c.updated_at > "
                "coalesce((SELECT max(updated_at) FROM conversations WHERE cwd=? OR cwd LIKE ? ESCAPE '\\'), '')))"
            )
            params = (scoped_cwd, conversation_service.escape_like(scoped_cwd.rstrip("/")) + "/%",
                      scoped_cwd, conversation_service.escape_like(scoped_cwd.rstrip("/")) + "/%")
        where = "WHERE " + " AND ".join(clauses)
        total_candidates = conn.execute(f"SELECT COUNT(*) {source_join} {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT {columns}, r.title_{language} AS relay_title,
                r.brief_{language} AS brief_summary, r.message_count
                {source_join} {where}
                ORDER BY c.updated_at DESC,c.id DESC LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        selected_convs = []
        for row in rows:
            c = dict(row)
            pid = c.get("project_id")
            ag = str(c.get("agent") or "").strip().lower()
            indexed = c.get("relay_title") is not None
            selected_convs.append({
                "conversation_id": c["id"], "id": c["id"], "external_id": c.get("external_id"),
                "scope_match": "unassigned" if not pid else ("project" if scoped_project_id == pid else "cwd" if scoped_cwd else "all"),
                "agent": ag, "is_cross_agent": bool(consumer_norm and ag and ag != consumer_norm),
                "project_id": pid,
                "project_name": project_names.get(pid) or local("未绑定目录会话", "Unlinked conversation"),
                "cwd": c.get("cwd") or "", "git_branch": _branch_label(c, language),
                "git_head": c.get("git_head") or "", "updated_at": c.get("updated_at") or "",
                "title": c.get("relay_title") or _clean_line(c.get("title") or "", 120) or local("未命名会话", "Untitled conversation"),
                "brief_summary": c.get("brief_summary") or local(
                    "尚无索引摘要；选择此会话可读取正文。", "No indexed summary yet; select this conversation to read it.",
                ),
                "user_goal": "", "message_count": c.get("message_count"),
                "summary_status": "indexed" if indexed else "metadata_only",
            })
    used_project_scope = bool(scoped_project_id or scoped_cwd)
    for idx, item in enumerate(selected_convs, offset + 1):
        item["index"] = idx
    # Freeze this page so a later numbered reply resolves to what was shown,
    # even if background sync inserted newer conversations in between.
    scope_keys = _relay_scope_keys(consumer_norm, scoped_project_id, scoped_cwd)
    page_mapping = {str(item["index"]): item["conversation_id"] for item in selected_convs}
    try:
        with db() as conn:
            _save_relay_discovery_snapshot(conn, scope_keys, page_mapping, offset)
        _clear_relay_snapshot_mem_cache(scope_keys)
    except sqlite3.OperationalError as exc:
        err_lower = str(exc).lower()
        if "locked" not in err_lower and "busy" not in err_lower:
            raise
        _update_relay_snapshot_mem_cache(scope_keys, page_mapping, offset, now_iso())

    scope_label = scoped_project_name or (Path(scoped_cwd).name if scoped_cwd else "")
    if selected_convs:
        if used_project_scope and scope_label:
            header_line = local(
                f"以下为当前项目【{scope_label}】的近期对话，以及单独标明「未绑定目录会话」的未知项目记录（不代表属于当前项目）：",
                f"Recent conversations for [{scope_label}], plus records labeled 'Unlinked conversation' whose project is unknown (not assumed to belong to this project):",
            )
        else:
            header_line = local(
                "以下为本机各已授权 Agent 的近期对话记录，按更新时间倒序排列（也包含同 Agent 历史会话与未知项目会话）：",
                "Recent conversations across authorized Agents, newest first (including same-Agent and unknown-project sessions):",
            )
        lines = [header_line]
        for item in selected_convs:
            badge = (
                local("跨Agent", "cross-agent")
                if item.get("is_cross_agent")
                else local("同Agent", "same-agent")
            )
            time_str = str(item.get("updated_at") or "")[:19]
            lines.append(
                f"{item['index']}. [{item['agent']} · {badge}] 【{item['project_name']}】 {item['title']} "
                f"(ID: `{item['conversation_id']}`"
                + (f" · {time_str}" if time_str else "")
                + f")\n   - {local('目录', 'Directory')}: {str(item.get('cwd') or '').strip() or local('未记录', 'not recorded')}"
                + f" · {local('分支', 'Branch')}: {str(item.get('git_branch') or '').strip() or local('未记录', 'not recorded')}"
                + f"\n   - {item['brief_summary']}"
            )
        lines.append("")
        remaining = max(0, total_candidates - offset - len(selected_convs))
        lines.append(local(
            f"共 {total_candidates} 条；本页 {len(selected_convs)} 条，后续还有 {remaining} 条。可用 offset={offset + len(selected_convs)} 继续查看；选择时优先使用稳定的会话 ID。",
            f"{total_candidates} total; {len(selected_convs)} on this page, {remaining} remaining. Continue with offset={offset + len(selected_convs)}; prefer a stable conversation ID when selecting.",
        ))
        lines.append(
            local(
                "请直接回复要加载的「对话序号」（如 1）或 `conversation_id`；选定后将加载该条对话上下文并向你汇报进度，等待你的下一步指令。",
                "Reply with the conversation number (e.g. 1) or `conversation_id`; selecting it loads that conversation's context, reports a brief summary, and waits for your next instruction.",
            )
        )
        question_prompt = "\n".join(lines)
    elif total_candidates:
        question_prompt = local(
            f"共 {total_candidates} 条可选会话；offset={offset} 已超出列表，请从 offset=0 查看。",
            f"{total_candidates} conversations are available; offset={offset} is past the end. Restart at offset=0.",
        )
    elif sync_pending:
        question_prompt = local(
            "本机后台刷新尚不可用；当前没有已入库的可选会话。请恢复后台服务后重试，不能据此判断宿主没有历史记录。"
            if sync_unavailable else "已请求本机后台刷新，当前可选会话仍在等待入库。请稍后再次查询，不能据此判断宿主没有历史记录。",
            "Background refresh is unavailable; no selectable records are stored yet. Restore the background service and retry; this does not mean native history is empty."
            if sync_unavailable else "Background refresh requested; selectable conversations are still being imported. Retry shortly; this does not mean native history is empty.",
        )
    elif used_project_scope and scope_label:
        question_prompt = local(
            f"当前项目【{scope_label}】里没有可接力的对话。没有改成展示其他项目。",
            f"Project [{scope_label}] has no conversation that can be continued. Other projects were not added.",
        )
    else:
        question_prompt = local(
            "本机现有记录中尚未找到可接力的历史对话记录。请先在「Agent 管理」中一键接入所需助手。",
            "No recent conversation records found locally. Connect your agents in Agent Management first.",
        )
    if sync_pending and selected_convs:
        question_prompt += "\n" + local(
            "以上为已入库记录；后台刷新尚未完成，列表可能还缺最新会话。",
            "These are stored records; background refresh is pending and newer sessions may not appear yet.",
        )

    return {
        "ok": True,
        "status": "sync_pending" if sync_pending and not total_candidates else "selection_required",
        "selection_kind": "conversation",
        "consumer": consumer_norm or None,
        "scoped_project_id": scoped_project_id if used_project_scope else None,
        "scoped_project_name": scoped_project_name if used_project_scope else None,
        "scoped_cwd": scoped_cwd if used_project_scope else None,
        "scope_widened": False,
        "synced_agents": synced_agents,
        "sync_failed": any(item.get("status") == "error" for item in synced_agents),
        "sync_pending": sync_pending,
        "sync_unavailable": sync_unavailable,
        "must_not_execute": True,
        "supported_agents": list(ALLOWED_TARGET_AGENTS),
        "total": len(selected_convs),
        "total_candidates": total_candidates,
        "offset": offset,
        "limit": limit,
        "omitted_count": total_candidates - len(selected_convs),
        "has_more": offset + len(selected_convs) < total_candidates,
        "next_offset": offset + len(selected_convs) if offset + len(selected_convs) < total_candidates else None,
        "candidates": selected_convs,
        "conversation_candidates": selected_convs,
        "question_prompt": question_prompt,
    }


def _resolve_relay_conversation_identifier(
    identifier: str, candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve exact native identity first; ambiguous IDs/titles require selection."""
    from app.services.conversation_service import _CONVERSATION_INDEX_COLUMNS, escape_like

    raw = str(identifier or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        selected = next((c for c in candidates if c.get("index") == int(raw)), None)
        if not selected:
            return None
        raw = selected.get("conversation_id") or selected["id"]
    with db() as conn:
        row = conn.execute(
            f"SELECT {_CONVERSATION_INDEX_COLUMNS} FROM conversations WHERE id = ?", (raw,)
        ).fetchone()
        if row:
            return dict(row)
        native_uuid = bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", raw))
        if native_uuid:
            rows = conn.execute(
                f"SELECT {_CONVERSATION_INDEX_COLUMNS} FROM conversations "
                "WHERE lower(external_id) = lower(?) OR lower(external_id) LIKE ? ESCAPE '\\'",
                (raw, "%:" + escape_like(raw.lower())),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_CONVERSATION_INDEX_COLUMNS} FROM conversations WHERE external_id = ?", (raw,)
            ).fetchall()
        if not rows and not native_uuid and not raw.startswith("conv_") and ":" not in raw:
            rows = conn.execute(
                f"SELECT {_CONVERSATION_INDEX_COLUMNS} FROM conversations WHERE lower(title) = lower(?)", (raw,)
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    f"SELECT {_CONVERSATION_INDEX_COLUMNS} FROM conversations WHERE lower(title) LIKE ? ESCAPE '\\'",
                    ("%" + escape_like(raw.lower()) + "%",),
                ).fetchall()
        if len(rows) > 1:
            raise HTTPException(422, {
                "code": AMBIGUOUS_CONVERSATION_CODE,
                "message": "Multiple conversations match; select an exact conversation_id.",
                "candidates": [{"conversation_id": r["id"], "external_id": r["external_id"],
                                "agent": r["agent"], "title": r["title"]} for r in rows],
            })
        return dict(rows[0]) if rows else None


def relay_conversation(
    conversation_id: str | None = None,
    *,
    consumer: str | None = None,
    max_tokens: int = 12000,
    sync_first: bool = True,
    language: str = 'zh',
    query: str | None = None,
    cwd: str | None = None,
    project_id: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    """One-step conversation-first relay for slash commands (/contextcore):
    - When conversation_id is omitted, returns the numbered list of recent conversations across all agents (scoped to cwd/project_id when provided).
    - A numbered reply resolves only to the consumer's unexpired displayed list.
      Missing, expired, or deleted selections require a new choice; numbers are
      never re-ranked against current history.
    - Selecting a conversation refreshes only its authorized native source, loads
      that conversation, and waits for a concrete instruction. Selection alone
      never creates or confirms a handoff or chooses an unrelated working task.
    """
    check_language(language)
    local = lambda zh, en: en if language == 'en' else zh
    if type(max_tokens) is not int or not 1 <= max_tokens <= 32000:
        raise HTTPException(status_code=422, detail="Context budget must be between 1 and 32000")
    target_agent = str(consumer or "").strip().lower()
    if target_agent not in ALLOWED_TARGET_AGENTS:
        raise HTTPException(status_code=422, detail="An explicit supported target Agent is required")

    list_request = not conversation_id or str(conversation_id).strip() in ("", "?", "list", "select")
    candidates: list[dict[str, Any]] = []
    if list_request:
        return discover_relay_conversations(
            consumer=target_agent, limit=limit, offset=offset, sync_first=sync_first,
            language=language, link_unlinked=True, cwd=cwd, project_id=project_id,
        )
    frozen_conv_id: str | None = None
    if str(conversation_id).strip().isdigit():
        # A number printed by an earlier listing must resolve to the record the
        # user actually saw, even when background sync inserted newer chats.
        index = str(int(str(conversation_id).strip()))
        from app.services import conversation_service
        with db() as conn:
            conversation_service._ensure_relay_index(conn)
            scoped_pid, _scoped_pname, scoped_cwd = _resolve_relay_scope(
                conn, consumer_norm=target_agent, cwd=cwd, project_id=project_id, local=local,
            )
            frozen_conv_id = _lookup_relay_discovery_snapshot(
                conn, index, _relay_scope_keys(target_agent, scoped_pid, scoped_cwd),
            )
    selected_conv = _resolve_relay_conversation_identifier(
        frozen_conv_id or str(conversation_id), candidates,
    )
    synced_agents = []
    if sync_first:
        from app.services import connection_service
        synced_agents = connection_service.sync_conversation_for_relay(
            (selected_conv.get("external_id") or selected_conv["id"]) if selected_conv else str(conversation_id),
            agent=selected_conv.get("agent") if selected_conv else None,
            source_path=selected_conv.get("source_path") if selected_conv else None,
        )
        selected_conv = _resolve_relay_conversation_identifier(
            selected_conv["id"] if selected_conv and str(conversation_id).strip().isdigit() else str(conversation_id),
            candidates,
        )
    if not selected_conv:
        raise HTTPException(status_code=404, detail=f"Conversation '{conversation_id}' not found")
    from app.services import conversation_service
    selected_conv = conversation_service.get_conversation(selected_conv["id"])
    history_fresh = bool(sync_first and synced_agents) and all(
        item.get("status") == "ok"
        or (item.get("status") == "skipped" and item.get("reason") == "source_unchanged")
        for item in synced_agents
    )
    sync_failed = bool(sync_first and not history_fresh)
    has_task = _relay_has_task_instruction(query) and not sync_failed

    resolved_pid = selected_conv.get("project_id")
    if not resolved_pid and selected_conv.get("cwd"):
        try:
            resolved_pid = project_service.reconstruct_or_resolve_project(selected_conv["cwd"])
        except Exception:
            resolved_pid = None

    pname = local("未绑定目录会话", "Unlinked conversation")
    pdirs: list[str] = []
    with db() as conn:
        if resolved_pid:
            prow = conn.execute("SELECT * FROM projects WHERE id = ?", (resolved_pid,)).fetchone()
            if prow:
                proj = row_to_dict(prow)
                pname = proj.get("name") or pname
                pdirs = proj.get("directories") or []
                if isinstance(pdirs, str):
                    pdirs = decode(pdirs, [])
                if not pdirs:
                    pdirs = project_service._get_project_directories(conn, resolved_pid)
        if not pdirs and selected_conv.get("cwd"):
            pdirs = [str(selected_conv["cwd"])]

        from app.services import governance_guardrails

        if resolved_pid:
            mem_rows = conn.execute(
                """
                SELECT * FROM contents
                WHERE (project_id = ? OR (project_id IS NULL AND category IN ('preference', 'lesson')))
                  AND status = 'active' AND category IN ('decision', 'preference')
                ORDER BY updated_at DESC
                """,
                (resolved_pid,),
            ).fetchall()
            if has_task:
                open_tasks, completed_tasks = list_project_tasks(conn, resolved_pid)
            else:
                # Loading one source must not infer a task from unrelated work.
                from app.services import working_memory_service
                working_memory_service.ensure_schema(conn)
                rows = conn.execute(
                    """SELECT * FROM working_tasks wt WHERE project_id=? AND source_conversation_id=?
                       AND (legacy_content_id IS NULL OR EXISTS (
                           SELECT 1 FROM contents c WHERE c.id=wt.legacy_content_id AND c.status='active'
                       ))""",
                    (resolved_pid, selected_conv["id"]),
                ).fetchall()
                linked = [
                    _working_task_to_handoff_item(working_memory_service._row_to_task(row, []), None)
                    for row in rows
                ]
                open_tasks = [t for t in linked if is_open_task(t)]
                completed_tasks = [t for t in linked if task_progress_status(t) == "completed"]
        else:
            mem_rows = conn.execute(
                """
                SELECT * FROM contents
                WHERE project_id IS NULL AND status = 'active' AND category IN ('decision', 'preference')
                ORDER BY updated_at DESC
                """
            ).fetchall()
            open_tasks, completed_tasks = [], []
        active_memories = [
            item for r in mem_rows
            if not governance_guardrails.is_tombstoned_memory(
                conn,
                project_id=(item := row_to_dict(r)).get("project_id"),
                category=str(item.get("category") or ""),
                title=str(item.get("title") or ""),
                content=str(item.get("content") or ""),
                include_cross_category=True,
            )
        ]

    source_agent = str(selected_conv.get("agent") or target_agent).strip().lower()
    conv_summary = _summarize_conversation_for_relay(selected_conv, language)
    display_title = conv_summary.get("title") or selected_conv.get("title") or selected_conv["id"]
    decisions_and_prefs = [
        {
            "id": m["id"],
            "category": m["category"],
            "title": m["title"],
            "content": m["content"],
            "version": m.get("version", 1),
        }
        for m in active_memories
        if m.get("category") in ("decision", "preference")
    ]

    handoff = None
    created_new_handoff_id: str | None = None
    if has_task and resolved_pid and source_agent != target_agent and source_agent in ALLOWED_TARGET_AGENTS:
        created = create_handoff(
            project_id=resolved_pid,
            target_agent=target_agent,
            conversation_id=selected_conv["id"],
            query=query,
            source_agent=source_agent,
            actor=target_agent,
            language=language,
        )
        # Confirmation is deferred until the selected-context payload actually fits.
        handoff = created
        created_new_handoff_id = created["id"]

    if handoff:
        card = handoff.get("card") if isinstance(handoff.get("card"), dict) else decode(handoff.get("card"), {})
        continuation = continuation_contract(handoff, target_agent)
    else:
        decision_lines = [
            f"[{d['id']} v{d['version']} · {d['category']}] {d['title']}: {d['content']}"
            for d in decisions_and_prefs[:12]
        ]
        wait_steps = local(
            "已加载；等待用户下一步指令。",
            "Loaded; wait for the user's next instruction.",
        )
        state_note = _code_state_text(project_service.worktree_code_state(selected_conv.get("cwd")))
        rendered_state = render(str(state_note), getattr(state_note, "spans", []), language)
        card = {
            "goal": (query if has_task else None) or conv_summary.get("user_goal") or selected_conv.get("title") or local("继续所选对话任务", "Continue selected conversation"),
            "progress": conv_summary.get("brief_summary") or local("已加载所选对话记录", "Loaded selected conversation record"),
            "decisions": "\n".join(decision_lines) if decision_lines else local("沿用当前项目与全局偏好约束", "Follow current project and global preferences"),
            "evidence": f"[{source_agent}] {display_title} ({selected_conv.get('cwd') or pname}) // {rendered_state}",
            "blockers": local("无显式阻塞", "No explicit blockers") if has_task else local("尚未收到具体执行指令", "No concrete task instruction yet"),
            "next_steps": query if has_task else wait_steps,
        }
        continuation = {
            "status": "ready" if has_task else "awaiting_confirmation",
            "must_not_execute": not has_task,
            "execution_mode": "approved",
            "target_agent": target_agent,
            "source_agent": source_agent,
            "selected_conversation_id": selected_conv["id"],
            "selected_task_id": None,
            "resume_from": card["next_steps"] if has_task else None,
            "do_not_redo": [t.get("title") for t in completed_tasks if t.get("title")],
            "parallel_open_tasks": [_task_candidate(t) for t in open_tasks],
            "excluded_historical_decisions": [],
        }

    matched_open_task = None
    sel_tid = continuation.get("selected_task_id")
    if sel_tid:
        matched_open_task = next(
            (t for t in open_tasks if t["id"] == sel_tid or t.get("_working_task_id") == sel_tid),
            None,
        )
    if matched_open_task is None:
        conv_linked = [t for t in open_tasks if t.get("source_conversation_id") == selected_conv["id"]]
        if len(conv_linked) == 1:
            matched_open_task = conv_linked[0]

    if matched_open_task and not continuation.get("selected_task_id"):
        continuation["selected_task_id"] = matched_open_task["id"]
    if matched_open_task and not handoff:
        constraints = matched_open_task.get("temp_constraints") or []
        if constraints:
            card["decisions"] += "\n" + "\n".join(str(value) for value in constraints)
        if matched_open_task.get("current_state"):
            card["progress"] += "\n" + str(matched_open_task["current_state"])

    is_discussion_only = bool(
        (matched_open_task and matched_open_task.get("execution_mode") == "discussion_only")
        or continuation.get("execution_mode") == "discussion_only"
    )
    if is_discussion_only:
        continuation["must_not_execute"] = True
        continuation["execution_mode"] = "discussion_only"
        continuation["status"] = "discussion_only"
        continuation["resume_from"] = None
        card = dict(card)
        gate = local("尚未授权改代码 (discussion_only)", "Code edits NOT authorized (discussion_only)")
        for field in ("progress", "decisions", "blockers", "next_steps"):
            if "discussion_only" not in str(card.get(field) or ""):
                card[field] = gate + "；" + str(card.get(field) or "")
    else:
        continuation.setdefault("execution_mode", "approved")
        if not has_task:
            continuation["must_not_execute"] = True
            continuation["resume_from"] = None
            if continuation.get("status") in ("ready", "awaiting_instruction"):
                continuation["status"] = "awaiting_confirmation"

    from app.services import retrieval_service

    # The protocol card remains available through read_handoff. Conversation
    # loading emits one bounded view, not repeated copies of its audit/snapshots.
    card = {field: str(card.get(field) or "") for field in HANDOFF_CARD_FIELDS}
    if handoff:
        # These twelve rules were also present in the old context alias. Keep
        # them protected in the canonical card rather than silently dropping
        # rules that the protocol card did not independently select.
        for decision in decisions_and_prefs[:12]:
            if decision["content"] not in card["decisions"]:
                card["decisions"] += (
                    f"\n[{decision['id']} v{decision['version']} · {decision['category']}] "
                    f"{decision['title']}: {decision['content']}"
                )
    summary_clipped_fields = []
    for field in ("goal", "progress", "evidence"):
        if field == "goal" and has_task:
            continue  # The concrete instruction, unlike a source digest, is protected.
        excerpt = _cap_card_excerpt(card[field], max_bytes=240, max_chars=240)
        if excerpt != card[field]:
            summary_clipped_fields.append(field)
            card[field] = excerpt
    ready = bool(has_task and not is_discussion_only)
    continuation = {
        key: continuation.get(key) for key in (
            "selected_task_id", "do_not_redo", "parallel_open_tasks",
        )
    } | {
        "status": "discussion_only" if is_discussion_only else "ready" if ready else "awaiting_confirmation",
        "must_not_execute": not ready,
        "execution_mode": "discussion_only" if is_discussion_only else "approved",
        "source_agent": source_agent, "target_agent": target_agent,
        "selected_conversation_id": selected_conv["id"],
        "handoff_id": handoff["id"] if handoff else None,
        "resume_from": card["next_steps"] if ready else None,
    }
    project = {
        "id": resolved_pid, "name": pname, "directories": pdirs,
        "working_directory": str(selected_conv.get("cwd") or "").strip() or None,
    }
    raw_msgs = [m for m in selected_conv.get("messages") or [] if isinstance(m, dict)]
    # Tool calls/results are not broken apart to manufacture a fitting excerpt.
    # They remain readable through the existing message-paging endpoint.
    text_messages = [
        {"id": m.get("id"), "role": m["role"], "content": m["content"],
         "timestamp": m.get("timestamp"), "source_index": index}
        for index, m in enumerate(raw_msgs)
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"]
    ]
    recorded_branch = str(selected_conv.get("git_branch") or "").strip()
    recorded_head = str(selected_conv.get("git_head") or "").strip()
    branch_display = (
        f"{recorded_branch} @ {recorded_head[:12]}" if recorded_branch and recorded_head
        else _branch_label(selected_conv, language) or local("未记录", "Not recorded")
    )
    header = [
        f"# [{source_agent}] {display_title}",
        *_session_directory_lines(local, selected_conv.get("cwd"), pdirs),
        local("- 会话分支: ", "- Session branch: ") + branch_display,
        local("- 状态: ", "- Status: ") + (
            local("尚未授权改代码 (discussion_only)", "Code edits NOT authorized (discussion_only)")
            if is_discussion_only else local("已加载，等待指令", "Loaded; wait for instruction")
            if not ready else local("执行用户本次明确指令", "Follow the user's explicit instruction")
        ),
    ]
    if not history_fresh:
        header.append(local(
            "- 未确认包含宿主最新消息；仅为已保存记录。",
            "- Native freshness unverified; stored records only.",
        ))
    selected_metadata = {
        "id": selected_conv["id"], "external_id": selected_conv.get("external_id"),
        "agent": source_agent, "title": display_title, "cwd": selected_conv.get("cwd"),
        "git_branch": selected_conv.get("git_branch"), "git_head": selected_conv.get("git_head"),
        "updated_at": conv_summary.get("updated_at"),
    }
    tool_fields_omitted = any(
        m.get("role") == "tool" or conversation_service._tool_fields(m) for m in raw_msgs
    )

    def pack(messages, clipped=None):
        ordered = sorted(messages, key=lambda m: m["source_index"])
        oldest = ordered[0]["source_index"] if ordered else len(raw_msgs)
        coverage = {
            "scope": "partial" if len(ordered) != len(raw_msgs) or clipped or tool_fields_omitted else "complete_messages",
            "total_messages": len(raw_msgs), "included_messages": len(ordered),
            "omitted_messages": len(raw_msgs) - len(ordered),
            "omitted_older_messages": oldest,
            "omitted_other_messages": len(raw_msgs) - len(ordered) - oldest,
            "clipped_message": clipped,
            "summary_clipped_fields": summary_clipped_fields,
            "process_summary_omitted": bool(selected_conv.get("process_summary")),
            "tool_fields_omitted": tool_fields_omitted,
        }
        selected = selected_metadata | {"messages": ordered, "truncated_older_messages": oldest}
        note = local(
            f"消息节选 {len(ordered)}/{len(raw_msgs)}；省略 {coverage['omitted_messages']} 条；非完整历史。",
            f"Message excerpt {len(ordered)}/{len(raw_msgs)}; {coverage['omitted_messages']} omitted; not full history.",
        )
        if clipped:
            note += local(
                f" 消息 {clipped['source_index']} 仅保留末尾，省略前缀 {clipped['omitted_prefix_bytes']} 字节。",
                f" Message {clipped['source_index']} is a tail excerpt; {clipped['omitted_prefix_bytes']} prefix bytes omitted.",
            )
        payload = {
            "ok": True, "status": "confirmed" if ready else "loaded", "selection_kind": "conversation",
            "confirmed": ready, "must_not_execute": not ready, "execution_mode": continuation["execution_mode"],
            "project": project, "conversation_id": selected_conv["id"],
            "synced_agents": synced_agents, "sync_failed": sync_failed,
            "history_fresh": history_fresh, "sync_requested": sync_first,
            "selected_conversation": selected,
            "handoff_id": handoff["id"] if handoff else None,
            "handoff": {"id": handoff["id"], "status": "confirmed" if ready else handoff["status"]} if handoff else None,
            "card": card, "readback": d005_readback(card), "continuation": continuation,
            "context": {
                "mode": "conversation_detail", "selected_conversation": selected,
                "project": {"$ref": "#/project"}, "six_elements": {"$ref": "#/card"},
            },
            "context_text": "\n".join([*header, note, *[f"- **{m['role']}**: {m['content']}" for m in ordered]]),
            "truncated_older_messages": oldest, "coverage": coverage,
        }
        return retrieval_service.bound_context_response(payload, max_tokens, language=language)

    # Protect the newest user request while fitting the latest assistant tail;
    # otherwise a huge reply could crowd out what the user actually asked.
    latest = text_messages[-1] if text_messages else None
    newest_user = next((m for m in reversed(text_messages) if m["role"] == "user"), None)
    kept = [newest_user] if newest_user and latest is not newest_user else []
    result = pack(kept)
    if not result["ok"]:
        if created_new_handoff_id:
            _cleanup_provisional_handoff(created_new_handoff_id)
            _clear_deleted_handoff_refs(result, created_new_handoff_id)
        return result  # Protected metadata/request alone cannot fit.
    if latest:
        candidate = pack([*kept, latest])
        if candidate["ok"]:
            kept.append(latest)
            result = candidate
            kept_indices = {m["source_index"] for m in kept}
            for message in reversed(text_messages[:-1]):
                if message["source_index"] in kept_indices:
                    continue
                candidate = pack([message, *kept])
                if not candidate["ok"]:
                    break
                kept.append(message)
                result = candidate
        else:
            # Fit the exact Unicode suffix against the *serialized* payload,
            # including both structured aliases and its Markdown rendering.
            text = latest["content"]
            low, high = 1, min(len(text), max_tokens)
            fitted = None
            original_bytes = len(text.encode("utf-8"))
            while low <= high:
                count = (low + high) // 2
                suffix = text[-count:]
                clipped = {
                    "source_index": latest["source_index"],
                    "original_bytes": original_bytes,
                    "included_bytes": len(suffix.encode("utf-8")),
                    "omitted_prefix_bytes": original_bytes - len(suffix.encode("utf-8")),
                }
                candidate = pack([*kept, latest | {"content": suffix}], clipped)
                if candidate["ok"]:
                    fitted = candidate
                    low = count + 1
                else:
                    high = count - 1
            if fitted is None:
                if created_new_handoff_id:
                    _cleanup_provisional_handoff(created_new_handoff_id)
                    _clear_deleted_handoff_refs(candidate, created_new_handoff_id)
                return candidate
            result = fitted
    try:
        with db() as conn:
            current = conn.execute("SELECT content_hash FROM conversations WHERE id=?", (selected_conv["id"],)).fetchone()
            if not current:
                raise HTTPException(404, "Selected conversation was deleted while loading")
            if current["content_hash"] != selected_conv.get("content_hash"):
                raise HTTPException(409, "Selected conversation changed while loading; retry")
        if handoff and ready:
            confirmed_handoff = confirm_handoff(handoff["id"], confirmed=True, actor=target_agent)
            actual = continuation_contract(confirmed_handoff, target_agent)
            if actual["must_not_execute"]:
                raise HTTPException(409, "Handoff state changed while preparing selected context; reload before executing")
    except Exception:
        if created_new_handoff_id:
            _cleanup_provisional_handoff(created_new_handoff_id)
        raise
    return result


def _resolve_relay_project_identifier(identifier: str, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve a user-supplied project ID, name, or 1-based candidate index."""
    raw = str(identifier or "").strip()
    if not raw:
        return None
    # 1. Check 1-based index into discovered candidates
    if raw.isdigit():
        idx = int(raw)
        for cand in candidates:
            if cand.get("index") == idx:
                with db() as conn:
                    row = conn.execute("SELECT * FROM projects WHERE id = ?", (cand["project_id"],)).fetchone()
                    if row:
                        p = row_to_dict(row)
                        p["directories"] = project_service._get_project_directories(conn, p["id"])
                        return p
    with db() as conn:
        # 2. Exact ID
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (raw,)).fetchone()
        if row:
            p = row_to_dict(row)
            p["directories"] = project_service._get_project_directories(conn, p["id"])
            return p
        # 3. Exact case-insensitive name
        row = conn.execute("SELECT * FROM projects WHERE lower(name) = lower(?) AND status = 'active'", (raw,)).fetchone()
        if row:
            p = row_to_dict(row)
            p["directories"] = project_service._get_project_directories(conn, p["id"])
            return p
        # 4. Unique substring match on project name
        rows = conn.execute(
            "SELECT * FROM projects WHERE lower(name) LIKE ? AND status = 'active' ORDER BY updated_at DESC",
            (f"%{raw.lower()}%",),
        ).fetchall()
        if len(rows) == 1:
            p = row_to_dict(rows[0])
            p["directories"] = project_service._get_project_directories(conn, p["id"])
            return p
    return None


def _cleanup_provisional_handoff(handoff_id: str) -> None:
    """Remove an unconfirmed draft handoff created inside a call whose payload did not fit budget."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        if not row or row["status"] != "awaiting_confirmation":
            return
        conn.execute("DELETE FROM handoffs WHERE id = ?", (handoff_id,))
        conn.execute("DELETE FROM audit_logs WHERE target = ? AND action = 'handoff.create'", (handoff_id,))


def _clear_deleted_handoff_refs(payload: Any, deleted_handoff_id: str | None) -> None:
    """Ensure a deleted provisional handoff ID is never echoed back in budget refusal payloads."""
    if not deleted_handoff_id or not isinstance(payload, dict):
        return
    if payload.get("handoff_id") == deleted_handoff_id:
        payload["handoff_id"] = None
    if isinstance(payload.get("handoff"), dict) and payload["handoff"].get("id") == deleted_handoff_id:
        payload["handoff"] = None
    cont = payload.get("continuation")
    if isinstance(cont, dict) and cont.get("handoff_id") == deleted_handoff_id:
        cont["handoff_id"] = None


def _compact_relay_continuation(continuation: dict[str, Any] | None) -> dict[str, Any] | None:
    """Deduplicate card/readback and bulky raw summary snapshots in relay_project's wire continuation."""
    if not isinstance(continuation, dict):
        return continuation
    compacted = dict(continuation)
    if "card" in compacted and isinstance(compacted["card"], dict):
        compacted["card"] = {"$ref": "#/card"}
    if "readback" in compacted and isinstance(compacted["readback"], dict):
        compacted["readback"] = {"$ref": "#/readback"}
    if isinstance(compacted.get("active_decisions"), list):
        compacted["active_decisions"] = [
            {"id": d["id"], "title": d.get("title"), "version": d.get("version", 1)}
            for d in compacted["active_decisions"]
            if isinstance(d, dict) and "id" in d
        ]
    if isinstance(compacted.get("excluded_historical_decisions"), list):
        compacted["excluded_historical_decisions"] = [
            {"id": d["id"], "title": d.get("title")}
            for d in compacted["excluded_historical_decisions"]
            if isinstance(d, dict) and "id" in d
        ]
    if isinstance(compacted.get("do_not_redo"), list):
        compacted["do_not_redo"] = [
            {"id": d["id"], "title": d.get("title")}
            for d in compacted["do_not_redo"]
            if isinstance(d, dict) and "id" in d
        ]
    raw_revs = compacted.get("source_revisions")
    if isinstance(raw_revs, dict):
        slim_revs: dict[str, Any] = {}
        for sid, rev in raw_revs.items():
            if not isinstance(rev, dict):
                slim_revs[sid] = rev
                continue
            stamp: dict[str, Any] = {}
            for key in ("type", "version", "updated_at"):
                if rev.get(key) is not None:
                    stamp[key] = rev[key]
            if rev.get("content_hash"):
                stamp["content_hash"] = str(rev["content_hash"])[:16]
            slim_revs[sid] = stamp
        compacted["source_revisions"] = slim_revs
    return compacted


def _compact_digest_item(sc: dict[str, Any]) -> dict[str, Any]:
    """Keep concise index metadata in compressed digest JSON; full goal & tool pairs are in context_text."""
    item: dict[str, Any] = {}
    for k in ("id", "agent", "title", "cwd", "updated_at", "message_count"):
        if k in sc:
            item[k] = sc[k]
    item["tool_pair_count"] = len(sc.get("tool_pairs") or [])
    if sc.get("tool_pair_omissions"):
        item["omitted_tool_pair_count"] = len(sc["tool_pair_omissions"])
    return item


def relay_project(
    project_id: str | None = None,
    *,
    consumer: str | None = None,
    context_mode: str = "compressed",
    task_id: str | None = None,
    conversation_id: str | None = None,
    query: str | None = None,
    max_tokens: int = 32000,
    sync_first: bool = True,
    language: str = 'zh',
    handoff_id: str | None = None,
) -> dict[str, Any]:
    """One-stop cross-agent project relay:
    - If project_id is omitted, syncs all supported agents' recent chats and returns
      summarized project candidates + question_prompt asking the user which project to relay.
    - Once project_id is selected, returns the project's handoff card, continuation contract,
      and either 'compressed' (short distilled context) or 'full' (complete recent conversations
      and active memories) context so the agent can seamlessly relay.
    """
    check_language(language)
    local = lambda zh, en: en if language == 'en' else zh
    if handoff_id is not None and (not isinstance(handoff_id, str) or not handoff_id.strip()):
        raise HTTPException(422, 'handoff_id must be a nonempty string')
    if type(max_tokens) is not int or not 1 <= max_tokens <= 32000:
        raise HTTPException(status_code=422, detail="Context budget must be between 1 and 32000")
    mode_raw = str(context_mode or "compressed").strip().lower()
    if mode_raw not in ("compressed", "short", "summary", "full", "complete", "raw", "压缩", "压缩上下文", "完整", "完整上下文"):
        raise HTTPException(status_code=422, detail="context_mode must be 'compressed' or 'full'")
    normalized_mode = "full" if mode_raw in ("full", "complete", "raw", "完整", "完整上下文") else "compressed"
    target_agent = str(consumer or "").strip().lower()
    if target_agent not in ALLOWED_TARGET_AGENTS:
        raise HTTPException(status_code=422, detail="An explicit supported target Agent is required")

    if (not project_id or str(project_id).strip() in ("", "?", "list", "select")) and conversation_id:
        return relay_conversation(
            conversation_id=conversation_id,
            consumer=target_agent,
            max_tokens=max_tokens,
            sync_first=sync_first if handoff_id is None else False,
            language=language,
            query=query,
        )

    discovery = discover_relay_projects(consumer=target_agent, limit=20,
                                        sync_first=sync_first if handoff_id is None else False,
                                        language=language, link_unlinked=handoff_id is None)
    if not project_id or str(project_id).strip() in ("", "?", "list", "select"):
        return discovery

    project = _resolve_relay_project_identifier(project_id, discovery.get("relay_candidates") or [])
    if not project:
        direct_conv = _resolve_relay_conversation_identifier(
            str(project_id), discovery.get("conversation_candidates") or []
        )
        if direct_conv and not str(project_id).strip().startswith("proj_"):
            return relay_conversation(
                conversation_id=direct_conv["id"],
                consumer=target_agent,
                max_tokens=max_tokens,
                sync_first=False,
                language=language,
                query=query,
            )
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    resolved_pid = project["id"]
    pname = project.get("name") or local("未命名项目", "Untitled project")
    pdirs = project.get("directories") or []
    if isinstance(pdirs, str):
        pdirs = decode(pdirs, [])
    if not pdirs:
        with db() as conn:
            pdirs = project_service._get_project_directories(conn, resolved_pid)

    with db() as conn:
        conv_rows = conn.execute(
            """
            SELECT * FROM conversations
            WHERE project_id = ?
            ORDER BY COALESCE(updated_at, created_at) DESC
            """,
            (resolved_pid,),
        ).fetchall()
        all_convs = [row_to_dict(r) for r in conv_rows]

        from app.services import governance_guardrails

        mem_rows = conn.execute(
            """
            SELECT * FROM contents
            WHERE (project_id = ? OR (project_id IS NULL AND category IN ('preference', 'lesson')))
              AND kind = 'memory' AND category != 'task' AND status = 'active'
            ORDER BY updated_at DESC
            """,
            (resolved_pid,),
        ).fetchall()
        active_memories = [
            item for r in mem_rows
            if not governance_guardrails.is_tombstoned_memory(
                conn,
                project_id=(item := row_to_dict(r)).get("project_id"),
                category=str(item.get("category") or ""),
                title=str(item.get("title") or ""),
                content=str(item.get("content") or ""),
                include_cross_category=True,
            )
        ]
        open_tasks, completed_tasks = list_project_tasks(conn, resolved_pid)

        selected_task = None
        if task_id:
            matched_open = next(
                (t for t in open_tasks if t["id"] == task_id or t.get("_working_task_id") == task_id),
                None,
            )
            if matched_open and matched_open.get("project_id") == resolved_pid:
                selected_task = matched_open
            else:
                row = conn.execute("SELECT 1 FROM contents WHERE id=?", (task_id,)).fetchone()
                wt_row = conn.execute("SELECT 1 FROM working_tasks WHERE id=?", (task_id,)).fetchone()
                if not row and not wt_row:
                    raise HTTPException(status_code=404, detail="Selected task not found")
                raise HTTPException(status_code=422, detail="Selected task must be an open task in this project")
        selected_conv = None
        if conversation_id:
            row = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Selected conversation not found")
            selected_conv = row_to_dict(row)
            if selected_conv.get("project_id") != resolved_pid:
                raise HTTPException(status_code=422, detail="Selected conversation does not belong to this project")
            if str(selected_conv.get("agent") or "").lower() == target_agent:
                raise HTTPException(status_code=422, detail="Select a conversation from a different source Agent")

        if not selected_task and not selected_conv:
            if len(open_tasks) > 1:
                return {
                    "ok": False, "status": "selection_required", "code": AMBIGUOUS_TASK_CODE,
                    "project_id": resolved_pid, "must_not_execute": True,
                    "candidates": [_task_candidate(t) for t in open_tasks],
                    "message": "Select a task before relaying; an older card does not select it for you.",
                }
            if len(open_tasks) == 1:
                selected_task = open_tasks[0]
            else:
                candidates = [row_to_dict(r) for r in conn.execute(
                    """SELECT * FROM conversations WHERE project_id=? AND lower(agent) != ?
                       ORDER BY COALESCE(updated_at,created_at) DESC,id""",
                    (resolved_pid, target_agent),
                )]
                if len(candidates) != 1:
                    return {
                        "ok": False, "status": "selection_required", "project_id": resolved_pid,
                        "must_not_execute": True,
                        "candidates": [{"id": c["id"], "title": c["title"], "agent": c["agent"]} for c in candidates],
                        "message": "Select a cross-Agent conversation; no executable handoff has been created.",
                    }
                selected_conv = candidates[0]

        required_sources = {item["id"] for item in (selected_task, selected_conv) if item}
        handoff = None
        for row in conn.execute(
            """SELECT * FROM handoffs WHERE project_id=? AND lower(target_agent)=?
               AND status IN ('confirmed','awaiting_confirmation') ORDER BY created_at DESC""",
            (resolved_pid, target_agent),
        ):
            candidate = row_to_dict(row)
            if handoff_id is not None and candidate['id'] != handoff_id:
                continue
            sources = candidate.get("source_ids") or []
            if isinstance(sources, str):
                sources = decode(sources, [])
            if not required_sources.issubset(set(sources)):
                continue
            card = candidate.get("card")
            if isinstance(card, str):
                card = decode(card, {})
            evidence = card.get("evidence") if isinstance(card, dict) else ""
            if conversation_id and isinstance(evidence, str) and (
                "关联未记录" in evidence or "link to the selected task is unrecorded" in evidence
            ):
                continue
            try:
                validate_source_revisions(conn, candidate)
            except HTTPException:
                continue
            handoff = present_handoff(candidate, conn, language)
            break

    created_new_handoff_id: str | None = None
    if not handoff:
        if handoff_id is not None:
            raise HTTPException(409, 'The selected handoff is unavailable or stale; prepare and review a new card')
        # The ordinary card builder owns provenance resolution. Do not bind the
        # latest unrelated conversation or swallow its validation errors.
        source = selected_conv or selected_task or {}
        source_agent = str(source.get("agent") or "").strip().lower()
        if selected_task and not selected_conv and source_agent in ("", "user"):
            cross_conv_agents: list[str] = []
            for c in all_convs:
                cand_agent = str(c.get("agent") or "").strip().lower()
                if cand_agent in ALLOWED_TARGET_AGENTS and cand_agent != target_agent and cand_agent not in cross_conv_agents:
                    cross_conv_agents.append(cand_agent)
            if len(cross_conv_agents) == 1:
                source_agent = cross_conv_agents[0]
            else:
                mem_agents: list[str] = []
                for m in active_memories:
                    if m.get("project_id") != resolved_pid:
                        continue
                    cand_agent = str(m.get("agent") or "").strip().lower()
                    if cand_agent in ALLOWED_TARGET_AGENTS and cand_agent != target_agent and cand_agent not in mem_agents:
                        mem_agents.append(cand_agent)
                if len(mem_agents) == 1:
                    source_agent = mem_agents[0]
                else:
                    source_agent = "user"
        if source_agent == target_agent or (source_agent not in ALLOWED_TARGET_AGENTS and source_agent != "user"):
            return {
                "ok": False, "status": "selection_required", "project_id": resolved_pid,
                "must_not_execute": True, "candidates": [],
                "message": "Select an identified predecessor from a different Agent.",
            }
        handoff = create_handoff(
            project_id=resolved_pid, target_agent=target_agent,
            task_id=selected_task["id"] if selected_task else None,
            conversation_id=selected_conv["id"] if selected_conv else None,
            query=query, source_agent=source_agent, actor=target_agent, language=language,
        )
        created_new_handoff_id = handoff["id"]

    def _abort_provisional(payload: Any = None) -> None:
        if created_new_handoff_id:
            _cleanup_provisional_handoff(created_new_handoff_id)
            _clear_deleted_handoff_refs(payload, created_new_handoff_id)

    summarized_convs = [_summarize_conversation_for_relay(c, language) for c in all_convs[:8]]
    decisions_and_prefs = [
        {
            "id": m["id"],
            "category": m["category"],
            "title": m["title"],
            "content": m["content"],
            "version": m.get("version", 1),
        }
        for m in active_memories
        if m.get("category") in ("decision", "preference")
    ]

    card = handoff.get("card") if isinstance(handoff.get("card"), dict) else decode(handoff.get("card"), {})
    continuation = continuation_contract(handoff, target_agent)
    is_discussion_only = bool(
        (selected_task and selected_task.get("execution_mode") == "discussion_only")
        or continuation.get("execution_mode") == "discussion_only"
    )
    if is_discussion_only:
        continuation["must_not_execute"] = True
        continuation["execution_mode"] = "discussion_only"
        continuation["resume_from"] = None

    from app.services import retrieval_service
    try:
        pack = retrieval_service.assemble_context(
            query=query or str(card.get("goal") or pname),
            project_id=resolved_pid,
            budget_tokens=max_tokens,
            consumer_id=target_agent,
            handoff_id=handoff["id"],
        )
    except Exception:
        _abort_provisional()
        raise

    if pack["status"] == "insufficient_budget":
        bounded_pack = retrieval_service.bound_context_response(pack, max_tokens, language=language)
        _abort_provisional(bounded_pack)
        return bounded_pack

    readback_dict = d005_readback(card)
    compressed_md_lines = [
        local(f"# 项目接力上下文（压缩模式 · {pname}）", f"# Project relay context (compressed · {pname})"),
        local("- 项目 ID: ", "- Project ID: ") + f"`{resolved_pid}`",
        "\n".join(_session_directory_lines(
            local, selected_conv.get("cwd") if selected_conv else None, pdirs,
        )),
        local("- 接力模式: 压缩短上下文 (compressed)", "- Mode: compressed context"),
        local("- 接棒卡：", "- Handoff: ") + f"{handoff['id']} · "
        + local("目标：", "target: ") + f"{target_agent} · "
        + local("状态：", "status: ") + str(continuation['status']),
        local("- 回读全部六项。在确认接棒或执行任务之前，等待用户明确确认。",
              "- Read back all six elements. Wait for explicit user confirmation before confirm_handoff or any task execution.")
        if continuation.get("must_not_execute", True)
        else local("- 仅已确认的目标助手可从记录的续接点继续，不要重复已完成的工作。",
                   "- Confirmed target only: continue from the recorded resume_from; do not repeat completed work."),
    ]
    if is_discussion_only:
        compressed_md_lines.append(
            local(
                "- 执行门禁: ⚠️ 当前任务处于「仅讨论方案 / 尚未授权改代码 (discussion_only)」阶段！禁止直接修改任何代码文件，直到用户明确授权执行。",
                "- Execution Gate: ⚠️ Current task is in DISCUSSION-ONLY state (尚未授权改代码 / discussion_only)! Do NOT edit any code files until user explicitly authorizes execution.",
            )
        )
    compressed_md_lines.extend([
        "",
        local("## 一、六要素接力摘要", "## 1. Six-point handoff summary"),
        *[f"- **{k}**: {v}" for k, v in readback_dict.items()],
        "",
        local("## 二、近期跨 Agent 对话精华摘要", "## 2. Recent cross-agent conversation digest"),
    ])
    compressed_prefix_lines = list(compressed_md_lines)
    working_directory = (str(selected_conv.get("cwd") or "").strip() or None) if selected_conv else None
    project_summary_obj = {
        "id": resolved_pid,
        "name": pname,
        "directories": pdirs,
        "working_directory": working_directory,
    }

    def _build_compressed_section(
        digest_items: list[dict[str, Any]], *, compact_items: bool
    ) -> tuple[list[str], dict[str, Any]]:
        md_lines = list(compressed_prefix_lines)
        if digest_items:
            for sc in digest_items:
                md_lines.append(f"- **[{sc['agent']}] {sc['title']}** ({str(sc['updated_at'])[:19]})")
                md_lines.append(local("  目标（未截断）: ", "  Goal (not truncated): ") + str(sc.get("user_goal") or ""))
                md_lines.append(
                    local("  工作目录: ", "  Working directory: ")
                    + (sc.get("cwd") or local("未知", "Unknown"))
                )
                for pair in sc.get("tool_pairs") or []:
                    pair_res = pair["result"] if pair.get("result") is not None else local("结果未记录", "result not recorded")
                    md_lines.append(f"  {pair.get('name') or 'tool'} {pair.get('arguments') or ''} -> {pair_res}")
                if sc.get("tool_pair_omissions"):
                    reasons = ", ".join(sorted({str(item.get("reason") or "") for item in sc["tool_pair_omissions"]}))
                    md_lines.append(local(
                        "  未配对工具记录未写成已完成结果: ",
                        "  Unpaired tool records were not written as completed results: ",
                    ) + reasons)
            hidden = len(all_convs) - len(digest_items)
            if hidden > 0:
                md_lines.append(local(
                    f"- 另有 {hidden} 段会话没有写入这段摘要。这不是把它们截短后冒充全文；需要正文时用完整模式。",
                    f"- {hidden} conversations are absent from this digest. Their text was not shortened and presented as complete; use full mode for the bodies.",
                ))
        elif all_convs:
            hidden = len(all_convs)
            md_lines.append(local(
                f"- 另有 {hidden} 段会话没有写入这段摘要。这不是把它们截短后冒充全文；需要正文时用完整模式。",
                f"- {hidden} conversations are absent from this digest. Their text was not shortened and presented as complete; use full mode for the bodies.",
            ))
        else:
            md_lines.append(local("- 暂无近期会话记录", "- No recent conversations"))

        if decisions_and_prefs:
            md_lines.append("")
            md_lines.append(local("## 三、当前生效的决策与偏好约束", "## 3. Current decisions and preferences"))
            for dp in decisions_and_prefs:
                md_lines.append(f"- [{dp['category']}] **{dp['title']}**: {dp['content']}")

        ctx_dict = {
            "mode": "compressed",
            "project": {"$ref": "#/project"},
            "six_elements": {"$ref": "#/card"},
            "resume_from": continuation.get("resume_from"),
            "recent_conversation_digest": [_compact_digest_item(sc) for sc in digest_items] if compact_items else digest_items,
            "active_decisions_and_constraints": decisions_and_prefs,
            "open_tasks": [_task_candidate(t) for t in open_tasks],
            "completed_tasks": [_task_candidate(t) for t in (completed_tasks[:10] if compact_items else completed_tasks)],
        }
        if compact_items and len(completed_tasks) > 10:
            ctx_dict["omitted_completed_tasks"] = len(completed_tasks) - 10
        return md_lines, ctx_dict

    handoff_wire = {"id": handoff["id"], "status": handoff["status"]} if handoff else None
    continuation_wire = _compact_relay_continuation(continuation)
    card_tokens = retrieval_service.estimate_tokens("\n".join(f"{k}: {v}" for k, v in readback_dict.items()))
    pinned_mems = [m for m in active_memories if "pinned" in _tags_list(m.get("tags"))]
    pinned_tokens = retrieval_service.estimate_tokens(
        "\n".join(f"{m.get('title')}: {m.get('content')}" for m in pinned_mems)
    )

    def _build_result_payload(selected_ctx: dict[str, Any], md_text: str, shown_conv_count: int) -> dict[str, Any]:
        mem_items_for_mode = full_memories if normalized_mode == "full" else decisions_and_prefs
        memory_tokens = max(
            0,
            retrieval_service.estimate_tokens(
                "\n".join(f"{m.get('title')}: {m.get('content')}" for m in mem_items_for_mode)
            ) - pinned_tokens,
        )
        total_tokens = retrieval_service.estimate_tokens(md_text)
        conversation_tokens = max(0, total_tokens - card_tokens - pinned_tokens - memory_tokens)
        token_breakdown = {
            "total_tokens": total_tokens,
            "card_tokens": card_tokens,
            "pinned_tokens": pinned_tokens,
            "memory_tokens": memory_tokens,
            "conversation_tokens": conversation_tokens,
            "pinned_count": len(pinned_mems),
            "memory_count": len(mem_items_for_mode),
            "conversation_count": shown_conv_count,
        }
        return {
            "ok": True,
            "status": "relayed",
            "must_not_execute": bool(continuation.get("must_not_execute", True)),
            "execution_mode": "discussion_only" if is_discussion_only else "approved",
            "context_mode": normalized_mode,
            "supported_context_modes": ["compressed", "full"],
            "synced_agents": discovery.get("synced_agents", []),
            "project": project_summary_obj,
            "handoff_id": handoff["id"] if handoff else None,
            "handoff": handoff_wire,
            "card": card,
            "readback": readback_dict,
            "continuation": continuation_wire,
            "context": selected_ctx,
            "context_text": md_text,
            "token_breakdown": token_breakdown,
            "pack_id": pack["id"],
            "coverage": "complete_project_snapshot" if normalized_mode == "full" else "partial",
            "omitted_conversations": 0 if normalized_mode == "full" else max(0, len(all_convs) - shown_conv_count),
        }

    # Recheck every emitted snapshot, not only the chosen handoff's sources.
    # A concurrent edit/delete must never produce a seemingly current full pack.
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            validate_source_revisions(conn, handoff)
            for memory in active_memories:
                current = retrieval_service._valid_content(conn, memory["id"], memory.get("version"))
                if current is None or current.get("content_hash") != memory.get("content_hash"):
                    raise HTTPException(409, "Context sources changed; assemble again")
            for conversation in all_convs:
                current = conn.execute("SELECT content_hash,project_id FROM conversations WHERE id=?", (conversation["id"],)).fetchone()
                if not current or current["content_hash"] != conversation.get("content_hash") or current["project_id"] != resolved_pid:
                    raise HTTPException(409, "Context sources changed; assemble again")
    except Exception:
        _abort_provisional()
        raise

    if normalized_mode == "full":
        full_conversations: list[dict[str, Any]] = []
        for c in all_convs:
            msgs = c.get("messages") or []
            if isinstance(msgs, str):
                msgs = decode(msgs, [])
            full_conversations.append(
                {
                    "id": c["id"],
                    "agent": c.get("agent"),
                    "title": c.get("title"),
                    "cwd": c.get("cwd"),
                    "updated_at": c.get("last_message_at") or c.get("updated_at") or c.get("created_at"),
                    "process_summary": c.get("process_summary") or "",
                    "messages": msgs,
                }
            )

        full_memories = [
            {
                "id": m["id"],
                "kind": m.get("kind"),
                "category": m.get("category"),
                "title": m.get("title"),
                "content": m.get("content"),
                "tags": m.get("tags") if isinstance(m.get("tags"), list) else decode(m.get("tags"), []),
                "version": m.get("version", 1),
                "updated_at": m.get("updated_at"),
            }
            for m in active_memories
        ]

        full_base_lines, _ = _build_compressed_section(summarized_convs[:5], compact_items=False)
        full_md_lines = list(full_base_lines)
        full_md_lines[0] = local(f"# 项目接力上下文（完整模式 · {pname}）", f"# Project relay context (full · {pname})")
        full_md_lines[3] = local("- 接力模式: 完整上下文 (full)", "- Mode: full context")
        full_md_lines.append("")
        full_md_lines.append(local("## 四、项目全部活跃记忆与知识条目", "## 4. All active project memories and knowledge"))
        if full_memories:
            for fm in full_memories:
                full_md_lines.append(f"### [{fm['category']}] {fm['title']} (`{fm['id']}` v{fm['version']})")
                full_md_lines.append(str(fm["content"] or ""))
        else:
            full_md_lines.append(local("- 暂无活跃记忆条目", "- No active memories"))

        full_md_lines.append("")
        full_md_lines.append(local("## 五、近期跨 Agent 完整对话记录", "## 5. Full recent cross-agent conversations"))
        if full_conversations:
            for fc in full_conversations:
                full_md_lines.append(f"### [{fc['agent']}] {fc['title']} (`{fc['id']}` · {str(fc['updated_at'] or '')[:19]})")
                if fc.get("process_summary"):
                    full_md_lines.append(local("> 会话摘要：", "> Conversation summary: ") + str(fc['process_summary']))
                for m in fc.get("messages") or []:
                    if isinstance(m, dict):
                        r_label = m.get("role") or "unknown"
                        content = m.get("content")
                        m_text = content.strip() if isinstance(content, str) else ""
                        if m_text and r_label != "tool":
                            full_md_lines.append(f"- **{r_label}**: {m_text}")
                        elif m_text and not m.get("tool_call_id"):
                            full_md_lines.append(local(
                                "- **tool**（未配对，不能当作已完成调用的结果）: ",
                                "- **tool** (unpaired; not a completed call result): ",
                            ) + m_text)
                extracted = assemble_tool_pairs(fc.get("messages"), fc.get("process_summary"))
                for pair in extracted["pairs"]:
                    pair_res = pair["result"] if pair["result"] is not None else local("结果未记录", "result not recorded")
                    full_md_lines.append(f"- {pair.get('name') or 'tool'} {pair.get('arguments') or ''} -> {pair_res}")
                if extracted["omitted"]:
                    reasons = ", ".join(sorted({str(item.get("reason") or "") for item in extracted["omitted"]}))
                    full_md_lines.append(local(
                        "- 未配对工具记录未写成已完成结果: ",
                        "- Unpaired tool records were not written as completed results: ",
                    ) + reasons)
        else:
            full_md_lines.append(local("- 暂无会话消息记录", "- No conversation messages"))

        full_context = {
            "mode": "full",
            "project": {"$ref": "#/project"},
            "six_elements": {"$ref": "#/card"},
            "resume_from": continuation.get("resume_from"),
            "recent_conversation_digest": summarized_convs,
            "conversations": full_conversations,
            "memories": full_memories,
            "open_tasks": [_task_candidate(t) for t in open_tasks],
            "completed_tasks": [_task_candidate(t) for t in completed_tasks],
        }
        bounded = retrieval_service.bound_context_response(
            _build_result_payload(full_context, "\n".join(full_md_lines), len(full_conversations)),
            max_tokens,
            language=language,
        )
    else:
        full_memories = []
        max_digests = min(5, len(summarized_convs))
        bounded = None
        for digest_limit in range(max_digests, -1, -1):
            c_md_lines, c_ctx = _build_compressed_section(summarized_convs[:digest_limit], compact_items=True)
            candidate = _build_result_payload(c_ctx, "\n".join(c_md_lines), len(summarized_convs[:digest_limit]))
            bounded = retrieval_service.bound_context_response(candidate, max_tokens, language=language)
            if bounded.get("status") != "insufficient_budget":
                break
        assert bounded is not None

    if bounded.get("status") == "insufficient_budget" or not bounded.get("ok"):
        _abort_provisional(bounded)
        return bounded

    if created_new_handoff_id:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior_drafts = conn.execute(
                """
                SELECT h.id FROM handoffs h
                WHERE h.project_id = ?
                  AND lower(h.target_agent) = ?
                  AND h.status = 'awaiting_confirmation'
                  AND h.id != ?
                  AND EXISTS (
                      SELECT 1 FROM audit_logs a
                      WHERE a.target = h.id AND a.action = 'handoff.relay_draft'
                  )
                """,
                (resolved_pid, target_agent, created_new_handoff_id),
            ).fetchall()
            for row in prior_drafts:
                conn.execute("UPDATE handoffs SET status = 'cancelled' WHERE id = ?", (row["id"],))
                log_audit(actor=target_agent, action="handoff.supersede_draft", target=row["id"], conn=conn)
            log_audit(actor=target_agent, action="handoff.relay_draft", target=created_new_handoff_id, conn=conn)

    return bounded
