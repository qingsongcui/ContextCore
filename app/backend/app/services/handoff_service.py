import os
import re
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode, decode
from app.integrations.constants import SUPPORTED_HOSTS
from app.services import project_service
from app.services.handoff_text import CardText, system, render, check_language

ALLOWED_TARGET_AGENTS = frozenset(SUPPORTED_HOSTS)
STALE_HANDOFF_DETAIL = "Source data changed or is unavailable; please regenerate this handoff card."
STALE_HANDOFF_MESSAGE = "来源已变化或不可用，请重新生成接棒卡。"
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


def task_progress_status(item: dict[str, Any] | None) -> str:
    if not item:
        return "pending"
    for tag in _tags_list(item.get("tags")):
        if tag.startswith("status:") and tag.split(":", 1)[1]:
            return tag.split(":", 1)[1]
    return "pending"


def is_open_task(item: dict[str, Any] | None) -> bool:
    if not item or item.get("status") != "active":
        return False
    if item.get("category") not in (None, "task"):
        return False
    return task_progress_status(item) != "completed"


def list_project_tasks(conn, project_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = conn.execute(
        """
        SELECT *
        FROM contents
        WHERE project_id = ? AND category = 'task' AND status = 'active'
        ORDER BY updated_at DESC
        """,
        (project_id,),
    ).fetchall()
    items = [row_to_dict(row) for row in rows]
    open_tasks = [item for item in items if is_open_task(item)]
    completed = [item for item in items if task_progress_status(item) == "completed"]
    return open_tasks, completed


def _task_candidate(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": item["id"],
        "title": str(item.get("title") or ""),
        "status": task_progress_status(item),
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
    for pair in extracted["pairs"]:
        args_str = str(pair["arguments"] or "")
        if max_piece_chars is not None and max_piece_bytes is not None:
            args_str = _cap_card_excerpt(args_str, max_chars=max_piece_chars, max_bytes=max_piece_bytes)
        prefix = f"{pair['name'] or 'tool'} {args_str} -> "
        if pair["result"] is None:
            pieces.append(CardText(prefix) + system("结果未记录", "result not recorded"))
        else:
            res_str = str(pair["result"])
            if max_piece_chars is not None and max_piece_bytes is not None:
                res_str = _cap_card_excerpt(res_str, max_chars=max_piece_chars, max_bytes=max_piece_bytes)
            pieces.append(prefix + res_str)
    if pieces:
        rendered = system("工具调用与结果（未截断）: ", "Tool calls and results (not truncated): ") + CardText("; ").join(pieces)
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
    selected_ids = {value for value in (
        project_id, (task or {}).get("id"), (conversation or {}).get("id"),
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
        relations = set(_tags_list(item.get("related_ids")))
        related_tasks = {
            related["id"] for related in conn.execute(
                """SELECT id FROM contents WHERE category='task'
                   AND id IN (SELECT value FROM json_each(?))""", (encode(sorted(relations)),),
            )
        }
        if related_tasks and (not task or task["id"] not in related_tasks):
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
        if _is_continuation_prompt(u) or "<name>contextcore-handoff</name>" in u:
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
            task_row = conn.execute("SELECT * FROM contents WHERE id = ?", (task_id,)).fetchone()
            if not task_row:
                raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
            resolved_task = row_to_dict(task_row)
            if resolved_task.get("category") != "task" or resolved_task.get("status") != "active":
                raise HTTPException(status_code=422, detail="task_id must reference an active task")
            if resolved_project_id and resolved_task.get("project_id") and resolved_task["project_id"] != resolved_project_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"Task '{task_id}' does not belong to project '{resolved_project_id}'",
                )
            if not is_open_task(resolved_task):
                raise HTTPException(status_code=422, detail="Selected task must be an open task in this project")
            resolved_project_id = resolved_task.get("project_id") or resolved_project_id
            source_ids.append(task_id)

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
                item for item in open_tasks if conn.execute(
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
            elif len(open_tasks) > 1:
                raise HTTPException(status_code=422, detail=_ambiguous_task_detail(open_tasks))
        elif not resolved_task and resolved_project_id:
            if len(open_tasks) > 1 and (not query or _is_continuation_prompt(query)):
                raise HTTPException(status_code=422, detail=_ambiguous_task_detail(open_tasks))
            if len(open_tasks) == 1 and (not query or _is_continuation_prompt(query)):
                resolved_task = open_tasks[0]
                source_ids.append(resolved_task["id"])


        # A selected task may identify its captured predecessor through existing
        # provenance. Recency alone cannot connect a parallel conversation.
        if resolved_task and not resolved_conv:
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
        has_task_sources = resolved_task and conn.execute(
            """SELECT 1 FROM content_sources WHERE content_id=?
               AND source_kind='conversation' LIMIT 1""", (resolved_task["id"],),
        ).fetchone()
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

        # 7. Build Card: Progress
        # Only the selected task is a source. Parallel open work is disclosed as
        # unselected; completed work is a do-not-redo list, not a resume target.
        progress_parts = []
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
        evidence_str = CardText(" // ").join(evidence_snippets)

        # 10. Build Card: Blockers
        blockers_list = []
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
        if query and _relay_has_task_instruction(query):
            next_steps_str = (system("目标助手 ", "Target assistant ") + clean_target
                              + system(" 执行：", " executes: ") + _cap_card_excerpt(query.strip(), max_chars=1200, max_bytes=1200))
        elif raw_next_steps:
            next_steps_str = CardText(" | ").join(raw_next_steps)
        elif resolved_task:
            next_steps_str = (system("目标助手 ", "Target assistant ") + clean_target
                              + system(" 继续任务「", " continues task “")
                              + str(resolved_task.get("title") or "") + system("」", "”"))
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
                source_revisions[sid] = {"type": "project", **_project_scene(conn, sid)}
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
        selected = [item for item in open_tasks if item["id"] in source_ids]
        payload["selected_task_id"] = selected[0]["id"] if len(selected) == 1 else None
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
    """Incrementally sync recent chat histories from all enabled supported agents before relay."""
    synced: list[dict[str, Any]] = []
    try:
        from pathlib import Path
        from app.services import connection_service, project_service

        conns = connection_service.list_connections()
        db_env = os.environ.get("CONTEXTCORE_DB", "")
        home_str = str(Path.home())
        is_isolated_db_with_real_home = bool(
            db_env
            and not db_env.startswith(home_str)
            and ("/tmp" in db_env or "/var/folders" in db_env or "pytest" in db_env or "test" in db_env.lower())
        )
        for c in conns:
            if not c.get("enabled") or c.get("paused"):
                continue
            raw_paths = c.get("paths") or []
            if is_isolated_db_with_real_home and raw_paths and all(str(p).startswith("~") for p in raw_paths):
                continue
            try:
                item = connection_service.sync_connection(
                    c["id"],
                    force=False,
                    allow_execution=(c["id"] == "codex"),
                )
                if isinstance(item, dict):
                    synced.append(
                        {
                            "agent_id": item.get("id") or c["id"],
                            "status": item.get("status"),
                            "conversations": item.get("conversations", 0),
                        }
                    )
            except Exception:
                synced.append({"agent_id": c["id"], "status": "error", "error": "History sync failed; inspect the connection status"})
        link_failures = _link_unlinked_conversations_to_projects()
        if link_failures:
            synced.append({"agent_id": None, "status": "error", "error": "Some conversations stayed unlinked after history sync"})
    except Exception:
        synced.append({"agent_id": None, "status": "error", "error": "History discovery failed"})
    return synced


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


def _summarize_conversation_for_relay(conv: dict[str, Any], language: str = 'zh') -> dict[str, Any]:
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
    generic_openers = {
        "了解一下项目",
        "了解一下项目。",
        "了解一下当前项目",
        "了解一下当前项目。",
        "了解项目",
        "继续",
        "请继续",
    }
    primary_user_msg = user_msgs[0] if user_msgs else ""
    if len(user_msgs) > 1 and primary_user_msg.strip() in generic_openers:
        for candidate_msg in user_msgs[1:]:
            if candidate_msg.strip() not in generic_openers and len(candidate_msg.strip()) > 4:
                primary_user_msg = candidate_msg
                break

    raw_title = _clean_line(str(conv.get("title") or ""), 120)
    is_placeholder_title = (
        not raw_title
        or raw_title.lower() in {"untitled session", "untitled", "new chat", "未命名会话"}
        or raw_title.lower().startswith("new session - ")
        or (raw_title in generic_openers and bool(primary_user_msg) and primary_user_msg.strip() not in generic_openers)
        or raw_title.startswith("<")
        or raw_title.startswith("# Files mentioned by the user:")
        or raw_title.startswith("# AGENTS.md instructions")
        or _is_continuation_prompt(raw_title)
    )
    if is_placeholder_title:
        raw_title = (
            _clean_line(primary_user_msg, 120)
            if primary_user_msg
            else (
                _clean_line(assistant_msgs[0], 120)
                if assistant_msgs
                else (local("Codex 子任务会话", "Codex subtask session") if ag == "codex" else local("未命名会话", "Untitled conversation"))
            )
        )
    title = raw_title or local("未命名会话", "Untitled conversation")
    process_summary = " ".join(str(conv.get("process_summary") or "").split()).strip()
    user_goal = primary_user_msg if primary_user_msg else title
    latest_user = user_msgs[-1] if user_msgs else user_goal
    latest_assistant = assistant_msgs[-1] if assistant_msgs else ""
    short_goal = _clean_line(user_goal, 120)
    goal_note = local("（摘要中的目标已缩短，以未截断字段为准）", " (the digest goal is shortened; use the untruncated field)") if short_goal != user_goal else ""
    if process_summary:
        brief = _clean_line(process_summary, 300)
        if brief != process_summary:
            brief += local("（过程摘要已缩短，以未截断字段为准）", " (process digest shortened; use the untruncated field)")
        if latest_assistant and latest_assistant[:60] not in brief:
            brief = brief + local("；最新进展：", "; latest progress: ") + _clean_line(latest_assistant, 140)
            if len(latest_assistant) > 140:
                brief += local("（进展摘录已缩短）", " (progress excerpt shortened)")
    else:
        parts = [local("目标：", "Goal: ") + short_goal + goal_note]
        if latest_assistant:
            parts.append(local("进展：", "Progress: ") + _clean_line(latest_assistant, 140))
        elif len(user_msgs) > 1:
            parts.append(local("最近提问：", "Latest question: ") + _clean_line(latest_user, 120))
        brief = local("；", "; ").join(parts)
    extracted = assemble_tool_pairs(messages, conv.get("process_summary"))

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
                LIMIT 10
                """,
                (pid,),
            ).fetchall()
            convs = [row_to_dict(r) for r in conv_rows]

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
                "SELECT COUNT(*) AS cnt FROM contents WHERE project_id = ? AND kind = 'memory' AND status = 'active'",
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
                (summarized_convs[0]["updated_at"] if summarized_convs else "")
                or (open_tasks[0]["updated_at"] if open_tasks else "")
                or proj.get("updated_at")
                or proj.get("created_at")
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


def _is_synthetic_benchmark_session(conv: dict[str, Any], summary: dict[str, Any]) -> bool:
    """Identify synthetic test/benchmark probe or automated subagent worker sessions so they do not clutter real project lists."""
    cwd_str = str(conv.get("cwd") or "")
    if "Mac全流程实测_" in cwd_str or "contextcore-synthetic-acceptance" in cwd_str:
        return True
    msg_count = int(summary.get("message_count") or 0)
    if msg_count == 0:
        return True
    goal_str = str(summary.get("user_goal") or "").strip()
    title_str = str(summary.get("title") or "").strip()
    if (
        goal_str.startswith("这是宿主超时语义实测。")
        or title_str.startswith("这是宿主超时语义实测。")
        or goal_str.startswith("Complete assignment thoroughly:")
        or title_str.startswith("Complete assignment thoroughly:")
        or goal_str.startswith("### Session update [compaction]")
        or title_str.startswith("### Session update [compaction]")
    ):
        return True
    if goal_str.lower() in {"reply with ok", "accounts", "ok", "ping"} and msg_count <= 2:
        return True
    return False


def discover_relay_conversations(
    *,
    consumer: str | None = None,
    limit: int = 15,
    sync_first: bool = True,
    language: str = 'zh',
    link_unlinked: bool = True,
    cwd: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Sync recent chats across all supported agents and return a flat numbered list of conversation records."""
    check_language(language)
    local = lambda zh, en: en if language == 'en' else zh
    synced_agents = sync_recent_agent_chats_for_relay(respect_budget=False) if sync_first else []
    if link_unlinked:
        link_failures = _link_unlinked_conversations_to_projects()
        if link_failures:
            synced_agents.append({"agent_id": None, "status": "error", "error": "Some conversations stayed unlinked after history sync"})
    consumer_norm = str(consumer or "").strip().lower()

    scoped_project_id: str | None = None
    scoped_project_name: str = ""
    scoped_cwd: str | None = None
    home_canon = project_service.canonical_project_path(str(Path.home()))

    with db() as conn:
        proj_rows = conn.execute("SELECT id, name FROM projects WHERE status = 'active'").fetchall()
        project_names = {r["id"]: (r["name"] or local("未命名项目", "Untitled project")) for r in proj_rows}

        if project_id and str(project_id).strip():
            raw_pid = str(project_id).strip()
            prow = conn.execute(
                "SELECT id, name FROM projects WHERE (id = ? OR lower(name) = lower(?)) AND status = 'active'",
                (raw_pid, raw_pid),
            ).fetchone()
            if prow:
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
        # If the newest conversation of consumer_norm is an active continuation-trigger session (/contextcore),
        # scope candidate discovery to that session's project/cwd.
        if not scoped_project_id and not scoped_cwd and consumer_norm:
            latest_consumer_rows = conn.execute(
                """
                SELECT * FROM conversations
                WHERE lower(agent) = ?
                ORDER BY COALESCE(updated_at, created_at) DESC
                LIMIT 2
                """,
                (consumer_norm,),
            ).fetchall()
            if latest_consumer_rows:
                latest_c = row_to_dict(latest_consumer_rows[0])
                latest_msgs = latest_c.get("messages") or []
                if isinstance(latest_msgs, str):
                    latest_msgs = decode(latest_msgs, [])
                if _is_continuation_only_session(latest_msgs):
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
                            project_service.canonical_project_path(r["path"])
                            for r in conn.execute("SELECT path FROM project_directories WHERE project_id = ?", (pid_val,)).fetchall()
                        ]
                        if p_dirs != [home_canon]:
                            scoped_project_id = pid_val
                            scoped_project_name = project_names.get(pid_val, "")

        def _fetch_convs(pid_filter: str | None, cwd_filter: str | None) -> list[dict[str, Any]]:
            if pid_filter and cwd_filter:
                rows = conn.execute(
                    """
                    SELECT * FROM conversations
                    WHERE project_id = ? OR cwd = ? OR cwd LIKE ?
                    ORDER BY COALESCE(updated_at, created_at) DESC
                    LIMIT 200
                    """,
                    (pid_filter, cwd_filter, cwd_filter.rstrip("/") + "/%"),
                ).fetchall()
            elif pid_filter:
                rows = conn.execute(
                    """
                    SELECT * FROM conversations
                    WHERE project_id = ?
                    ORDER BY COALESCE(updated_at, created_at) DESC
                    LIMIT 200
                    """,
                    (pid_filter,),
                ).fetchall()
            elif cwd_filter:
                rows = conn.execute(
                    """
                    SELECT * FROM conversations
                    WHERE cwd = ? OR cwd LIKE ?
                    ORDER BY COALESCE(updated_at, created_at) DESC
                    LIMIT 200
                    """,
                    (cwd_filter, cwd_filter.rstrip("/") + "/%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM conversations
                    ORDER BY COALESCE(updated_at, created_at) DESC
                    LIMIT 100
                    """
                ).fetchall()
            return [row_to_dict(r) for r in rows]

        raw_convs = _fetch_convs(scoped_project_id, scoped_cwd)
        used_project_scope = bool(scoped_project_id or scoped_cwd)

        def _build_candidates(convs_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
            built: list[dict[str, Any]] = []
            for c in convs_list:
                msgs = c.get("messages") or []
                if isinstance(msgs, str):
                    msgs = decode(msgs, [])
                ag = str(c.get("agent") or "").strip().lower()
                if _is_continuation_only_session(msgs):
                    continue
                summary = _summarize_conversation_for_relay(c, language)
                pid = c.get("project_id")
                pname = project_names.get(pid) if pid else local("未绑定目录会话", "Unlinked conversation")
                is_cross_agent = bool(consumer_norm and ag and ag != consumer_norm)
                built.append(
                    {
                        "conversation_id": c["id"],
                        "id": c["id"],
                        "agent": summary["agent"],
                        "is_cross_agent": is_cross_agent,
                        "project_id": pid,
                        "project_name": pname or local("未绑定目录会话", "Unlinked conversation"),
                        "cwd": summary.get("cwd") or "",
                        "updated_at": summary.get("updated_at") or "",
                        "title": summary.get("title") or local("未命名会话", "Untitled conversation"),
                        "brief_summary": summary.get("brief_summary") or "",
                        "user_goal": summary.get("user_goal") or "",
                        "message_count": summary.get("message_count", 0),
                        "_synthetic": _is_synthetic_benchmark_session(c, summary),
                    }
                )
            non_synthetic = [item for item in built if not item.get("_synthetic")]
            effective = non_synthetic if non_synthetic else built
            deduped: list[dict[str, Any]] = []
            seen_keys: set[tuple[str, str, str, str]] = set()
            for item in effective:
                item.pop("_synthetic", None)
                dedup_key = (
                    str(item.get("agent") or "").lower(),
                    str(item.get("project_id") or ""),
                    str(item.get("title") or "").strip().lower(),
                    str(item.get("user_goal") or "")[:120].strip().lower(),
                )
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                deduped.append(item)
            return deduped

        conv_candidates = _build_candidates(raw_convs)
        # A project that already has sessions, or an explicit project/cwd, must not
        # silently pick up other projects after continuation-only sessions are filtered.
        explicit_scope = bool(str(project_id or "").strip() or str(cwd or "").strip())
        scope_widened_reason = None
        if used_project_scope and not conv_candidates and not raw_convs and not explicit_scope:
            scope_widened_reason = local(
                "当前范围内没有对话记录，因此下面改为展示本机全部项目的近期会话。这些会话不属于刚才那个范围。",
                "No conversations were stored in the current scope, so the list below is every recent conversation on this Mac. These sessions are outside that scope.",
            )
            used_project_scope = False
            conv_candidates = _build_candidates(_fetch_convs(None, None))

    conv_candidates.sort(
        key=lambda item: (
            1 if (scoped_cwd and str(item.get("cwd") or "").rstrip("/") == scoped_cwd.rstrip("/")) else 0,
            str(item.get("updated_at") or ""),
            1 if item.get("is_cross_agent") else 0,
        ),
        reverse=True,
    )
    selected_convs = conv_candidates[: max(1, min(limit, 50))]
    for idx, item in enumerate(selected_convs, 1):
        item["index"] = idx

    scope_label = scoped_project_name or (Path(scoped_cwd).name if scoped_cwd else "")
    if selected_convs:
        if scope_widened_reason:
            header_line = scope_widened_reason
        elif used_project_scope and scope_label:
            header_line = local(
                f"以下为当前项目【{scope_label}】在各已授权 Agent 的近期对话记录（已自动过滤其他无关项目，优先排列跨 Agent 会话）：",
                f"Recent conversations for current project [{scope_label}] across authorized Agents (prioritizing cross-Agent sessions):",
            )
        else:
            header_line = local(
                "以下为本机各已授权 Agent 的近期对话记录（优先排列跨 Agent 会话，也包含同 Agent 历史会话与无目录会话）：",
                "Recent conversations across authorized Agents (prioritizing cross-Agent sessions, plus same-Agent and unlinked sessions):",
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
                + f")\n   - {item['brief_summary']}"
            )
        lines.append("")
        lines.append(
            local(
                "请直接回复要加载的「对话序号」（如 1）或 `conversation_id`；选定后将加载该条对话上下文并向你汇报进度，等待你的下一步指令。",
                "Reply with the conversation number (e.g. 1) or `conversation_id`; selecting it loads that conversation's context, reports a brief summary, and waits for your next instruction.",
            )
        )
        question_prompt = "\n".join(lines)
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

    return {
        "ok": True,
        "status": "selection_required",
        "selection_kind": "conversation",
        "consumer": consumer_norm or None,
        "scoped_project_id": scoped_project_id if used_project_scope else None,
        "scoped_project_name": scoped_project_name if used_project_scope else None,
        "scoped_cwd": scoped_cwd if used_project_scope else None,
        "scope_widened": bool(scope_widened_reason),
        "synced_agents": synced_agents,
        "sync_failed": any(item.get("status") == "error" for item in synced_agents),
        "supported_agents": list(ALLOWED_TARGET_AGENTS),
        "total": len(selected_convs),
        "total_candidates": len(selected_convs),
        "candidates": selected_convs,
        "conversation_candidates": selected_convs,
        "question_prompt": question_prompt,
    }


def _resolve_relay_conversation_identifier(
    identifier: str, candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve a user-supplied 1-based conversation index, conversation_id, external_id, or title."""
    raw = str(identifier or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        idx = int(raw)
        for cand in candidates:
            if cand.get("index") == idx:
                cid = cand.get("conversation_id") or cand.get("id")
                if cid:
                    with db() as conn:
                        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (cid,)).fetchone()
                        if row:
                            return row_to_dict(row)
    with db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (raw,)).fetchone()
        if row:
            return row_to_dict(row)
        row = conn.execute("SELECT * FROM conversations WHERE external_id = ?", (raw,)).fetchone()
        if row:
            return row_to_dict(row)
        row = conn.execute(
            "SELECT * FROM conversations WHERE lower(title) = lower(?) ORDER BY COALESCE(updated_at, created_at) DESC",
            (raw,),
        ).fetchone()
        if row:
            return row_to_dict(row)
        rows = conn.execute(
            "SELECT * FROM conversations WHERE lower(title) LIKE ? ORDER BY COALESCE(updated_at, created_at) DESC",
            (f"%{raw.lower()}%",),
        ).fetchall()
        if len(rows) == 1:
            return row_to_dict(rows[0])
    return None


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
) -> dict[str, Any]:
    """One-step conversation-first relay for slash commands (/contextcore):
    - When conversation_id is omitted, returns the numbered list of recent conversations across all agents (scoped to cwd/project_id when provided).
    - When conversation_id (or 1-based candidate index) is supplied, treats the user's selection as
      explicit confirmation, loads that selected conversation's detailed context (recent complete turns +
      six-element summary + project active memories, auto-truncating older turns if over budget), and returns
      continuation.status='ready' (must_not_execute=False) so the Agent can start working immediately.
    """
    check_language(language)
    local = lambda zh, en: en if language == 'en' else zh
    if type(max_tokens) is not int or not 1 <= max_tokens <= 32000:
        raise HTTPException(status_code=422, detail="Context budget must be between 1 and 32000")
    target_agent = str(consumer or "").strip().lower()
    if target_agent not in ALLOWED_TARGET_AGENTS:
        raise HTTPException(status_code=422, detail="An explicit supported target Agent is required")

    discovery = discover_relay_conversations(
        consumer=target_agent,
        limit=25,
        sync_first=sync_first,
        language=language,
        link_unlinked=True,
        cwd=cwd,
        project_id=project_id,
    )
    if not conversation_id or str(conversation_id).strip() in ("", "?", "list", "select"):
        return discovery

    selected_conv = _resolve_relay_conversation_identifier(
        str(conversation_id), discovery.get("conversation_candidates") or []
    )
    if not selected_conv:
        raise HTTPException(status_code=404, detail=f"Conversation '{conversation_id}' not found")

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

        if resolved_pid:
            mem_rows = conn.execute(
                """
                SELECT * FROM contents
                WHERE (project_id = ? OR (project_id IS NULL AND category IN ('preference', 'lesson')))
                  AND status = 'active'
                ORDER BY updated_at DESC
                """,
                (resolved_pid,),
            ).fetchall()
            open_tasks, completed_tasks = list_project_tasks(conn, resolved_pid)
        else:
            mem_rows = conn.execute(
                """
                SELECT * FROM contents
                WHERE project_id IS NULL AND status = 'active'
                ORDER BY updated_at DESC
                """
            ).fetchall()
            open_tasks, completed_tasks = [], []
        active_memories = [row_to_dict(r) for r in mem_rows]

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

    has_task = _relay_has_task_instruction(query)
    handoff = None
    if resolved_pid and source_agent != target_agent and source_agent in ALLOWED_TARGET_AGENTS:
        created = create_handoff(
            project_id=resolved_pid,
            target_agent=target_agent,
            conversation_id=selected_conv["id"],
            query=query,
            source_agent=source_agent,
            actor=target_agent,
            language=language,
        )
        # A selected conversation loads context. It confirms the card only when
        # the same request already contains a concrete task.
        handoff = confirm_handoff(created["id"], confirmed=True, actor=target_agent) if has_task else created

    if handoff:
        card = handoff.get("card") if isinstance(handoff.get("card"), dict) else decode(handoff.get("card"), {})
        continuation = continuation_contract(handoff, target_agent)
    else:
        decision_lines = [f"[{d['category']}] {d['title']}: {d['content']}" for d in decisions_and_prefs[:8]]
        wait_steps = local(
            "先向用户汇报对话已加载完毕，等待下一步指令后再修改代码",
            "Report that the conversation is loaded, then wait for the next instruction before changing code",
        )
        card = {
            "goal": (query if has_task else None) or conv_summary.get("user_goal") or selected_conv.get("title") or local("继续所选对话任务", "Continue selected conversation"),
            "progress": conv_summary.get("brief_summary") or local("已加载所选对话记录", "Loaded selected conversation record"),
            "decisions": "\n".join(decision_lines) if decision_lines else local("沿用当前项目与全局偏好约束", "Follow current project and global preferences"),
            "evidence": f"[{source_agent}] {display_title} ({selected_conv.get('cwd') or pname})",
            "blockers": local("无显式阻塞", "No explicit blockers") if has_task else local("尚未收到具体执行指令", "No concrete task instruction yet"),
            "next_steps": query if has_task else wait_steps,
        }
        continuation = {
            "status": "ready" if has_task else "awaiting_confirmation",
            "must_not_execute": not has_task,
            "target_agent": target_agent,
            "source_agent": source_agent,
            "selected_conversation_id": selected_conv["id"],
            "selected_task_id": None,
            "resume_from": card["next_steps"] if has_task else None,
            "do_not_redo": [t.get("title") for t in completed_tasks if t.get("title")],
            "parallel_open_tasks": [_task_candidate(t) for t in open_tasks],
            "excluded_historical_decisions": [],
        }
    if not has_task:
        continuation["must_not_execute"] = True
        continuation["resume_from"] = None
        if continuation.get("status") in ("ready", "awaiting_instruction"):
            continuation["status"] = "awaiting_confirmation"

    readback_dict = d005_readback(card)
    raw_msgs = selected_conv.get("messages") or []
    if isinstance(raw_msgs, str):
        raw_msgs = decode(raw_msgs, [])
    dict_msgs = [m for m in raw_msgs if isinstance(m, dict)]

    from app.services import retrieval_service
    waiting = bool(continuation.get("must_not_execute"))
    header_lines = [
        local(
            f"# 对话接力上下文（{'已加载，等待指令' if waiting else '已确认直连'} · [{source_agent}] {display_title}）",
            f"# Conversation relay context ({'Loaded, waiting for instruction' if waiting else 'Confirmed'} · [{source_agent}] {display_title})",
        ),
        local("- 对话 ID: ", "- Conversation ID: ") + f"`{selected_conv['id']}`",
        local("- 来源 Agent: ", "- Source Agent: ") + f"`{source_agent}` -> `{target_agent}`",
        local("- 所属项目: ", "- Project: ") + f"{pname}" + (f" (`{resolved_pid}`)" if resolved_pid else ""),
        local("- 工作目录: ", "- Working directories: ") + (", ".join(pdirs) if pdirs else local("未绑定目录", "None registered")),
        local("- 状态: 对话上下文已加载完毕。请先向用户告知「✅ 对话上下文已加载完毕」，简要汇报该对话的【此前目标】与【最新进展】（2～3行即可），然后停下来等待用户的下一步指令；除非用户在选择序号的同时已经明确给出了具体执行任务，否则切勿擅自开始干活或修改文件。",
              "- Status: Conversation context loaded. First report '✅ Conversation context loaded' to the user with a brief 2-3 line summary of the goal and latest progress, then wait for the user's next instruction. Do NOT start executing tasks or modifying files unless the user explicitly included a task instruction alongside their selection."),
        "",
        local("## 一、六要素接力摘要", "## 1. Six-point handoff summary"),
        *[f"- **{k}**: {v}" for k, v in readback_dict.items()],
    ]
    if decisions_and_prefs:
        header_lines.append("")
        header_lines.append(local("## 二、当前生效的决策与偏好约束", "## 2. Active decisions and preferences"))
        for dp in decisions_and_prefs[:12]:
            header_lines.append(f"- [{dp['category']}] **{dp['title']}**: {dp['content']}")

    header_lines.append("")
    header_lines.append(local("## 三、选中对话的详细消息记录", "## 3. Selected conversation transcript"))
    if selected_conv.get("process_summary"):
        header_lines.append(local("> 会话过程摘要：", "> Session summary: ") + str(selected_conv["process_summary"]))

    formatted_msg_lines: list[str] = []
    for m in dict_msgs:
        r_label = m.get("role") or "unknown"
        content = m.get("content")
        m_text = content.strip() if isinstance(content, str) else ""
        if m_text and r_label != "tool":
            formatted_msg_lines.append(f"- **{r_label}**: {m_text}")

    extracted_tools = assemble_tool_pairs(dict_msgs, selected_conv.get("process_summary"))
    tool_lines: list[str] = []
    for pair in extracted_tools["pairs"][-10:]:
        result_str = pair["result"] if pair.get("result") is not None else local("结果未记录", "result not recorded")
        tool_lines.append(f"- `{pair.get('name') or 'tool'}` {pair.get('arguments') or ''} -> {result_str}")

    # Budget-aware reverse slicing of message turns so we never fail with insufficient_budget on long chats
    base_md = "\n".join(header_lines + tool_lines)
    base_tokens = retrieval_service.estimate_tokens(base_md)
    msg_budget = max(200, int(max_tokens * 0.65) - base_tokens)

    kept_msg_lines: list[str] = []
    used_msg_tokens = 0
    for line in reversed(formatted_msg_lines):
        line_tokens = retrieval_service.estimate_tokens(line) + 2
        if kept_msg_lines and used_msg_tokens + line_tokens > msg_budget:
            break
        if not kept_msg_lines and line_tokens > msg_budget:
            # Keep at least a truncated tail of the latest turn
            max_chars = max(120, msg_budget * 2)
            kept_msg_lines.append(line[:max_chars] + " ...")
            used_msg_tokens += retrieval_service.estimate_tokens(kept_msg_lines[-1])
            break
        kept_msg_lines.append(line)
        used_msg_tokens += line_tokens
    kept_msg_lines.reverse()

    truncated_count = max(0, len(formatted_msg_lines) - len(kept_msg_lines))
    final_md_lines = list(header_lines)
    if truncated_count > 0:
        final_md_lines.append(
            local(
                f"> 注：原对话较长，已自动截取最新 {len(kept_msg_lines)} 条关键消息轮次（省略较早的 {truncated_count} 条消息）。",
                f"> Note: Conversation is long; automatically kept the newest {len(kept_msg_lines)} message turns ({truncated_count} older messages omitted).",
            )
        )
    if kept_msg_lines:
        final_md_lines.extend(kept_msg_lines)
    else:
        final_md_lines.append(local("- 暂无会话消息正文", "- No message body"))
    if tool_lines:
        final_md_lines.append("")
        final_md_lines.append(local("### 近期已验证工具调用", "### Recent verified tool calls"))
        final_md_lines.extend(tool_lines)

    markdown_text = "\n".join(final_md_lines)
    kept_turn_count = len(kept_msg_lines)
    kept_messages = dict_msgs[-kept_turn_count:] if kept_turn_count > 0 else []

    context_obj = {
        "mode": "conversation_detail",
        "project": {"id": resolved_pid, "name": pname, "directories": pdirs},
        "selected_conversation": {
            "id": selected_conv["id"],
            "agent": source_agent,
            "title": display_title,
            "cwd": selected_conv.get("cwd"),
            "updated_at": conv_summary.get("updated_at"),
            "process_summary": selected_conv.get("process_summary") or "",
            "messages": kept_messages,
            "truncated_older_messages": truncated_count,
        },
        "six_elements": card,
        "resume_from": continuation.get("resume_from"),
        "active_decisions_and_constraints": decisions_and_prefs[:12],
        "open_tasks": [_task_candidate(t) for t in open_tasks[:10]],
    }

    return {
        "ok": True,
        "status": "loaded" if waiting else "confirmed",
        "selection_kind": "conversation",
        "confirmed": not waiting,
        "must_not_execute": waiting,
        "project": {"id": resolved_pid, "name": pname, "directories": pdirs},
        "conversation_id": selected_conv["id"],
        "selected_conversation": context_obj["selected_conversation"],
        "handoff_id": handoff["id"] if handoff else None,
        "handoff": handoff,
        "card": card,
        "readback": readback_dict,
        "continuation": continuation,
        "context": context_obj,
        "context_text": markdown_text,
        "truncated_older_messages": truncated_count,
    }


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


def relay_project(
    project_id: str | None = None,
    *,
    consumer: str | None = None,
    context_mode: str = "compressed",
    task_id: str | None = None,
    conversation_id: str | None = None,
    query: str | None = None,
    max_tokens: int = 4000,
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

        mem_rows = conn.execute(
            """
            SELECT * FROM contents
            WHERE (project_id = ? OR (project_id IS NULL AND category IN ('preference', 'lesson')))
              AND status = 'active'
            ORDER BY updated_at DESC
            """,
            (resolved_pid,),
        ).fetchall()
        active_memories = [row_to_dict(r) for r in mem_rows]
        open_tasks, completed_tasks = list_project_tasks(conn, resolved_pid)

        selected_task = None
        if task_id:
            row = conn.execute("SELECT * FROM contents WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Selected task not found")
            selected_task = row_to_dict(row)
            if selected_task.get("project_id") != resolved_pid or task_id not in {t["id"] for t in open_tasks}:
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
                (resolved_pid, target_agent, handoff["id"]),
            ).fetchall()
            for row in prior_drafts:
                conn.execute("UPDATE handoffs SET status = 'cancelled' WHERE id = ?", (row["id"],))
                log_audit(actor=target_agent, action="handoff.supersede_draft", target=row["id"], conn=conn)
            log_audit(actor=target_agent, action="handoff.relay_draft", target=handoff["id"], conn=conn)

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

    from app.services import retrieval_service
    pack = retrieval_service.assemble_context(
        query=query or str(card.get("goal") or pname),
        project_id=resolved_pid,
        budget_tokens=max_tokens,
        consumer_id=target_agent,
        handoff_id=handoff["id"],
    )

    if pack["status"] == "insufficient_budget":
        return retrieval_service.bound_context_response(pack, max_tokens, language=language)

    readback_dict = d005_readback(card)
    compressed_md_lines = [
        local(f"# 项目接力上下文（压缩模式 · {pname}）", f"# Project relay context (compressed · {pname})"),
        local("- 项目 ID: ", "- Project ID: ") + f"`{resolved_pid}`",
        local("- 工作目录: ", "- Working directories: ") + (', '.join(pdirs) if pdirs else local("未绑定目录", "None registered")),
        local("- 接力模式: 压缩短上下文 (compressed)", "- Mode: compressed context"),
        local("- 接棒卡：", "- Handoff: ") + f"{handoff['id']} · "
        + local("目标：", "target: ") + f"{target_agent} · "
        + local("状态：", "status: ") + str(continuation['status']),
        local("- 回读全部六项。在确认接棒或执行任务之前，等待用户明确确认。",
              "- Read back all six elements. Wait for explicit user confirmation before confirm_handoff or any task execution.")
        if continuation.get("must_not_execute", True)
        else local("- 仅已确认的目标助手可从记录的续接点继续，不要重复已完成的工作。",
                   "- Confirmed target only: continue from the recorded resume_from; do not repeat completed work."),
        "",
        local("## 一、六要素接力摘要", "## 1. Six-point handoff summary"),
        *[f"- **{k}**: {v}" for k, v in readback_dict.items()],
        "",
        local("## 二、近期跨 Agent 对话精华摘要", "## 2. Recent cross-agent conversation digest"),
    ]
    listed_digests = summarized_convs[:5]
    if listed_digests:
        for sc in listed_digests:
            compressed_md_lines.append(
                f"- **[{sc['agent']}] {sc['title']}** ({str(sc['updated_at'])[:19]})"
            )
            compressed_md_lines.append(local("  目标（未截断）: ", "  Goal (not truncated): ") + str(sc.get("user_goal") or ""))
            compressed_md_lines.append(
                local("  工作目录: ", "  Working directory: ")
                + (sc.get("cwd") or local("未知", "Unknown"))
            )
            for pair in sc.get("tool_pairs") or []:
                result = pair["result"] if pair.get("result") is not None else local("结果未记录", "result not recorded")
                compressed_md_lines.append(f"  {pair.get('name') or 'tool'} {pair.get('arguments') or ''} -> {result}")
            if sc.get("tool_pair_omissions"):
                reasons = ", ".join(sorted({str(item.get("reason") or "") for item in sc["tool_pair_omissions"]}))
                compressed_md_lines.append(local(
                    "  未配对工具记录未写成已完成结果: ",
                    "  Unpaired tool records were not written as completed results: ",
                ) + reasons)
        hidden = len(all_convs) - len(listed_digests)
        if hidden > 0:
            compressed_md_lines.append(local(
                f"- 另有 {hidden} 段会话没有写入这段摘要。这不是把它们截短后冒充全文；需要正文时用完整模式。",
                f"- {hidden} conversations are absent from this digest. Their text was not shortened and presented as complete; use full mode for the bodies.",
            ))
    else:
        compressed_md_lines.append(local("- 暂无近期会话记录", "- No recent conversations"))

    if decisions_and_prefs:
        compressed_md_lines.append("")
        compressed_md_lines.append(local("## 三、当前生效的决策与偏好约束", "## 3. Current decisions and preferences"))
        for dp in decisions_and_prefs:
            compressed_md_lines.append(f"- [{dp['category']}] **{dp['title']}**: {dp['content']}")

    compressed_context = {
        "mode": "compressed",
        "project": {"id": resolved_pid, "name": pname, "directories": pdirs},
        "six_elements": card,
        "resume_from": continuation.get("resume_from"),
        "recent_conversation_digest": listed_digests,
        "active_decisions_and_constraints": decisions_and_prefs,
        "open_tasks": [_task_candidate(t) for t in open_tasks],
        "completed_tasks": [_task_candidate(t) for t in completed_tasks],
        "markdown": "\n".join(compressed_md_lines),
    }

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

    full_md_lines = list(compressed_md_lines)
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
                result = pair["result"] if pair["result"] is not None else local("结果未记录", "result not recorded")
                full_md_lines.append(f"- {pair.get('name') or 'tool'} {pair.get('arguments') or ''} -> {result}")
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
        "project": {"id": resolved_pid, "name": pname, "directories": pdirs},
        "six_elements": card,
        "resume_from": continuation.get("resume_from"),
        "recent_conversation_digest": summarized_convs,
        "conversations": full_conversations,
        "memories": full_memories,
        "open_tasks": [_task_candidate(t) for t in open_tasks],
        "completed_tasks": [_task_candidate(t) for t in completed_tasks],
        "markdown": "\n".join(full_md_lines),
    }

    selected_context = full_context if normalized_mode == "full" else compressed_context
    card_tokens = retrieval_service.estimate_tokens("\n".join(f"{k}: {v}" for k, v in readback_dict.items()))
    pinned_mems = [m for m in active_memories if "pinned" in _tags_list(m.get("tags"))]
    pinned_tokens = retrieval_service.estimate_tokens(
        "\n".join(f"{m.get('title')}: {m.get('content')}" for m in pinned_mems)
    )
    mem_items_for_mode = full_memories if normalized_mode == "full" else decisions_and_prefs
    memory_tokens = max(
        0,
        retrieval_service.estimate_tokens(
            "\n".join(f"{m.get('title')}: {m.get('content')}" for m in mem_items_for_mode)
        ) - pinned_tokens,
    )
    total_tokens = retrieval_service.estimate_tokens(selected_context["markdown"])
    conversation_tokens = max(0, total_tokens - card_tokens - pinned_tokens - memory_tokens)
    token_breakdown = {
        "total_tokens": total_tokens,
        "card_tokens": card_tokens,
        "pinned_tokens": pinned_tokens,
        "memory_tokens": memory_tokens,
        "conversation_tokens": conversation_tokens,
        "pinned_count": len(pinned_mems),
        "memory_count": len(mem_items_for_mode),
        "conversation_count": len(full_conversations if normalized_mode == "full" else summarized_convs[:5]),
    }

    result = {
        "ok": True,
        "status": "relayed",
        "context_mode": normalized_mode,
        "supported_context_modes": ["compressed", "full"],
        "synced_agents": discovery.get("synced_agents", []),
        "project": {"id": resolved_pid, "name": pname, "directories": pdirs},
        "handoff_id": handoff["id"] if handoff else None,
        "handoff": handoff,
        "card": card,
        "readback": readback_dict,
        "continuation": continuation,
        "context": selected_context,
        "context_text": selected_context["markdown"],
        "token_breakdown": token_breakdown,
        "pack_id": pack["id"],
        "coverage": "complete_project_snapshot" if normalized_mode == "full" else "partial",
        "omitted_conversations": 0 if normalized_mode == "full" else max(0, len(all_convs) - 5),
    }
    # Recheck every emitted snapshot, not only the chosen handoff's sources.
    # A concurrent edit/delete must never produce a seemingly current full pack.
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
    selected_context.pop("markdown", None)
    return retrieval_service.bound_context_response(result, max_tokens, language=language)
