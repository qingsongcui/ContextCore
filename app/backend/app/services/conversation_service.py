from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any
from fastapi import HTTPException
from app.date_filters import upper_date_bound
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode, decode


def normalize_iso_timestamp(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            val = float(ts)
            if val > 1e11:
                val /= 1000.0
            dt = datetime.fromtimestamp(val, tz=timezone.utc)
            return dt.isoformat()
        except Exception:
            return None
    if not isinstance(ts, str):
        return None
    raw = ts.strip()
    if not raw:
        return None
    try:
        clean = raw
        if clean.endswith("Z") or clean.endswith("z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def _extract_message_timestamps(messages: list[dict[str, Any]] | None) -> tuple[str | None, str | None]:
    if not messages:
        return None, None
    first_ts = None
    last_ts = None
    for m in messages:
        if isinstance(m, dict):
            raw = m.get("timestamp")
            if raw:
                norm = normalize_iso_timestamp(raw)
                if norm:
                    if first_ts is None:
                        first_ts = norm
                    last_ts = norm
    return first_ts, last_ts

def is_shared_database_path(source_path: str | None) -> bool:
    if not source_path:
        return False
    lower = source_path.lower()
    return (
        lower.endswith((".vscdb", ".db", ".sqlite", ".sqlite3"))
        or "#" in lower
        or "state.vscdb" in lower
    )


def escape_like(value: str) -> str:
    """Escape %, _, and backslash for SQLite LIKE queries using backslash as escape char."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compute_source_identity_key(agent: str, source_path: str | None, title: str, external_id: str | None = None) -> str:
    identity = (external_id or source_path or title).strip()
    raw = f"conversation:{agent}:{identity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def conversation_identity_keys(agent: str, source_path: str | None, title: str, external_id: str | None = None) -> list[str]:
    """Return identity keys for conversation deduplication and tombstones.
    For shared databases (e.g. Cursor state.vscdb, OpenCode db), only the exact session
    identifier (external_id) is used. Bare source_path must NEVER be used as an alias for
    shared databases, as tombstoning it would block sibling and future sessions.
    """
    primary = compute_source_identity_key(agent, source_path, title, external_id)
    keys = [primary]
    if external_id and source_path and not is_shared_database_path(source_path):
        alternate = compute_source_identity_key(agent, source_path, title, None)
        if alternate not in keys:
            keys.append(alternate)
    return keys


def compute_conversation_hash(agent: str, source_path: str | None, title: str, external_id: str | None = None) -> str:
    """Backward compatibility alias for stable source identity key."""
    return compute_source_identity_key(agent, source_path, title, external_id)


def compute_content_revision_hash(
    title: str,
    messages: list[dict[str, Any]] | None,
    process_summary: str = "",
    cwd: str | None = None,
) -> str:
    norm = _normalize_messages(messages)
    msgs_json = json.dumps(norm, sort_keys=True, ensure_ascii=False)
    summary_repr = (process_summary or "").strip()
    raw = f"content_rev:{title.strip()}:{cwd or ''}:{summary_repr}:{msgs_json}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _message_identity_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    return ""


def _tool_fields(message: dict[str, Any]) -> dict[str, Any]:
    """Keep a call and its result id intact. Do not shorten arguments."""
    fields: dict[str, Any] = {}
    calls = message.get("tool_calls")
    if isinstance(calls, list) and calls:
        fields["tool_calls"] = calls
    call_id = message.get("tool_call_id")
    if isinstance(call_id, str) and call_id.strip():
        fields["tool_call_id"] = call_id.strip()
    return fields


def _has_retained_payload(content: Any, tool_fields: dict[str, Any]) -> bool:
    if isinstance(content, str) and content.strip():
        return True
    if isinstance(content, list) and content:
        return True
    return bool(tool_fields)


def _normalize_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized = []
    for index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, (str, list)):
            content = ""
        fields = _tool_fields(message)
        if not _has_retained_payload(content, fields):
            continue
        message_id = message.get("id") or message.get("external_id") or f"m{index + 1}"
        normalized.append({
            "id": str(message_id),
            "role": str(message.get("role") or "assistant"),
            "content": content,
            "timestamp": str(message.get("timestamp") or ""),
            **fields,
        })
    return normalized


def _serialize_messages_with_offsets(messages: list[dict[str, str]]) -> tuple[str, list[tuple[int, int, int]]]:
    """Serialize one JSON array and record UTF-8 byte ranges for each message."""
    encoded_messages: list[str] = []
    offsets: list[tuple[int, int, int]] = []
    byte_position = 1  # after the opening '['
    for ordinal, message in enumerate(messages):
        encoded = json.dumps(message, ensure_ascii=False)
        if ordinal:
            byte_position += 1  # comma
        encoded_bytes = encoded.encode("utf-8")
        offsets.append((ordinal, byte_position, len(encoded_bytes)))
        byte_position += len(encoded_bytes)
        encoded_messages.append(encoded)
    return "[" + ",".join(encoded_messages) + "]", offsets


def _store_message_offsets(
    conn,
    conversation_id: str,
    offsets: list[tuple[int, int, int]],
) -> None:
    conn.execute("DELETE FROM conversation_message_offsets WHERE conversation_id=?", (conversation_id,))
    conn.execute("DELETE FROM conversation_message_index_state WHERE conversation_id=?", (conversation_id,))
    conn.executemany(
        """INSERT INTO conversation_message_offsets
           (conversation_id,ordinal,start_byte,byte_length) VALUES (?,?,?,?)""",
        ((conversation_id, ordinal, start, length) for ordinal, start, length in offsets),
    )
    conn.execute(
        "INSERT INTO conversation_message_index_state(conversation_id,message_count) VALUES (?,?)",
        (conversation_id, len(offsets)),
    )


def _merge_messages(
    existing_messages: list[dict[str, Any]] | None,
    incoming_messages: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    """Append/upsert incremental turns into existing messages rather than replacing them."""
    existing_norm = _normalize_messages(existing_messages)
    if not incoming_messages:
        return existing_norm
    if not existing_norm:
        return _normalize_messages(incoming_messages)

    by_id: dict[str, int] = {}
    content_map: dict[tuple[str, str], int] = {}
    merged = list(existing_norm)

    for idx, m in enumerate(merged):
        by_id[m["id"]] = idx
        content_map[(m["role"], _message_identity_text(m["content"]))] = idx

    for incoming in incoming_messages:
        if not isinstance(incoming, dict):
            continue
        content = incoming.get("content")
        if not isinstance(content, (str, list)):
            content = ""
        fields = _tool_fields(incoming)
        if not _has_retained_payload(content, fields):
            continue
        role = str(incoming.get("role") or "assistant")
        explicit_id = incoming.get("id") or incoming.get("external_id")
        ts = str(incoming.get("timestamp") or "")
        identity = _message_identity_text(content)

        if explicit_id and str(explicit_id) in by_id:
            idx = by_id[str(explicit_id)]
            old_key = (merged[idx]['role'], _message_identity_text(merged[idx]['content']))
            if content_map.get(old_key) == idx:
                content_map.pop(old_key, None)
            merged[idx] = {
                "id": str(explicit_id),
                "role": role,
                "content": content,
                "timestamp": ts or merged[idx]["timestamp"],
                **fields,
            }
            content_map[(role, identity)] = idx
            continue

        key = (role, identity)
        # Native identity wins over text similarity: two separate user turns
        # may legitimately say the same thing. Text dedup is only the fallback
        # for sources that did not provide a stable event/message identity.
        if not explicit_id and key in content_map:
            idx = content_map[key]
            if ts and not merged[idx]["timestamp"]:
                merged[idx]["timestamp"] = ts
            continue

        if explicit_id:
            new_mid = str(explicit_id)
        else:
            sequence = len(merged) + 1
            new_mid = f"m{sequence}"
            while new_mid in by_id:
                sequence += 1
                new_mid = f"m{sequence}"
        new_msg = {
            "id": new_mid,
            "role": role,
            "content": content,
            "timestamp": ts,
            **fields,
        }
        merged.append(new_msg)
        by_id[new_mid] = len(merged) - 1
        content_map[key] = len(merged) - 1

    return merged
def _sanitize_process_summary(summary: Any) -> str:
    if not summary:
        return ""
    if isinstance(summary, str):
        text = summary.strip()
        return "" if text in {"[]", "{}", "null", "None"} else text
    if isinstance(summary, list):
        lines = []
        for item in summary:
            if isinstance(item, dict):
                cmd = str(item.get("command") or item.get("name") or "")
                has_outcome = "outcome" in item or "result" in item
                if has_outcome:
                    out = item.get("outcome", item.get("result"))
                    out_text = "" if out is None else str(out)
                    lines.append(f"- **{cmd}**: {out_text}" if cmd else out_text)
                elif cmd:
                    lines.append(f"- **{cmd}**: （结果未记录）")
                else:
                    lines.append("- （不完整的工具记录，未截断也未补写结果）")
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)
    return str(summary)


def _conversation_filters(
    project_id: str | None = None,
    query: str | None = None,
    agent: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> tuple[str, tuple]:
    clauses = []
    params = []

    if project_id == "__unassigned__":
        clauses.append("project_id IS NULL")
    elif project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if agent:
        clauses.append("agent = ?")
        params.append(agent)
    if since:
        since_clean = since.strip()
        norm_since = normalize_iso_timestamp(since_clean) if len(since_clean) > 10 else since_clean
        clauses.append("started_at >= ?")
        params.append(norm_since or since_clean)
    if until:
        until_clean = until.strip()
        op, bound = upper_date_bound(until_clean)
        norm_bound = normalize_iso_timestamp(bound) if op == "<=" else bound
        clauses.append(f"started_at {op} ?")
        params.append(norm_bound or bound)
    if query:
        escaped_query = escape_like(query.strip())
        q = f"%{escaped_query}%"
        clauses.append("(title LIKE ? ESCAPE '\\' OR messages LIKE ? ESCAPE '\\' OR external_id LIKE ? ESCAPE '\\')")
        params.extend([q, q, q])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, tuple(params)


def list_conversations(
    project_id: str | None = None,
    query: str | None = None,
    agent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    summary: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200):
        raise HTTPException(422, "limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise HTTPException(422, "offset must be nonnegative")
    where, params = _conversation_filters(project_id, query, agent, since, until)
    columns = _CONVERSATION_INDEX_COLUMNS if summary else "*"
    sql = f"SELECT {columns} FROM conversations {where} ORDER BY updated_at DESC,id DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params = (*params, limit, offset)
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row) if summary else row_to_dict(row)
            if summary:
                item["messages"] = []
                item["process_summary"] = ""
                item["started_at"] = item.get("started_at") or ""
            result.append(item)
        return result


# Explicit metadata projection: never fetch/deserialize transcript or summary.
_CONVERSATION_INDEX_COLUMNS = (
    "id,external_id,agent,title,project_id,cwd,git_branch,git_head,source_path,started_at,"
    "updated_at,processing_status,created_at"
)


def conversation_index(
    project_id: str | None = None,
    query: str | None = None,
    agent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise HTTPException(422, "offset must be nonnegative")
    where, params = _conversation_filters(project_id, query, agent, since, until)
    with db() as conn:
        # Count and page use one consistent read snapshot while import commits.
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) FROM conversations {where}", params).fetchone()[0]
        cursor = conn.execute(
            f"SELECT {_CONVERSATION_INDEX_COLUMNS} FROM conversations {where} "
            "ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        items = [dict(row) for row in cursor]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_conversation(conversation_id: str, *, include_messages: bool = True) -> dict[str, Any]:
    with db() as conn:
        columns = "*" if include_messages else _CONVERSATION_INDEX_COLUMNS
        row = conn.execute(
            f"SELECT {columns} FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return row_to_dict(row) if include_messages else dict(row)


def conversation_messages_page(
    conversation_id: str, *, limit: int = 30, offset: int = 0
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise HTTPException(422, "limit must be between 1 and 100")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise HTTPException(422, "offset must be nonnegative")
    with db() as conn:
        if not callable(getattr(conn, "blobopen", None)):
            raise HTTPException(
                status_code=503,
                detail="Conversation message paging requires Python 3.11 or newer with SQLite incremental BLOB support",
            )
        total = _ensure_conversation_message_offsets(conn, conversation_id)
        offsets = conn.execute(
            """SELECT ordinal,start_byte,byte_length FROM conversation_message_offsets
               WHERE conversation_id=? ORDER BY ordinal LIMIT ? OFFSET ?""",
            (conversation_id, limit, offset),
        ).fetchall()
        source = conn.execute("SELECT rowid FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not source:
            raise HTTPException(status_code=404, detail="Conversation not found")
        try:
            items = []
            with conn.blobopen("conversations", "messages", source["rowid"], readonly=True) as transcript:
                for item_offset in offsets:
                    transcript.seek(item_offset["start_byte"])
                    raw_message = transcript.read(item_offset["byte_length"])
                    if len(raw_message) != item_offset["byte_length"]:
                        raise ValueError("message index no longer matches stored transcript")
                    items.append(json.loads(raw_message.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Stored conversation message is malformed") from exc
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _ensure_conversation_message_offsets(conn, conversation_id: str) -> int:
    """Read or build the bounded byte-offset index in a consistent transaction."""
    conn.execute("BEGIN")
    state = conn.execute(
        "SELECT message_count FROM conversation_message_index_state WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    if state:
        return state["message_count"]

    exists = conn.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Legacy rows have no offsets yet. A writer lock keeps this one-time index
    # build atomic with concurrent transcript replacement/deletion.
    conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
    state = conn.execute(
        "SELECT message_count FROM conversation_message_index_state WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    if state:
        return state["message_count"]
    row = conn.execute("SELECT rowid FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return _build_conversation_message_offsets(conn, conversation_id, row["rowid"])


def _build_conversation_message_offsets(conn, conversation_id: str, rowid: int) -> int:
    """Index a legacy JSON array a bounded byte chunk at a time, without JSON1."""
    chunk_size = 64 * 1024
    batch_size = 256
    batch: list[tuple[str, int, int, int]] = []
    count = 0
    position = 0
    depth = 0
    item_start: int | None = None
    started = False
    closed = False
    after_comma = False
    in_string = False
    escaped = False
    whitespace = {0x20, 0x09, 0x0A, 0x0D}

    conn.execute("DELETE FROM conversation_message_offsets WHERE conversation_id=?", (conversation_id,))
    conn.execute("DELETE FROM conversation_message_index_state WHERE conversation_id=?", (conversation_id,))
    try:
        with conn.blobopen("conversations", "messages", rowid, readonly=True) as transcript:
            size = len(transcript)
            while position < size:
                transcript.seek(position)
                chunk = transcript.read(min(chunk_size, size - position))
                if not chunk:
                    raise ValueError("transcript ended before its declared length")
                for chunk_index, byte in enumerate(chunk):
                    absolute = position + chunk_index
                    if not started:
                        if byte in whitespace:
                            continue
                        if byte != ord("["):
                            raise ValueError("stored messages are not a JSON array")
                        started = True
                        depth = 1
                        continue
                    if closed:
                        if byte not in whitespace:
                            raise ValueError("trailing data after stored JSON array")
                        continue
                    if in_string:
                        if escaped:
                            escaped = False
                        elif byte == ord("\\"):
                            escaped = True
                        elif byte == ord('"'):
                            in_string = False
                        continue
                    if byte == ord('"'):
                        if item_start is None:
                            item_start = absolute
                            after_comma = False
                        in_string = True
                        continue
                    if byte in (ord("{"), ord("[")):
                        if item_start is None:
                            item_start = absolute
                            after_comma = False
                        depth += 1
                        continue
                    if byte == ord("}"):
                        if depth <= 1:
                            raise ValueError("unbalanced object in stored messages")
                        depth -= 1
                        continue
                    if byte == ord("]"):
                        if depth == 1:
                            if after_comma:
                                raise ValueError("trailing comma in stored messages")
                            if item_start is not None:
                                byte_length = absolute - item_start
                                if byte_length <= 0:
                                    raise ValueError("empty message in stored messages")
                                batch.append((conversation_id, count, item_start, byte_length))
                                count += 1
                            depth = 0
                            closed = True
                        elif depth > 1:
                            depth -= 1
                        else:
                            raise ValueError("unbalanced array in stored messages")
                        continue
                    if byte == ord(",") and depth == 1:
                        if item_start is None:
                            raise ValueError("empty message in stored messages")
                        byte_length = absolute - item_start
                        if byte_length <= 0:
                            raise ValueError("empty message in stored messages")
                        batch.append((conversation_id, count, item_start, byte_length))
                        count += 1
                        item_start = None
                        after_comma = True
                    elif byte not in whitespace and item_start is None:
                        item_start = absolute
                        after_comma = False

                    if len(batch) >= batch_size:
                        conn.executemany(
                            """INSERT INTO conversation_message_offsets
                               (conversation_id,ordinal,start_byte,byte_length) VALUES (?,?,?,?)""",
                            batch,
                        )
                        batch.clear()
                position += len(chunk)
        if not started or not closed or in_string or depth != 0:
            raise ValueError("stored messages are not a complete JSON array")
        if batch:
            conn.executemany(
                """INSERT INTO conversation_message_offsets
                   (conversation_id,ordinal,start_byte,byte_length) VALUES (?,?,?,?)""",
                batch,
            )
    except (sqlite3.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Stored conversation messages are not a valid JSON array") from exc

    conn.execute(
        "INSERT INTO conversation_message_index_state(conversation_id,message_count) VALUES (?,?)",
        (conversation_id, count),
    )
    return count


def _recorded_git_checkout(cwd: str | None) -> tuple[str | None, str | None]:
    """Branch name and commit recorded for this conversation's directory. Detached HEAD stores no branch."""
    if not cwd or not str(cwd).strip():
        return None, None
    from app.services.project_service import read_git_checkout
    checkout = read_git_checkout(str(cwd))
    if not checkout:
        return None, None
    branch = None if checkout.get("detached") else checkout.get("branch")
    head = checkout.get("head")
    return (str(branch) if branch else None), (str(head) if head else None)


def _ensure_relay_index(conn) -> None:
    """Small derived metadata, never a second copy of the transcript."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_relay_index (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
            source_hash TEXT NOT NULL,
            continuation_only INTEGER NOT NULL,
            title_zh TEXT NOT NULL,
            title_en TEXT NOT NULL,
            brief_zh TEXT NOT NULL,
            brief_en TEXT NOT NULL,
            message_count INTEGER NOT NULL
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversation_relay_index)")}
    if "has_human_turn" not in columns:
        conn.execute("ALTER TABLE conversation_relay_index ADD COLUMN has_human_turn INTEGER")


RELAY_INDEX_VERSION_PREFIX = "v4:"


def _has_human_turn(messages: list[dict[str, Any]]) -> bool:
    """True when a real user turn exists; assistant-only worker reports do not count."""
    from app.services.handoff_service import _is_continuation_prompt

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "").lower() != "user":
            continue
        content = " ".join(str(msg.get("content") or "").split()).strip()
        if not content or _is_continuation_prompt(content):
            continue
        if "<name>contextcore" in content or "$contextcore" in content or "contextcore-handoff/SKILL.md" in content:
            continue
        if content.startswith("Generate a title and a git branch name for a coding agent"):
            continue
        return True
    return False


def _versioned_relay_hash(source_hash: str | None) -> str:
    return f"{RELAY_INDEX_VERSION_PREFIX}{source_hash or ''}"


def _update_relay_index(conn, conversation_id: str, source_hash: str, conversation: dict[str, Any]) -> None:
    from app.services.handoff_service import _clean_line, _is_continuation_only_session, _summarize_conversation_for_relay

    _ensure_relay_index(conn)
    versioned = _versioned_relay_hash(source_hash)
    current = conn.execute(
        "SELECT source_hash FROM conversation_relay_index WHERE conversation_id=?", (conversation_id,)
    ).fetchone()
    if current and current["source_hash"] == versioned:
        return
    zh = _summarize_conversation_for_relay(conversation, "zh", include_tools=False)
    en = _summarize_conversation_for_relay(conversation, "en", include_tools=False)
    msgs = conversation.get("messages")
    msg_list = msgs if isinstance(msgs, list) else []
    conn.execute("""
        INSERT INTO conversation_relay_index
            (conversation_id,source_hash,continuation_only,title_zh,title_en,brief_zh,brief_en,message_count,has_human_turn)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(conversation_id) DO UPDATE SET
            source_hash=excluded.source_hash,continuation_only=excluded.continuation_only,
            title_zh=excluded.title_zh,title_en=excluded.title_en,
            brief_zh=excluded.brief_zh,brief_en=excluded.brief_en,message_count=excluded.message_count,
            has_human_turn=excluded.has_human_turn
    """, (
        conversation_id, versioned, int(_is_continuation_only_session(msg_list)),
        _clean_line(zh["title"], 120), _clean_line(en["title"], 120),
        _clean_line(zh["brief_summary"], 600), _clean_line(en["brief_summary"], 600),
        len(msg_list), int(_has_human_turn(msg_list)),
    ))


def backfill_relay_index_batch(*, limit: int = 100, stop=None) -> int:
    """Incrementally index legacy/upgraded conversations newest-first off the request path."""
    bounded = max(1, min(int(limit), 500))
    with db() as conn:
        _ensure_relay_index(conn)
        rows = conn.execute(
            """
            SELECT c.id, c.agent, c.title, c.cwd, c.messages, c.process_summary, c.content_hash
            FROM conversations c
            LEFT JOIN conversation_relay_index r
              ON r.conversation_id = c.id
             AND r.source_hash = (? || coalesce(c.content_hash, ''))
            WHERE r.conversation_id IS NULL
            ORDER BY c.updated_at DESC, c.id DESC
            LIMIT ?
            """,
            (RELAY_INDEX_VERSION_PREFIX, bounded),
        ).fetchall()
    if not rows:
        return 0
    with db() as conn:
        _ensure_relay_index(conn)
        for row in rows:
            if stop is not None and stop.is_set():
                break
            msgs = decode(row["messages"], [])
            if not isinstance(msgs, list):
                msgs = []
            raw_title = (row["title"] or "").strip()
            conv_payload = {
                "id": row["id"],
                "agent": row["agent"],
                "title": raw_title,
                "cwd": row["cwd"] or "",
                "messages": msgs,
                "process_summary": row["process_summary"] or "",
            }
            effective_title = raw_title
            if (not raw_title or raw_title.lower() in {"untitled session", "untitled", "new chat"}) and msgs:
                from app.services.handoff_service import _clean_line, _summarize_conversation_for_relay
                derived = _clean_line(_summarize_conversation_for_relay(conv_payload, "zh", include_tools=False)["title"], 120)
                if derived and derived not in {"未命名会话", "Untitled conversation"}:
                    effective_title = derived
                    conv_payload["title"] = effective_title
            c_hash = row["content_hash"]
            if not c_hash or effective_title != raw_title:
                c_hash = compute_content_revision_hash(effective_title, msgs, row["process_summary"] or "", row["cwd"])
                conn.execute(
                    "UPDATE conversations SET title = ?, content_hash = ? WHERE id = ?",
                    (effective_title, c_hash, row["id"]),
                )
            _update_relay_index(conn, row["id"], c_hash, conv_payload)
        remaining = conn.execute(
            """
            SELECT COUNT(*)
            FROM conversations c
            LEFT JOIN conversation_relay_index r
              ON r.conversation_id = c.id
             AND r.source_hash = (? || coalesce(c.content_hash, ''))
            WHERE r.conversation_id IS NULL
            """,
            (RELAY_INDEX_VERSION_PREFIX,),
        ).fetchone()[0]
    return int(remaining)

def upsert_conversation(
    agent: str,
    title: str,
    external_id: str | None = None,
    project_id: str | None = None,
    cwd: str | None = None,
    source_path: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    process_summary: Any = "",
    incremental: bool = False,
    started_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")

    source_key = compute_source_identity_key(agent, source_path, clean_title, external_id)
    now = now_iso()
    clean_summary = _sanitize_process_summary(process_summary)
    target_id = None
    changed = False

    norm_started = normalize_iso_timestamp(started_at)
    norm_updated = normalize_iso_timestamp(updated_at)
    if norm_started is None or norm_updated is None:
        msg_first, msg_last = _extract_message_timestamps(messages)
        if norm_started is None:
            norm_started = msg_first
        if norm_updated is None:
            norm_updated = msg_last
    if norm_started is not None and norm_updated is None:
        norm_updated = norm_started
    elif norm_updated is not None and norm_started is None:
        norm_started = norm_updated
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        from app.services import processing_jobs
        processing_jobs.ensure_schema(conn)
        if project_id:
            proj_row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj_row:
                raise HTTPException(status_code=404, detail=f"Target project '{project_id}' not found")

        if is_shared_database_path(source_path):
            shared_key = compute_source_identity_key(agent, source_path, "", None)
            conn.execute(
                "DELETE FROM tombstones WHERE entity_type = 'conversation' AND dedupe_key = ?",
                (shared_key,),
            )

        keys = conversation_identity_keys(agent, source_path, clean_title, external_id)
        tombstone = conn.execute(
            f"SELECT id FROM tombstones WHERE entity_type = 'conversation' AND dedupe_key IN ({','.join('?' for _ in keys)})",
            tuple(keys),
        ).fetchone()
        if tombstone:
            raise HTTPException(
                status_code=410,
                detail="Conversation was previously deleted and tombstoned.",
            )

        existing = None
        if external_id:
            existing = conn.execute(
                "SELECT * FROM conversations WHERE agent = ? AND external_id = ?",
                (agent, external_id),
            ).fetchone()
        if not existing and source_path and external_id and not is_shared_database_path(source_path):
            existing = conn.execute(
                "SELECT * FROM conversations WHERE agent = ? AND source_path = ? AND (external_id IS NULL OR external_id = '')",
                (agent, source_path),
            ).fetchone()
        if not existing and source_path and not external_id:
            existing = conn.execute(
                "SELECT * FROM conversations WHERE agent = ? AND source_path = ?",
                (agent, source_path),
            ).fetchone()

        if existing:
            target_id = existing["id"]
            # A legacy path-only import may acquire its native session ID later,
            # even when the transcript is byte-for-byte unchanged.
            if external_id and existing["external_id"] != external_id:
                conn.execute(
                    "UPDATE conversations SET external_id = ? WHERE id = ?",
                    (external_id, target_id),
                )
            existing_msgs = decode(existing["messages"], [])
            if incremental:
                merged_msgs = _merge_messages(existing_msgs, messages)
                target_started_at = existing["started_at"]
                if norm_started and norm_started < target_started_at:
                    target_started_at = norm_started
                if norm_updated:
                    target_updated_at = max(existing["updated_at"], norm_updated)
                else:
                    target_updated_at = existing["updated_at"]
            else:
                merged_msgs = _normalize_messages(messages)
                target_started_at = norm_started or existing["started_at"] or now
                target_updated_at = norm_updated or existing["updated_at"] or now

            new_content_hash = compute_content_revision_hash(clean_title, merged_msgs, clean_summary, cwd)

            if (existing["content_hash"] == new_content_hash
                    and (project_id is None or project_id == existing["project_id"])):
                changed = False
                ts_updates = []
                ts_params = []
                if norm_started and existing["started_at"] != target_started_at:
                    ts_updates.append("started_at = ?")
                    ts_params.append(target_started_at)
                if norm_updated and existing["updated_at"] != target_updated_at:
                    ts_updates.append("updated_at = ?")
                    ts_params.append(target_updated_at)
                if not existing["git_branch"] and not existing["git_head"]:
                    branch_name, head_sha = _recorded_git_checkout(cwd or existing["cwd"])
                    if branch_name is not None or head_sha is not None:
                        ts_updates.append("git_branch = ?")
                        ts_params.append(branch_name)
                        ts_updates.append("git_head = ?")
                        ts_params.append(head_sha)
                if ts_updates:
                    ts_params.append(target_id)
                    conn.execute(f"UPDATE conversations SET {', '.join(ts_updates)} WHERE id = ?", tuple(ts_params))
                eff_pid = project_id if project_id is not None else existing["project_id"]
                if eff_pid:
                    from app.services import project_service
                    project_service.touch_project_activity(conn, eff_pid, target_updated_at)
            else:
                changed = True
                branch_name, head_sha = _recorded_git_checkout(cwd)
                if branch_name is None and head_sha is None and cwd == existing["cwd"]:
                    branch_name = existing["git_branch"]
                    head_sha = existing["git_head"]
                serialized_messages, message_offsets = _serialize_messages_with_offsets(merged_msgs)
                conn.execute(
                    """
                    UPDATE conversations SET
                        title = ?,
                        cwd = ?,
                        git_branch = ?,
                        git_head = ?,
                        source_path = ?,
                        messages = ?,
                        process_summary = ?,
                        project_id = coalesce(?, project_id),
                        content_hash = ?,
                        started_at = ?,
                        updated_at = ?,
                        processing_status = 'pending',
                        processing_error = NULL
                    WHERE id = ?
                    """,
                    (
                        clean_title,
                        cwd,
                        branch_name,
                        head_sha,
                        source_path,
                        serialized_messages,
                        clean_summary,
                        project_id,
                        new_content_hash,
                        target_started_at,
                        target_updated_at,
                        target_id,
                    ),
                )
                _store_message_offsets(conn, target_id, message_offsets)
                eff_pid = project_id if project_id is not None else existing["project_id"]
                if eff_pid:
                    from app.services import project_service
                    project_service.touch_project_activity(conn, eff_pid, target_updated_at)
                log_audit(actor=agent, action="conversation.update_sync", target=target_id, conn=conn)
                from app.services import distillation_service
                distillation_service.queue_distillation(target_id, _connection=conn)
        else:
            changed = True
            target_id = new_id("conv")
            merged_msgs = _normalize_messages(messages)
            serialized_messages, message_offsets = _serialize_messages_with_offsets(merged_msgs)
            new_content_hash = compute_content_revision_hash(clean_title, merged_msgs, clean_summary, cwd)
            branch_name, head_sha = _recorded_git_checkout(cwd)
            target_started_at = norm_started or now
            target_updated_at = norm_updated or now
            conn.execute(
                """
                INSERT INTO conversations (
                    id, external_id, project_id, agent, title, cwd, git_branch, git_head, source_path,
                    started_at, updated_at, messages, process_summary, processing_status,
                    processing_error, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?)
                """,
                (
                    target_id,
                    external_id,
                    project_id,
                    agent,
                    clean_title,
                    cwd,
                    branch_name,
                    head_sha,
                    source_path,
                    target_started_at,
                    target_updated_at,
                    serialized_messages,
                    clean_summary,
                    new_content_hash,
                    now,
                ),
            )
            _store_message_offsets(conn, target_id, message_offsets)
            if project_id:
                from app.services import project_service
                project_service.touch_project_activity(conn, project_id, target_updated_at)
            log_audit(actor=agent, action="conversation.create", target=target_id, conn=conn)
            from app.services import distillation_service
            distillation_service.queue_distillation(target_id, _connection=conn)
        _update_relay_index(conn, target_id, new_content_hash, {
            "agent": agent, "title": clean_title, "messages": merged_msgs, "process_summary": clean_summary,
        })
    res = get_conversation(target_id)
    res["_changed"] = changed
    return res

def create_conversation(
    agent: str,
    title: str,
    project_id: str | None = None,
    source_path: str | None = None,
    messages: list[dict[str, Any]] | None = None,
    process_summary: Any = "",
    external_id: str | None = None,
    cwd: str | None = None,
    started_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    return upsert_conversation(
        agent=agent,
        title=title,
        external_id=external_id,
        project_id=project_id,
        cwd=cwd,
        source_path=source_path,
        messages=messages,
        process_summary=process_summary,
        started_at=started_at,
        updated_at=updated_at,
    )


def update_conversation(
    conversation_id: str,
    title: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        from app.services import processing_jobs, distillation_service
        processing_jobs.ensure_schema(conn)
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")

        updates = []
        params = []
        now = now_iso()

        if title is not None:
            clean_title = title.strip()
            if not clean_title:
                raise HTTPException(status_code=422, detail="Title cannot be empty")
            updates.append("title = ?")
            params.append(clean_title)

        if project_id is not None:
            if project_id:
                proj_row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
                if not proj_row:
                    raise HTTPException(status_code=404, detail=f"Target project '{project_id}' not found")
            updates.append("project_id = ?")
            params.append(project_id)

        updates.append("updated_at = ?")
        params.append(now)
        params.append(conversation_id)

        conn.execute(f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?", tuple(params))
        updated = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        revision_hash = compute_content_revision_hash(updated['title'], decode(updated['messages'], []), updated['process_summary'], updated['cwd'])
        conn.execute("UPDATE conversations SET content_hash=? WHERE id=?", (revision_hash, conversation_id))
        _update_relay_index(conn, conversation_id, revision_hash, row_to_dict(updated))
        if processing_jobs.source_fingerprint(row) != processing_jobs.source_fingerprint(conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()):
            distillation_service.queue_distillation(conversation_id, _connection=conn)
        if updated["project_id"]:
            from app.services import project_service
            project_service.touch_project_activity(conn, updated["project_id"], now)
        log_audit(actor="user", action="conversation.update", target=conversation_id, conn=conn)

    # Outside transaction block
    return get_conversation(conversation_id)


def delete_conversation(conversation_id: str, actor: str = "user") -> dict[str, bool]:
    from app.services.handoff_service import invalidate_handoffs

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conversation not found")

        conv = row_to_dict(row)
        now = now_iso()

        # Invalidate handoffs inside same txn before physical delete
        invalidate_handoffs(conn, conversation_id)

        for source_key in conversation_identity_keys(
            conv["agent"], conv.get("source_path"), conv["title"], conv.get("external_id")
        ):
            conn.execute(
                """
                INSERT OR REPLACE INTO tombstones (
                    id, entity_type, dedupe_key, scope_project_id, deleted_at, deleted_by, reason
                ) VALUES (?, 'conversation', ?, ?, ?, ?, 'user_deleted')
                """,
                (new_id("tomb"), source_key, conv.get("project_id"), now, actor),
            )

        from app.services.processing_jobs import cancel_conversation_jobs
        cancel_conversation_jobs(conn, conversation_id)

        # Remove from distillation queue
        conn.execute("DELETE FROM distillation_queue WHERE conversation_id = ?", (conversation_id,))

        # Mark any content sources linked to this conversation as deleted
        conn.execute(
            "UPDATE content_sources SET deleted = 1 WHERE source_id = ? AND source_kind = 'conversation'",
            (conversation_id,),
        )

        # Delete conversation (memories remain intact per D011 independent deletion)
        _ensure_relay_index(conn)
        conn.execute("DELETE FROM conversation_relay_index WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

        log_audit(actor=actor, action="conversation.delete", target=conversation_id, conn=conn)

    return {"ok": True}
