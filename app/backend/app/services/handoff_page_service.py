"""Write-on-select five-field conversation handoff pages.

Pages are produced when a conversation is selected for relay — never during
list discovery, and never by rewriting conversations.messages/content_hash.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any

from app.database import db, decode, encode, get_app_setting, log_audit, new_id, now_iso, row_to_dict

HANDOFF_PAGE_FIELDS = ("goal", "done", "do_not_redo", "unverified", "next_step")
FIELD_MAX_CHARS = 600
HANDOFF_PAGE_TIMEOUT = 60.0
# Hard ceiling for model stdin message text; trim older assistant prose first,
# then oversized tool/historical turns while keeping the latest request/reply tail.
MAX_INPUT_CHARS = 120_000
_OVERSIZED_ENTRY_HEAD_TAIL = 4000

_PAGE_LOCKS_GUARD = threading.Lock()
_PAGE_LOCKS: dict[str, threading.Lock] = {}


def _lock_for(conversation_id: str) -> threading.Lock:
    with _PAGE_LOCKS_GUARD:
        lock = _PAGE_LOCKS.get(conversation_id)
        if lock is None:
            lock = threading.Lock()
            _PAGE_LOCKS[conversation_id] = lock
        return lock


def get_handoff_page(conversation_id: str) -> dict[str, Any] | None:
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM conversation_handoff_pages WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            item = row_to_dict(row)
            if isinstance(item.get("facts_json"), str):
                item["facts_json"] = decode(item["facts_json"], {})
            return item
    except sqlite3.OperationalError:
        return None


def get_handoff_page_for_detail(conversation_id: str) -> dict[str, Any] | None:
    """Read-only lookup for conversation detail UI, computing staleness against content_hash."""
    try:
        with db() as conn:
            row = conn.execute(
                """
                SELECT p.*,
                       (c.content_hash IS NOT NULL AND p.source_content_hash != c.content_hash) AS computed_stale
                FROM conversation_handoff_pages p
                JOIN conversations c ON c.id = p.conversation_id
                WHERE p.conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if not row:
                return None
            item = row_to_dict(row)
            if isinstance(item.get("facts_json"), str):
                item["facts_json"] = decode(item["facts_json"], {})
            item["stale"] = bool(item.pop("computed_stale", False))
            return item
    except sqlite3.OperationalError:
        return None


def build_facts(conversation: dict[str, Any]) -> dict[str, Any]:
    from app.services.handoff_service import assemble_tool_pairs

    messages = conversation.get("messages") or []
    if isinstance(messages, str):
        messages = decode(messages, [])
    pairs = assemble_tool_pairs(messages, conversation.get("process_summary"))
    user_quotes: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "").lower() != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            user_quotes.append(" ".join(content.split())[:240])
    files: list[str] = []
    commands: list[dict[str, str]] = []
    for pair in pairs.get("pairs") or []:
        name = str(pair.get("name") or "")
        arguments = str(pair.get("arguments") or "")
        result = "" if pair.get("result") is None else str(pair.get("result"))
        if name:
            commands.append({
                "name": name,
                "arguments": arguments[:400],
                "result": result[:400],
            })
        for token in arguments.replace(",", " ").split():
            path = cleaned_path(token)
            if path and path not in files:
                files.append(path)
        for token in result.replace(",", " ").split():
            path = cleaned_path(token)
            if path and path not in files:
                files.append(path)
    return {
        "user_quotes": user_quotes[-12:],
        "files": files[:80],
        "commands": commands[-40:],
        "git_branch": conversation.get("git_branch") or "",
        "git_head": conversation.get("git_head") or "",
        "message_count": len(messages),
        "title": conversation.get("title") or "",
        "agent": conversation.get("agent") or "",
    }


def cleaned_path(token: str) -> str | None:
    cleaned = token.strip("'\"`()[]{}.,;")
    if not cleaned or cleaned.startswith("http"):
        return None
    if "/" in cleaned or cleaned.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".json")):
        return cleaned[:240]
    return None


def _empty_fields() -> dict[str, str]:
    return {field: "" for field in HANDOFF_PAGE_FIELDS}


def _validate_fields(fields: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    out: dict[str, str] = {}
    for field in HANDOFF_PAGE_FIELDS:
        value = str(fields.get(field) or "")
        if len(value) > FIELD_MAX_CHARS:
            return None, f"Field '{field}' exceeds {FIELD_MAX_CHARS} characters"
        out[field] = value
    return out, None


def _save_page(
    conversation_id: str,
    *,
    source_content_hash: str,
    covered_message_count: int,
    status: str,
    fields: dict[str, str],
    facts: dict[str, Any],
    error: str | None,
) -> dict[str, Any]:
    now = now_iso()
    fallback = {
        "conversation_id": conversation_id,
        "source_content_hash": source_content_hash,
        "covered_message_count": covered_message_count,
        "status": status,
        "goal": fields.get("goal", ""),
        "done": fields.get("done", ""),
        "do_not_redo": fields.get("do_not_redo", ""),
        "unverified": fields.get("unverified", ""),
        "next_step": fields.get("next_step", ""),
        "facts_json": facts,
        "error": error,
        "updated_at": now,
    }
    try:
        with db() as conn:
            conn.execute(
                """
                INSERT INTO conversation_handoff_pages (
                    conversation_id, source_content_hash, covered_message_count, status,
                    goal, done, do_not_redo, unverified, next_step, facts_json, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    source_content_hash=excluded.source_content_hash,
                    covered_message_count=excluded.covered_message_count,
                    status=excluded.status,
                    goal=excluded.goal,
                    done=excluded.done,
                    do_not_redo=excluded.do_not_redo,
                    unverified=excluded.unverified,
                    next_step=excluded.next_step,
                    facts_json=excluded.facts_json,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    conversation_id,
                    source_content_hash,
                    covered_message_count,
                    status,
                    fields.get("goal", ""),
                    fields.get("done", ""),
                    fields.get("do_not_redo", ""),
                    fields.get("unverified", ""),
                    fields.get("next_step", ""),
                    encode(facts),
                    error,
                    now,
                ),
            )
    except sqlite3.OperationalError:
        return fallback
    return get_handoff_page(conversation_id) or fallback


def _compute_access(conn: sqlite3.Connection, conversation: dict[str, Any]) -> str | None:
    from app.services.conversation_service import compute_source_identity_key

    agent = str(conversation.get("agent") or "").strip().lower()
    if agent:
        source_key = compute_source_identity_key(
            agent,
            conversation.get("source_path"),
            str(conversation.get("title") or ""),
            conversation.get("external_id"),
        )
        if conn.execute(
            "SELECT 1 FROM tombstones WHERE entity_type='conversation' AND dedupe_key=?",
            (source_key,),
        ).fetchone():
            return "Conversation source was deleted"
        source = conn.execute(
            "SELECT enabled, paused FROM connections WHERE id=?", (agent,)
        ).fetchone()
        if source and source["paused"]:
            return f"Source host '{agent}' connection is paused"
        if conversation.get("source_path") and source and not source["enabled"]:
            return f"Source host '{agent}' connection is disabled"

    authorized = bool(get_app_setting("compute_authorized", False, conn=conn))
    host = get_app_setting("compute_host", None, conn=conn)
    compute = conn.execute(
        "SELECT enabled, paused FROM connections WHERE id=?", (host,)
    ).fetchone() if host else None
    if not authorized:
        return "Compute execution not authorized; handoff page pending explicit user grant"
    if host != "omp":
        return f"Unsupported compute host '{host}'; only 'omp' is supported"
    if not compute or not compute["enabled"] or compute["paused"]:
        return f"Compute host '{host}' connection is not enabled or is paused"
    return None


def _middle_elide(text: str, keep_each_side: int) -> str:
    if len(text) <= keep_each_side * 2 + 32:
        return text
    return f"{text[:keep_each_side]}\n...[middle omitted]...\n{text[-keep_each_side:]}"


def _prepare_messages_for_model(
    messages: list[dict[str, Any]],
    *,
    start_index: int = 0,
) -> tuple[list[dict[str, Any]], str]:
    sliced = list(enumerate(messages))[start_index:]
    prepared: list[dict[str, Any]] = []
    for index, message in sliced:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        content = message.get("content")
        text = content if isinstance(content, str) else encode(content) if content is not None else ""
        prepared.append({"index": index, "role": role, "content": text})

    total = sum(len(entry["content"]) for entry in prepared)
    if total <= MAX_INPUT_CHARS:
        return prepared, ""

    notes: list[str] = []

    # Pass 1: trim oldest assistant/system prose while preserving user and tool evidence.
    overflow = total - MAX_INPUT_CHARS
    trimmed_assistant: list[int] = []
    for entry in prepared:
        if overflow <= 0:
            break
        if entry["role"] not in ("assistant", "system") or not entry["content"]:
            continue
        text = entry["content"]
        if len(text) <= overflow:
            overflow -= len(text)
            entry["content"] = ""
            trimmed_assistant.append(entry["index"])
        else:
            entry["content"] = text[overflow:]
            trimmed_assistant.append(entry["index"])
            overflow = 0
    if trimmed_assistant:
        notes.append(
            "Shortened assistant prose for turns "
            + ", ".join(str(i) for i in trimmed_assistant[:15])
        )

    # Pass 2: bound oversized tool outputs or historical user pastes if still over limit.
    if overflow > 0 and prepared:
        shortened_other: list[int] = []
        last_pos = len(prepared) - 1
        for pos, entry in enumerate(prepared):
            if overflow <= 0:
                break
            if pos == last_pos:
                continue
            text = entry["content"]
            if len(text) > _OVERSIZED_ENTRY_HEAD_TAIL * 2 + 64:
                compact = _middle_elide(text, _OVERSIZED_ENTRY_HEAD_TAIL)
                saved = len(text) - len(compact)
                if saved > 0:
                    entry["content"] = compact
                    overflow -= saved
                    shortened_other.append(entry["index"])

        # If still over limit, trim remaining older turns from front, protecting final turn tail.
        dropped: list[int] = []
        for pos, entry in enumerate(prepared):
            if overflow <= 0:
                break
            if pos == last_pos or not entry["content"]:
                continue
            text = entry["content"]
            if len(text) <= overflow:
                overflow -= len(text)
                entry["content"] = ""
                dropped.append(entry["index"])
            else:
                entry["content"] = text[overflow:]
                dropped.append(entry["index"])
                overflow = 0

        if overflow > 0 and prepared[last_pos]["content"]:
            tail_text = prepared[last_pos]["content"]
            keep = max(0, len(tail_text) - overflow)
            prepared[last_pos]["content"] = tail_text[-keep:] if keep else ""
            shortened_other.append(prepared[last_pos]["index"])

        if shortened_other:
            notes.append("Shortened long turns " + ", ".join(str(i) for i in shortened_other[:15]))
        if dropped:
            notes.append("Omitted older turns " + ", ".join(str(i) for i in dropped[:15]))

    return prepared, "; ".join(notes) + ("." if notes else "")


def _record_handoff_page_usage_and_audit(
    *,
    job_id: str,
    conversation_id: str,
    started_at: str,
    result_status: str,
    usage: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    from app.security import authenticated_agent
    from app.services import usage_service

    state = "succeeded" if result_status == "success" else "failed"
    try:
        usage_service.ingest([
            {
                "origin": "contextcore_compute",
                "observation_id": f"{job_id}:terminal",
                "logical_request_id": job_id,
                "attempt_id": job_id,
                "host_id": "omp",
                "started_at": started_at,
                "ended_at": now_iso(),
                "status": state,
                "protocol": "openai",
                "raw_usage": usage or {},
                "input_includes_cache": False,
                "coverage": "safe_cli_handoff_page",
                "confidence": "reported" if usage else "unknown",
            }
        ])
    except Exception:
        pass
    try:
        tokens = usage.get("tokens") if isinstance(usage, dict) else None
        now = now_iso()
        with db() as conn:
            conn.execute(
                "INSERT INTO usage_meters(id,host_id,tokens,requests,note,created_at) VALUES(?,?,?,1,?,?)",
                (new_id("use"), "omp", tokens, f"Handoff page {result_status} for conversation {conversation_id}", now),
            )
            log_audit(
                actor=authenticated_agent.get() or "omp",
                action="handoff_page.write",
                target=conversation_id,
                result=result_status if not error else f"{result_status}: {str(error)[:120]}",
                conn=conn,
            )
    except Exception:
        pass


def ensure_handoff_page(conversation: dict[str, Any]) -> dict[str, Any]:
    """Return a ready page for this conversation content, writing on miss/stale.

    Never mutates conversations.messages or content_hash.
    Never writes persistent error rows when compute is disabled or unavailable.
    """
    conversation_id = conversation["id"]
    with _lock_for(conversation_id):
        content_hash = str(conversation.get("content_hash") or "")
        messages = conversation.get("messages") or []
        if isinstance(messages, str):
            messages = decode(messages, [])
        message_count = len(messages)
        facts = build_facts(conversation)
        existing = get_handoff_page(conversation_id)

        if (
            existing
            and existing.get("status") == "ready"
            and existing.get("source_content_hash") == content_hash
        ):
            return {
                **existing,
                "fresh": True,
                "reused": True,
                "stale": False,
                "write_attempted": False,
            }

        try:
            with db() as conn:
                access_error = _compute_access(conn, conversation)
        except sqlite3.OperationalError:
            access_error = "Database busy; skipping handoff page compute"

        if not access_error and message_count == 0 and not str(conversation.get("process_summary") or "").strip():
            access_error = "Conversation has no messages to summarize"

        if access_error:
            if existing and existing.get("status") == "ready":
                return {
                    **existing,
                    "fresh": False,
                    "reused": True,
                    "write_attempted": False,
                    "write_error": access_error,
                    "stale": existing.get("source_content_hash") != content_hash,
                }
            # Do not persist error rows or hit SQLite when compute is simply disabled/unavailable.
            return {
                **_empty_fields(),
                "conversation_id": conversation_id,
                "source_content_hash": content_hash,
                "covered_message_count": 0,
                "status": "unavailable",
                "facts_json": facts,
                "error": access_error,
                "updated_at": None,
                "fresh": False,
                "reused": False,
                "stale": False,
                "write_attempted": False,
                "write_error": access_error,
            }

        previous_page = None
        start_index = 0
        if existing and existing.get("status") == "ready" and existing.get("source_content_hash") != content_hash:
            prev_covered = int(existing.get("covered_message_count") or 0)
            if 0 < prev_covered < message_count:
                previous_page = {field: existing.get(field, "") for field in HANDOFF_PAGE_FIELDS}
                start_index = prev_covered
            elif prev_covered == message_count and message_count > 0:
                previous_page = {field: existing.get(field, "") for field in HANDOFF_PAGE_FIELDS}
                start_index = max(0, message_count - 6)

        prepared, coverage_note = _prepare_messages_for_model(messages, start_index=start_index)
        if coverage_note:
            facts = {**facts, "coverage_note": coverage_note}
        payload = {
            "conversation_id": conversation_id,
            "project_id": conversation.get("project_id"),
            "title": conversation.get("title"),
            "git_branch": conversation.get("git_branch"),
            "git_head": conversation.get("git_head"),
            "process_summary": conversation.get("process_summary") or "",
            "facts_json": facts,
            "previous_page": previous_page,
            "messages": prepared,
            "coverage_note": coverage_note,
        }

        from app import transport

        started_at = now_iso()
        job_id = new_id("hpage")
        try:
            result = transport.run_distill_sync(
                "omp",
                payload,
                allow_execution=True,
                timeout=HANDOFF_PAGE_TIMEOUT,
                parse_mode="handoff_page",
            )
        except (transport.DistillExecutionError, transport.TransportError, TimeoutError, OSError) as exc:
            result = transport.DistillResult(
                status="failed",
                error=str(exc) or type(exc).__name__,
            )

        _record_handoff_page_usage_and_audit(
            job_id=job_id,
            conversation_id=conversation_id,
            started_at=started_at,
            result_status=result.status,
            usage=result.usage,
            error=result.error,
        )

        if result.status != "success" or not isinstance(result.structured, dict):
            error = result.error or f"Handoff page write failed with status={result.status}"
            if existing and existing.get("status") == "ready":
                return {
                    **existing,
                    "fresh": False,
                    "reused": True,
                    "write_attempted": True,
                    "write_error": error,
                    "stale": existing.get("source_content_hash") != content_hash,
                }
            page = _save_page(
                conversation_id,
                source_content_hash=content_hash,
                covered_message_count=0,
                status="error",
                fields=_empty_fields(),
                facts=facts,
                error=error,
            )
            return {**page, "fresh": False, "reused": False, "stale": False, "write_attempted": True, "write_error": error}

        fields, field_error = _validate_fields(result.structured)
        if field_error or fields is None:
            error = field_error or "Invalid handoff page fields"
            if existing and existing.get("status") == "ready":
                return {
                    **existing,
                    "fresh": False,
                    "reused": True,
                    "write_attempted": True,
                    "write_error": error,
                    "stale": True,
                }
            page = _save_page(
                conversation_id,
                source_content_hash=content_hash,
                covered_message_count=0,
                status="error",
                fields=_empty_fields(),
                facts=facts,
                error=error,
            )
            return {**page, "fresh": False, "reused": False, "stale": False, "write_attempted": True, "write_error": error}

        page = _save_page(
            conversation_id,
            source_content_hash=content_hash,
            covered_message_count=message_count,
            status="ready",
            fields=fields,
            facts=facts,
            error=None,
        )
        return {**page, "fresh": True, "reused": False, "stale": False, "write_attempted": True}


def format_handoff_page_text(page: dict[str, Any], *, language: str = "zh") -> str:
    labels = {
        "goal": ("在做什么", "Goal"),
        "done": ("已经做完的", "Done"),
        "do_not_redo": ("不能重做的", "Do not redo"),
        "unverified": ("还没验证的", "Unverified"),
        "next_step": ("下一步", "Next step"),
    }
    lines = ["## 交接纸" if language != "en" else "## Handoff page"]
    status = page.get("status")
    if status == "unavailable":
        lines.append(
            "- 状态: 未生成（计算未启用或不可用；以下为最近原话）"
            if language != "en"
            else "- Status: not generated (compute disabled or unavailable; recent verbatim turns attached)"
        )
        return "\n".join(lines)
    if status != "ready":
        reason = page.get("write_error") or page.get("error") or status
        lines.append(
            f"- 状态: 未写成（{reason}）"
            if language != "en"
            else f"- Status: write failed ({reason})"
        )
        return "\n".join(lines)
    if page.get("stale") or page.get("fresh") is False:
        covered = page.get("covered_message_count")
        lines.append(
            f"- 说明: 纸写到第 {covered} 条；会话可能还有更新"
            if language != "en"
            else f"- Note: page covers through message {covered}; newer turns may exist"
        )
    if page.get("write_error"):
        lines.append(
            f"- 写纸说明: {page['write_error']}"
            if language != "en"
            else f"- Write note: {page['write_error']}"
        )
    facts = page.get("facts_json") or {}
    if isinstance(facts, dict) and facts.get("coverage_note"):
        lines.append(
            f"- 模型输入范围: {facts['coverage_note']}"
            if language != "en"
            else f"- Model input coverage: {facts['coverage_note']}"
        )
    for field in HANDOFF_PAGE_FIELDS:
        zh, en = labels[field]
        label = en if language == "en" else zh
        lines.append(f"- **{label}**: {page.get(field) or '—'}")
    return "\n".join(lines)


def wire_page_view(page: dict[str, Any] | None) -> dict[str, Any] | None:
    """Compact page payload for relay_conversation; excludes internal facts_json bloat."""
    if not page:
        return None
    status = page.get("status") or "unavailable"
    if status != "ready":
        return {
            "status": status,
            "fresh": False,
            "reused": False,
            "write_error": page.get("write_error") or page.get("error") or "",
        }
    out: dict[str, Any] = {
        "status": "ready",
        "covered_message_count": page.get("covered_message_count", 0),
        "goal": page.get("goal", ""),
        "done": page.get("done", ""),
        "do_not_redo": page.get("do_not_redo", ""),
        "unverified": page.get("unverified", ""),
        "next_step": page.get("next_step", ""),
        "fresh": bool(page.get("fresh")),
        "reused": bool(page.get("reused")),
    }
    if page.get("stale"):
        out["stale"] = True
    if page.get("write_error"):
        out["write_error"] = page["write_error"]
    return out


def public_page_view(page: dict[str, Any] | None) -> dict[str, Any] | None:
    """Detail-endpoint projection of a persisted handoff page (omits bulky facts_json)."""
    if not page:
        return None
    return {
        "conversation_id": page.get("conversation_id"),
        "source_content_hash": page.get("source_content_hash"),
        "covered_message_count": page.get("covered_message_count"),
        "status": page.get("status"),
        "goal": page.get("goal", ""),
        "done": page.get("done", ""),
        "do_not_redo": page.get("do_not_redo", ""),
        "unverified": page.get("unverified", ""),
        "next_step": page.get("next_step", ""),
        "error": page.get("error"),
        "updated_at": page.get("updated_at"),
        "fresh": page.get("fresh"),
        "reused": page.get("reused"),
        "stale": page.get("stale"),
        "write_error": page.get("write_error"),
    }
