"""
ContextCore Cursor Adapter.

History support is implemented for both desktop Cursor local SQLite state.vscdb
and Cursor CLI (`cursor agent`) local SQLite/protobuf store.db (`~/.cursor/chats/<md5>/<uuid>/store.db`),
following patterns observed in open-source projects:
- grapeot/ai_session_export (MIT License, https://github.com/grapeot/ai_session_export)
- vshulcz/deja-vu (MIT License, https://github.com/vshulcz/deja-vu)
- munhq/chat-recall (Elastic License 2.0, https://github.com/munhq/chat-recall)

Observed and verified schema:
- Desktop Database: state.vscdb (SQLite 3)
  - Table composerHeaders:
      composerId TEXT, value TEXT (JSON), isSubagent INTEGER, createdAt INTEGER, lastUpdatedAt INTEGER
  - Table cursorDiskKV:
      key TEXT, value BLOB / TEXT (JSON)
- CLI Database: store.db (SQLite 3)
  - Table meta: key TEXT PRIMARY KEY, value TEXT (hex-encoded JSON with agentId, latestRootBlobId, name, createdAt)
  - Table blobs: id TEXT PRIMARY KEY, data BLOB (protobuf root node + UTF-8 JSON message blobs)
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .base import (
    MAX_SUMMARY_LENGTH,
    AgentAdapterError,
    bound_process_summary,
    deduplicate_roots,
    isolated_sqlite_snapshot,
    validate_path,
)

# Prefix format: bubbleId:<composerId>:<bubbleId>
BUBBLE_PREFIX = "bubbleId:"
BUBBLE_RANGE_END = "bubbleId;"
USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL | re.IGNORECASE)
WORKSPACE_PATH_RE = re.compile(r"Workspace Path:\s*([^\n\r<]+)")


def _extract_store_root_blob_refs(
    data: bytes,
    known_ids: set[str] | None = None,
    depth: int = 0,
) -> list[str]:
    """Extract ordered 32-byte SHA-256 or 64-char hex message blob references from a Cursor CLI index/root protobuf blob."""
    if depth > 16 or not data:
        return []
    refs: list[str] = []
    pos = 0
    n = len(data)
    while pos < n:
        tag = 0
        shift = 0
        terminated = False
        while pos < n:
            b = data[pos]
            pos += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                terminated = True
                break
            shift += 7
            if shift > 35:
                return []
        if not terminated:
            return []
        wire_type = tag & 0x07
        field_num = tag >> 3
        if field_num == 0 or wire_type in (3, 4, 6, 7):
            return []
        if wire_type == 0:
            v_shift = 0
            v_term = False
            while pos < n:
                b = data[pos]
                pos += 1
                if not (b & 0x80):
                    v_term = True
                    break
                v_shift += 7
                if v_shift > 63:
                    return []
            if not v_term:
                return []
        elif wire_type == 1:
            if pos + 8 > n:
                return []
            pos += 8
        elif wire_type == 2:
            length = 0
            shift = 0
            l_term = False
            while pos < n:
                b = data[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    l_term = True
                    break
                shift += 7
                if shift > 35:
                    return []
            if not l_term or length < 0 or pos + length > n:
                return []
            chunk = data[pos : pos + length]
            pos += length
            matched = False
            if length == 32:
                hex_id = chunk.hex()
                if known_ids is None:
                    if field_num == 1:
                        refs.append(hex_id)
                        matched = True
                elif hex_id in known_ids:
                    refs.append(hex_id)
                    matched = True
            elif length == 64 and known_ids is not None:
                try:
                    ascii_id = chunk.decode("ascii").lower()
                    if ascii_id in known_ids:
                        refs.append(ascii_id)
                        matched = True
                except UnicodeDecodeError:
                    pass
            if not matched and depth < 16 and length > 2:
                nested = _extract_store_root_blob_refs(chunk, known_ids=known_ids, depth=depth + 1)
                if nested:
                    refs.extend(nested)
        elif wire_type == 5:
            if pos + 4 > n:
                return []
            pos += 4
        else:
            return []
    return refs


def _looks_like_submessage_wire(chunk: bytes) -> bool:
    """Quick structural check for whether length-delimited bytes plausibly encode a >=2-field protobuf message."""
    if len(chunk) < 4:
        return False
    pos = 0
    n = len(chunk)
    fields = 0
    while pos < n:
        tag = 0
        shift = 0
        term = False
        while pos < n:
            b = chunk[pos]
            pos += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                term = True
                break
            shift += 7
            if shift > 35:
                return False
        if not term:
            return False
        wire = tag & 0x07
        fnum = tag >> 3
        if fnum <= 0 or fnum > 64 or wire in (3, 4, 6, 7):
            return False
        if wire == 0:
            v_shift = 0
            v_term = False
            while pos < n:
                b = chunk[pos]
                pos += 1
                if not (b & 0x80):
                    v_term = True
                    break
                v_shift += 7
                if v_shift > 63:
                    return False
            if not v_term:
                return False
        elif wire == 1:
            if pos + 8 > n:
                return False
            pos += 8
        elif wire == 2:
            ln = 0
            l_shift = 0
            l_term = False
            while pos < n:
                b = chunk[pos]
                pos += 1
                ln |= (b & 0x7F) << l_shift
                if not (b & 0x80):
                    l_term = True
                    break
                l_shift += 7
                if l_shift > 35:
                    return False
            if not l_term or ln < 0 or pos + ln > n:
                return False
            pos += ln
        elif wire == 5:
            if pos + 4 > n:
                return False
            pos += 4
        fields += 1
    return pos == n and fields >= 2


def _decode_store_blob_message(raw_bytes: bytes, depth: int = 0) -> dict[str, Any] | None:
    """Decode a Cursor CLI store.db blob from either UTF-8 JSON or binary protobuf."""
    try:
        text_payload = raw_bytes.decode("utf-8")
        parsed = json.loads(text_payload)
        if isinstance(parsed, dict) and ("role" in parsed or "content" in parsed or "text" in parsed):
            return parsed
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    if depth > 8 or not raw_bytes:
        return None

    pos = 0
    n = len(raw_bytes)
    varint_fields: list[tuple[int, int]] = []
    string_fields: list[tuple[int, str]] = []
    nested_role = ""
    nested_texts: list[str] = []

    while pos < n:
        tag = 0
        shift = 0
        tag_term = False
        while pos < n:
            b = raw_bytes[pos]
            pos += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                tag_term = True
                break
            shift += 7
            if shift > 35:
                return None
        if not tag_term:
            return None
        wire_type = tag & 0x07
        field_num = tag >> 3
        if field_num == 0 or wire_type in (3, 4, 6, 7):
            return None
        if wire_type == 0:
            val = 0
            shift = 0
            val_term = False
            while pos < n:
                b = raw_bytes[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    val_term = True
                    break
                shift += 7
                if shift > 63:
                    return None
            if not val_term:
                return None
            varint_fields.append((field_num, val))
        elif wire_type == 1:
            if pos + 8 > n:
                return None
            pos += 8
        elif wire_type == 2:
            length = 0
            shift = 0
            len_term = False
            while pos < n:
                b = raw_bytes[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    len_term = True
                    break
                shift += 7
                if shift > 35:
                    return None
            if not len_term or length < 0 or pos + length > n:
                return None
            chunk = raw_bytes[pos : pos + length]
            pos += length
            if not chunk:
                continue

            # 1. Check JSON dict message wrapper first
            try:
                decoded_json = chunk.decode("utf-8")
                if decoded_json.startswith("{") and decoded_json.endswith("}"):
                    try:
                        inner_json = json.loads(decoded_json)
                        if isinstance(inner_json, dict) and ("role" in inner_json or "content" in inner_json or "text" in inner_json):
                            return inner_json
                    except json.JSONDecodeError:
                        pass
            except UnicodeDecodeError:
                decoded_json = ""

            # 2. Judiciously try nested protobuf submessage decode before treating tag-prefixed bytes as plain text
            if depth < 8 and _looks_like_submessage_wire(chunk):
                nested_msg = _decode_store_blob_message(chunk, depth=depth + 1)
                if isinstance(nested_msg, dict) and nested_msg.get("role") in ("user", "assistant", "tool", "system"):
                    if not nested_role:
                        nested_role = str(nested_msg["role"])
                    nested_content = nested_msg.get("content")
                    if isinstance(nested_content, str) and nested_content.strip():
                        nested_texts.append(nested_content.strip())
                    continue

            # 3. Plain UTF-8 text or role field
            if decoded_json:
                stripped = decoded_json.strip()
                if stripped and all(ord(ch) >= 32 or ch in "\n\r\t" for ch in stripped):
                    if len(stripped) == 64 and all(c in "0123456789abcdefABCDEF" for c in stripped):
                        continue
                    string_fields.append((field_num, stripped))
                    continue

            # 4. Non-UTF8 / non-printable length-delimited wrapper fallback
            if depth < 8:
                nested_msg = _decode_store_blob_message(chunk, depth=depth + 1)
                if isinstance(nested_msg, dict) and nested_msg.get("role") in ("user", "assistant", "tool", "system"):
                    if not nested_role:
                        nested_role = str(nested_msg["role"])
                    nested_content = nested_msg.get("content")
                    if isinstance(nested_content, str) and nested_content.strip():
                        nested_texts.append(nested_content.strip())
        elif wire_type == 5:
            if pos + 4 > n:
                return None
            pos += 4
        else:
            return None

    varint_role: int | None = None
    for fnum, val in varint_fields:
        if fnum in (1, 2, 3) and val in (1, 2):
            varint_role = val
            break

    role_val = ""
    role_idx: int | None = None
    has_other_content = len(string_fields) > 1 or bool(nested_texts)
    for idx, (fnum, s_val) in enumerate(string_fields):
        low = s_val.lower()
        if fnum <= 3 and low in ("user", "assistant", "tool", "system"):
            if has_other_content or (low in ("tool", "system") and varint_role is None):
                role_val = low
                role_idx = idx
                break

    text_parts: list[str] = []
    for idx, (_fnum, s_val) in enumerate(string_fields):
        if idx == role_idx:
            continue
        text_parts.append(s_val)
    text_parts.extend(nested_texts)

    if not role_val and nested_role:
        role_val = nested_role
    if not role_val and varint_role in (1, 2) and text_parts:
        role_val = "user" if varint_role == 1 else "assistant"
    if role_val in ("user", "assistant", "tool", "system") and (text_parts or role_val in ("tool", "system")):
        return {
            "role": role_val,
            "content": "\n".join(text_parts),
        }
    return None


def _collect_store_leaf_blobs(
    root_id: str,
    blob_map: dict[str, bytes],
    *,
    max_depth: int = 16,
) -> list[str]:
    """Recursively traverse intermediate index/node blobs in order with cycle guard to collect leaf message blobs."""
    known_ids = set(blob_map.keys())
    visited: set[str] = set()
    leaves: list[str] = []

    def _visit(blob_id: str, depth: int) -> None:
        if depth > max_depth or blob_id in visited or blob_id not in blob_map:
            return
        visited.add(blob_id)
        raw = blob_map[blob_id]
        if not raw:
            return
        if depth > 0:
            msg = _decode_store_blob_message(raw)
            if isinstance(msg, dict):
                leaves.append(blob_id)
                return
        child_refs = _extract_store_root_blob_refs(raw, known_ids=known_ids)
        if child_refs:
            for ref in child_refs:
                _visit(ref, depth + 1)
            return
        if depth == 0:
            msg = _decode_store_blob_message(raw)
            if isinstance(msg, dict):
                leaves.append(blob_id)

    _visit(root_id, 0)
    return leaves


def _extract_cursor_cli_user_text(raw_text: str) -> str:
    """Extract user prompt from Cursor CLI message text, unwrapping <user_query> and skipping system-injected headers."""
    if not raw_text:
        return ""
    matches = USER_QUERY_RE.findall(raw_text)
    if matches:
        return "\n\n".join(m.strip() for m in matches if m.strip()).strip()
    stripped = raw_text.strip()
    if stripped.startswith(("<user_info>", "<system_reminder>", "<manually_attached_skills>", "<dynamic_tools>")):
        cleaned = re.sub(
            r"<(user_info|agent_transcripts|dynamic_tools|manually_attached_skills|system_reminder|timestamp)>.*?</\1>",
            "",
            stripped,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        return cleaned
    return stripped


def _parse_cursor_store_db(cur: sqlite3.Cursor, resolved_path: Path) -> list[dict[str, Any]]:
    """Parse a Cursor CLI store.db (`meta` + `blobs` tables) into normalized session records."""
    meta_obj: dict[str, Any] = {}
    try:
        cur.execute("SELECT key, value FROM meta")
        for key, raw_val in cur:
            if not raw_val:
                continue
            decoded_str = ""
            if isinstance(raw_val, bytes):
                decoded_str = raw_val.decode("utf-8", errors="replace")
            elif isinstance(raw_val, str):
                raw_str = raw_val.strip()
                if raw_str.startswith("{"):
                    decoded_str = raw_str
                else:
                    try:
                        decoded_str = bytes.fromhex(raw_str).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        decoded_str = raw_str
            if decoded_str:
                try:
                    parsed = json.loads(decoded_str)
                    if isinstance(parsed, dict):
                        meta_obj.update(parsed)
                        if str(key) == "0":
                            break
                except json.JSONDecodeError:
                    continue
    except sqlite3.Error as exc:
        raise AgentAdapterError(
            host="cursor",
            code="unsupported_format",
            path=str(resolved_path),
            detail=f"Failed to read Cursor CLI store.db meta table: {exc}",
        ) from exc

    agent_id = str(meta_obj.get("agentId") or resolved_path.parent.name or resolved_path.stem).strip()
    root_blob_id = str(meta_obj.get("latestRootBlobId") or "").strip()
    raw_name = str(meta_obj.get("name") or "").strip()
    created_at_ms = meta_obj.get("createdAt")
    session_ts = ""
    if isinstance(created_at_ms, (int, float)) and created_at_ms > 0:
        try:
            val = float(created_at_ms)
            if val > 1e11:
                val /= 1000.0
            session_ts = datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
        except Exception:
            session_ts = ""

    cur.execute("SELECT id, data FROM blobs")
    blob_map: dict[str, bytes] = {}
    ordered_ids: list[str] = []
    for bid, data in cur:
        if bid is None or data is None:
            continue
        bid_str = str(bid)
        raw_bytes = bytes(data) if not isinstance(data, bytes) else data
        blob_map[bid_str] = raw_bytes
        ordered_ids.append(bid_str)

    candidate_ids: list[str] = []
    if root_blob_id and root_blob_id in blob_map:
        candidate_ids = _collect_store_leaf_blobs(root_blob_id, blob_map, max_depth=16)
    if not candidate_ids:
        candidate_ids = [bid for bid in ordered_ids if bid != root_blob_id]

    messages: list[dict[str, Any]] = []
    summary_items: list[str] = []
    summary_omitted = 0
    cwd = ""
    first_user_text = ""

    for bid in candidate_ids:
        raw_bytes = blob_map.get(bid)
        if not raw_bytes:
            continue
        msg_obj = _decode_store_blob_message(raw_bytes)
        if not isinstance(msg_obj, dict):
            continue

        role = str(msg_obj.get("role") or "").strip()
        content = msg_obj.get("content")
        raw_content_text = ""
        if isinstance(content, str):
            raw_content_text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    p_type = str(part.get("type") or "")
                    if p_type == "text" and part.get("text"):
                        parts.append(str(part["text"]))
                    elif p_type in ("tool-call", "tool_use", "toolCall"):
                        tool_name = str(part.get("toolName") or part.get("name") or "tool")[:MAX_SUMMARY_LENGTH]
                        if len(summary_items) < 50:
                            summary_items.append(f"- tool_call: {tool_name}")
                        else:
                            summary_omitted += 1
                    elif p_type == "reasoning":
                        if len(summary_items) < 50:
                            summary_items.append("- reasoning: recorded")
                        else:
                            summary_omitted += 1
            raw_content_text = "\n".join(parts)

        if not cwd and raw_content_text:
            ws_match = WORKSPACE_PATH_RE.search(raw_content_text)
            if ws_match:
                cwd = ws_match.group(1).strip()

        if role == "user":
            clean_user = _extract_cursor_cli_user_text(raw_content_text)
            if clean_user:
                if not first_user_text:
                    first_user_text = clean_user
                messages.append({
                    "external_id": f"cursor:cli:{agent_id}:{bid[:16]}",
                    "role": "user",
                    "content": clean_user,
                    "timestamp": session_ts,
                })
        elif role == "assistant":
            clean_assistant = raw_content_text.strip()
            if clean_assistant:
                messages.append({
                    "external_id": f"cursor:cli:{agent_id}:{bid[:16]}",
                    "role": "assistant",
                    "content": clean_assistant,
                    "timestamp": session_ts,
                })
        elif role == "tool":
            if len(summary_items) < 50:
                summary_items.append("- tool_result: recorded")
            else:
                summary_omitted += 1

    if not messages:
        raise AgentAdapterError(
            host="cursor",
            code="unsupported_format",
            path=str(resolved_path),
            detail="Cursor CLI store.db has no extractable user or assistant dialogues.",
        )

    if raw_name and raw_name.lower() not in ("new agent", "untitled", "new chat"):
        title = raw_name
    elif first_user_text:
        title = first_user_text.splitlines()[0][:120]
    else:
        title = raw_name or agent_id

    return [{
        "external_id": f"cursor:cli:{agent_id}",
        "title": title,
        "cwd": cwd,
        "started_at": session_ts,
        "updated_at": session_ts,
        "messages": messages,
        "process_summary": bound_process_summary(
            "\n".join(summary_items[:50]),
            truncated=summary_omitted > 0,
        ),
        "source_path": str(resolved_path),
    }]


def _lexical_to_text(node: Any) -> str:
    """Extract plain text from a Lexical editor JSON tree."""
    if isinstance(node, dict):
        if "root" in node:
            return _lexical_to_text(node["root"])
        node_type = node.get("type")
        if node_type == "text":
            return str(node.get("text") or "")
        if node_type == "linebreak":
            return "\n"
        children = node.get("children")
        if isinstance(children, list):
            return "".join(_lexical_to_text(child) for child in children)
        return ""
    if isinstance(node, list):
        return "".join(_lexical_to_text(child) for child in node)
    return ""


def _extract_bubble_text(bubble: dict[str, Any]) -> str:
    """Extract text from bubble text field or richText Lexical tree."""
    text = str(bubble.get("text") or "").strip()
    if text:
        return text
    rich_text = bubble.get("richText")
    if isinstance(rich_text, str) and rich_text.strip():
        try:
            return _lexical_to_text(json.loads(rich_text)).strip()
        except json.JSONDecodeError:
            return ""
    return ""


def _find_cursor_databases(roots: list[str | Path]) -> list[Path]:
    """Locate state.vscdb and Cursor CLI store.db database files under authorized roots."""
    deduped = deduplicate_roots(roots)
    databases: list[Path] = []
    seen: set[str] = set()

    for root_path in deduped:
        if root_path.is_file():
            if root_path.name in ("state.vscdb", "store.db") or root_path.suffix == ".vscdb":
                canon = str(root_path.resolve())
                if canon not in seen:
                    seen.add(canon)
                    databases.append(root_path)
        elif root_path.is_dir():
            # Check direct or subfolder state.vscdb / store.db
            for direct_name in ("state.vscdb", "store.db"):
                direct = root_path / direct_name
                if direct.is_file():
                    canon = str(direct.resolve())
                    if canon not in seen:
                        seen.add(canon)
                        databases.append(direct)
            storage = root_path / "User" / "globalStorage" / "state.vscdb"
            if storage.is_file():
                canon = str(storage.resolve())
                if canon not in seen:
                    seen.add(canon)
                    databases.append(storage)
            # General recursive search under root
            for pattern in ("state.vscdb", "store.db"):
                for candidate in root_path.rglob(pattern):
                    if candidate.is_file():
                        canon = str(candidate.resolve())
                        if canon not in seen:
                            seen.add(canon)
                            databases.append(candidate)
    return databases


def _parse_cursor_db(db_path: Path, roots: list[str | Path]) -> list[dict[str, Any]]:
    """Parse sessions from a validated Cursor state.vscdb or CLI store.db file."""
    resolved_path = validate_path(db_path, roots)
    with isolated_sqlite_snapshot(resolved_path, host="cursor") as staged_path:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(staged_path, timeout=2.0)
            conn.execute("PRAGMA mmap_size = 0")
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            if conn is not None:
                conn.close()
            raise AgentAdapterError(
                host="cursor",
                code="file_changed",
                path=str(resolved_path),
                detail=f"Cursor database was rewritten during open: {exc}",
                retryable=True,
            ) from exc
        except Exception as exc:
            if conn is not None:
                conn.close()
            raise AgentAdapterError(
                host="cursor",
                code="permission_denied",
                path=str(resolved_path),
                detail=f"Cannot open Cursor database in read-only mode: {exc}",
            ) from exc

        try:
            return _parse_cursor_db_conn(conn, resolved_path)
        except AgentAdapterError:
            raise
        except sqlite3.DatabaseError as exc:
            raise AgentAdapterError(
                host="cursor",
                code="file_changed",
                path=str(resolved_path),
                detail=f"Cursor database was rewritten during read: {exc}",
                retryable=True,
            ) from exc
        finally:
            conn.close()


def _parse_cursor_db_conn(conn: sqlite3.Connection, resolved_path: Path) -> list[dict[str, Any]]:
    conn.execute("BEGIN")
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur}

    if "blobs" in tables and "meta" in tables:
        return _parse_cursor_store_db(cur, resolved_path)

    if "cursorDiskKV" not in tables and "composerHeaders" not in tables:
        raise AgentAdapterError(
            host="cursor",
            code="unsupported_format",
            path=str(resolved_path),
            detail="Cursor SQLite database lacks required tables (composerHeaders / cursorDiskKV / blobs+meta).",
        )

    # 1. Load composer metadata from composerHeaders if present
    meta_by_id: dict[str, dict[str, Any]] = {}
    if "composerHeaders" in tables:
        try:
            cur.execute("SELECT composerId, value, isSubagent FROM composerHeaders")
            for cid, val, is_sub in cur:
                if is_sub:
                    meta_by_id[cid] = {"is_subagent": True}
                    del val
                    continue
                parsed_val: dict[str, Any] = {}
                if val:
                    try:
                        parsed_val = json.loads(val)
                    except json.JSONDecodeError:
                        parsed_val = {}

                cwd = ""
                ws = parsed_val.get("workspaceIdentifier")
                if isinstance(ws, dict):
                    uri = ws.get("uri")
                    if isinstance(uri, dict):
                        cwd = str(uri.get("fsPath") or "")

                title = str(parsed_val.get("name") or "").strip()
                created_at = parsed_val.get("createdAt") or 0
                last_updated = parsed_val.get("lastUpdatedAt") or 0

                meta_by_id[cid] = {
                    "is_subagent": False,
                    "title": title,
                    "cwd": cwd,
                    "created_at_ms": created_at,
                    "last_updated_ms": last_updated,
                }
                del parsed_val, val
        except sqlite3.OperationalError:
            pass

    # 2. Enumerate composers from bubble keys in cursorDiskKV
    sessions: list[dict[str, Any]] = []

    if "cursorDiskKV" in tables:
        cur.execute(
            "SELECT key FROM cursorDiskKV WHERE key >= ? AND key < ?",
            (BUBBLE_PREFIX, BUBBLE_RANGE_END),
        )
        composer_ids: list[str] = []
        seen_composers: set[str] = set()
        for (k,) in cur:
            parts = k.split(":")
            if len(parts) < 3:
                continue
            cid = parts[1]
            if cid in seen_composers or meta_by_id.get(cid, {}).get("is_subagent"):
                continue
            seen_composers.add(cid)
            composer_ids.append(cid)

        for cid in composer_ids:
            prefix = f"{BUBBLE_PREFIX}{cid}:"
            cur.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?",
                (prefix, f"{BUBBLE_PREFIX}{cid};"),
            )
            bubbles: list[tuple[str, dict[str, Any]]] = []
            for k, raw_v in cur:
                parts = k.split(":")
                if len(parts) < 3:
                    continue
                bid = parts[2]
                if raw_v is None:
                    continue
                if isinstance(raw_v, bytes):
                    raw_v = raw_v.decode("utf-8", errors="replace")
                try:
                    bubble_data = json.loads(raw_v)
                except json.JSONDecodeError:
                    continue
                if not isinstance(bubble_data, dict):
                    continue
                bubble_type = bubble_data.get("type")
                capability = bubble_data.get("capabilityType") or bubble_type
                compact = {
                    "type": bubble_type if isinstance(bubble_type, (int, str, bool)) else None,
                    "text": _extract_bubble_text(bubble_data) if bubble_type in (1, 2) else "",
                    "createdAt": str(bubble_data.get("createdAt") or ""),
                    "capabilityType": str(capability)[:MAX_SUMMARY_LENGTH] if capability else None,
                }
                bubbles.append((bid, compact))
                del bubble_data, raw_v

            header = meta_by_id.get(cid, {})
            messages: list[dict[str, Any]] = []
            summary_items: list[str] = []
            summary_omitted = 0
            first_user_text = ""
            started_at = ""
            updated_at = ""

            def _bubble_sort_key(item: tuple[str, dict[str, Any]]) -> str:
                return str(item[1].get("createdAt") or "")

            sorted_bubbles = sorted(bubbles, key=_bubble_sort_key)

            for bid, b in sorted_bubbles:
                b_type = b.get("type")
                text = _extract_bubble_text(b)
                ts = str(b.get("createdAt") or "")
                if ts:
                    if not started_at:
                        started_at = ts
                    updated_at = ts

                if b_type == 1 and text:
                    if not first_user_text:
                        first_user_text = text
                    messages.append({
                        "external_id": f"cursor:{cid}:{bid}",
                        "role": "user",
                        "content": text,
                        "timestamp": ts,
                    })
                elif b_type == 2 and text:
                    messages.append({
                        "external_id": f"cursor:{cid}:{bid}",
                        "role": "assistant",
                        "content": text,
                        "timestamp": ts,
                    })
                else:
                    cap = b.get("capabilityType") or b.get("type")
                    if cap:
                        if len(summary_items) < 50:
                            summary_items.append(f"- step: {cap}")
                        else:
                            summary_omitted += 1

            if not messages:
                continue

            session_title = header.get("title") or (first_user_text.splitlines()[0][:120] if first_user_text else cid)
            if not session_title:
                session_title = cid

            sessions.append({
                "external_id": f"cursor:{cid}",
                "title": session_title,
                "cwd": header.get("cwd", ""),
                "started_at": started_at,
                "updated_at": updated_at or started_at,
                "messages": messages,
                "process_summary": bound_process_summary(
                    "\n".join(summary_items[:50]),
                    truncated=summary_omitted > 0,
                ),
                "source_path": str(resolved_path),
            })

    if not sessions:
        raise AgentAdapterError(
            host="cursor",
            code="unsupported_format",
            path=str(resolved_path),
            detail="Cursor database has no extractable user or assistant dialogues.",
        )

    return sessions


def read_cursor_sessions(roots: list[str | Path], *, source_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """Scan authorized roots and return parsed Cursor sessions."""
    if not roots:
        raise AgentAdapterError(
            host="cursor",
            code="root_not_granted",
            detail="No authorized roots provided.",
        )

    if source_paths is None:
        db_files = _find_cursor_databases(roots)
    else:
        db_files = [Path(p).expanduser() for p in source_paths
                    if Path(p).name in ("state.vscdb", "store.db") or Path(p).suffix == ".vscdb"]
    if not db_files:
        raise AgentAdapterError(
            host="cursor",
            code="root_not_granted",
            path=str(roots[0]),
            detail="No Cursor state.vscdb or store.db database files were found under granted roots.",
        )

    all_sessions: list[dict[str, Any]] = []
    failures = []
    seen_external_ids: set[str] = set()

    for db_path in db_files:
        try:
            sessions = _parse_cursor_db(db_path, roots)
        except (AgentAdapterError, sqlite3.Error) as exc:
            failures.append({"path": str(db_path), "code": getattr(exc, "code", "unsupported_format"), "detail": str(exc)})
            continue
        for s in sessions:
            eid = s["external_id"]
            if eid not in seen_external_ids:
                seen_external_ids.add(eid)
                all_sessions.append(s)

    if failures:
        from .base import PartialHistoryError
        if all_sessions:
            raise PartialHistoryError(host="cursor", code="partial_sync", sessions=all_sessions, failures=failures, detail="Some databases could not be read")
        first = failures[0]
        raise AgentAdapterError(host="cursor", code=first["code"], path=first["path"], detail=first["detail"])
    return all_sessions


def normalize_cursor_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize official Cursor hook payloads and document known vendor limitations.
    Records concrete field availability and known limitations.
    """
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    raw_event = (
        payload.get("event")
        or payload.get("event_name")
        or inner.get("event")
        or inner.get("event_name")
        or "unknown"
    )

    if raw_event in ("prompt", "beforeSubmitPrompt"):
        event_name = "beforeSubmitPrompt"
    elif raw_event in ("assistant", "afterAgentResponse"):
        event_name = "afterAgentResponse"
    elif raw_event in ("tool", "postToolUse"):
        event_name = "postToolUse"
    elif raw_event in ("session-start", "sessionStart"):
        event_name = "sessionStart"
    elif raw_event in ("stop", "Stop"):
        event_name = "stop"
    elif raw_event in ("end", "sessionEnd"):
        event_name = "sessionEnd"
    else:
        event_name = raw_event

    conversation_id = (
        payload.get("conversation_id")
        or payload.get("session_id")
        or inner.get("conversation_id")
        or inner.get("session_id")
    )
    if not conversation_id:
        raise AgentAdapterError(host="cursor", code="unsupported_format", detail="Hook requires a stable conversation ID")
    generation_id = payload.get("generation_id") or inner.get("generation_id")
    timestamp = payload.get("timestamp") or inner.get("timestamp") or ""

    record: dict[str, Any] = {
        "event_id": f"cursor:{conversation_id}:{event_name}:{generation_id}",
        "host": "cursor",
        "event": event_name,
        "event_name": event_name,
        "conversation_id": conversation_id,
        "external_id": f"cursor:{conversation_id}",
        "title": payload.get("title") or f"Cursor {conversation_id}",
        "cwd": payload.get("cwd") or inner.get("cwd"),
        "timestamp": timestamp,
        "messages": [],
        "metadata": {
            "source": "official_hook",
            "raw_event": raw_event,
            "limitations": [],
        },
    }

    if event_name == "sessionStart":
        record["metadata"]["limitations"].append(
            "sessionStart additional_context is fire-and-forget; agent loop does not wait."
        )

    elif event_name == "beforeSubmitPrompt":
        prompt = payload.get("prompt") or inner.get("prompt") or ""
        if prompt:
            record["messages"].append({
                "external_id": f"cursor:{conversation_id}:prompt:{generation_id}" if generation_id else None,
                "role": "user",
                "content": prompt,
                "timestamp": timestamp,
            })
        record["metadata"]["limitations"].append(
            "beforeSubmitPrompt hook cannot inject developer context; observer only."
        )

    elif event_name == "afterAgentResponse":
        text = payload.get("text") or inner.get("text") or ""
        if text:
            record["messages"].append({
                "external_id": f"cursor:{conversation_id}:response:{generation_id}" if generation_id else None,
                "role": "assistant",
                "content": text,
                "timestamp": timestamp,
            })

    elif event_name == "postToolUse":
        tool_name = payload.get("tool_name") or inner.get("tool_name") or "unknown_tool"
        record["metadata"]["tool_call"] = {
            "name": tool_name,
            "status": payload.get("status") or inner.get("status") or "completed",
            "duration_ms": payload.get("duration_ms") or inner.get("duration_ms"),
        }
        record["metadata"]["limitations"].append(
            "postToolUse cannot block, retry, or modify tool output."
        )

    elif event_name in ("stop", "sessionEnd"):
        record["metadata"]["final_status"] = payload.get("final_status") or payload.get("status")
        record["metadata"]["duration_ms"] = payload.get("duration_ms")

    return record
