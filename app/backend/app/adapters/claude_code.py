"""ContextCore Claude Code adapter.

Reads Claude Code project transcripts under ``~/.claude/projects/<encoded-cwd>/<session>.jsonl``.
The filename stem is the session identity authority; record-level ``sessionId`` may differ after
resume/rewind and never overwrites the owning session. Branches are rebuilt from the last valid
message leaf through ``parentUuid`` so abandoned rewinds do not leak into the conversation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .base import (
    usage_metadata_record,
    iter_complete_jsonl,
    AgentAdapterError,
    PartialHistoryError,
    bound_process_summary,
    validate_file_safety,
    validate_path,
)

CLAUDE_KNOWN_META_TYPES = frozenset({
    "summary",
    "system",
    "file-history-snapshot",
    "queue-operation",
    "permission-mode",
    "attachment",
    "last-prompt",
    "custom-title",
    "agent-name",
    "compact_boundary",
})
CLAUDE_MESSAGE_TYPES = frozenset({"user", "assistant"})
MAX_SUMMARY = 2048


def _iso(value: Any, fallback: str = "") -> str:
    text = str(value or "").strip()
    return text or fallback


def _text_from_content(content: Any) -> tuple[str, list[str], int]:
    """Return (prose, process lines, thinking block count)."""
    if isinstance(content, str):
        return content.strip(), [], 0
    prose: list[str] = []
    process: list[str] = []
    thinking = 0
    if not isinstance(content, list):
        return "", [], 0
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "text":
            text = str(block.get("text") or "").strip()
            if text:
                prose.append(text)
        elif kind == "thinking":
            thinking += 1
        elif kind == "tool_use":
            name = str(block.get("name") or "tool")
            payload = block.get("input")
            keys = ", ".join(list(payload)[:4]) if isinstance(payload, dict) else ""
            process.append(f"- **{name}** [call]: {keys or 'input recorded'}"[:200])
        elif kind == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(str(item.get("text") or "") for item in body if isinstance(item, dict))
            status = "error" if block.get("is_error") else "ok"
            process.append(f"- **tool** [{status}]: {str(body or '')[:150]}")
        elif kind in {"image", "document"}:
            process.append(f"- **{kind}** [attachment]")
    return "\n".join(prose).strip(), process, thinking


def _message_record(entry: dict[str, Any], stem: str, index: int) -> dict[str, Any] | None:
    if entry.get("isSidechain") is True or entry.get("isMeta") is True:
        return None
    entry_type = str(entry.get("type") or "")
    message = entry.get("message")
    role = str(message.get("role") or "") if isinstance(message, dict) else ""
    if entry_type not in CLAUDE_MESSAGE_TYPES and role not in CLAUDE_MESSAGE_TYPES:
        return None
    role = role or entry_type
    if role not in CLAUDE_MESSAGE_TYPES:
        return None
    content = message.get("content") if isinstance(message, dict) else entry.get("content")
    prose, process, thinking = _text_from_content(content)
    # A user record that only carries tool results is not user speech.
    if role == "user" and not prose and process:
        return None
    if not prose:
        return None
    message_id = str(entry.get("uuid") or f"{stem}:{index}")
    return {
        "external_id": f"claude-code:{stem}:{message_id}",
        "role": role,
        "content": prose,
        "timestamp": _iso(entry.get("timestamp")),
        "_process": process,
        "_thinking": thinking,
    }


def _leaf_chain(entries: list[dict[str, Any]], stem: str) -> list[dict[str, Any]]:
    """Walk parentUuid from the last valid message back to its root."""
    by_uuid = {str(entry.get("uuid")): entry for entry in entries if entry.get("uuid")}
    def find_leaf(require_stem: bool) -> dict[str, Any] | None:
        for entry in reversed(entries):
            if entry.get("isSidechain") is True or entry.get("isMeta") is True:
                continue
            record_session = str(entry.get("sessionId") or "")
            if require_stem and record_session and record_session != stem:
                continue
            if str(entry.get("type") or "") in CLAUDE_MESSAGE_TYPES or entry.get("_has_message") is True:
                return entry
        return None

    leaf = find_leaf(True) or find_leaf(False)
    if leaf is None:
        return []
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = leaf
    while isinstance(cursor, dict):
        uid = str(cursor.get("uuid") or "")
        if uid and uid in seen:
            break
        if uid:
            seen.add(uid)
        chain.append(cursor)
        parent = cursor.get("parentUuid")
        if not isinstance(parent, str) or not parent:
            break
        cursor = by_uuid.get(parent)
    chain.reverse()
    return chain


def _parse_file(path: Path, roots: list[str | Path]) -> dict[str, Any] | None:
    validate_file_safety(path, roots)
    def identity(stat):
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    before = path.stat()
    usage_observations: list[dict[str, Any]] = []
    stem = path.stem
    title = ""
    saw_line = False
    with path.open("rb", buffering=8192) as source_file:
        bom = source_file.read(3) == b"\xef\xbb\xbf"
    index = sqlite3.connect("")
    try:
        index.execute("PRAGMA journal_mode=OFF")
        index.execute("PRAGMA synchronous=OFF")
        index.execute("PRAGMA cache_size=-256")
        index.execute("PRAGMA temp_store=FILE")
        index.execute("""CREATE TABLE entries(
            position INTEGER PRIMARY KEY, uuid TEXT, parent_uuid TEXT,
            session_id TEXT, type TEXT, sidechain INTEGER, meta INTEGER,
            has_message INTEGER, cwd TEXT, timestamp TEXT, byte_offset INTEGER)""")
        index.execute("CREATE TABLE uuid_positions(uuid TEXT PRIMARY KEY, position INTEGER)")
        index.execute("CREATE TABLE active(position INTEGER PRIMARY KEY, depth INTEGER)")
        index.execute("CREATE TABLE seen(uuid TEXT PRIMARY KEY)")
        index.execute("CREATE TABLE unknown_types(value TEXT PRIMARY KEY)")
        index.execute("CREATE TABLE mismatched_sessions(value TEXT PRIMARY KEY)")
        byte_offset = 0
        for position, line in enumerate(iter_complete_jsonl(path, encoding="utf-8-sig", host="claude-code")):
            if not line.strip():
                byte_offset += len(line.encode("utf-8")) + (3 if position == 0 and bom else 0)
                continue
            saw_line = True
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                raise AgentAdapterError(host="claude-code", code="unsupported_format", path=str(path), detail="Malformed JSONL record") from None
            if not isinstance(entry, dict):
                byte_offset += len(line.encode("utf-8")) + (3 if position == 0 and bom else 0)
                continue
            entry_type = str(entry.get("type") or "")
            if entry_type not in CLAUDE_MESSAGE_TYPES and entry_type not in CLAUDE_KNOWN_META_TYPES:
                index.execute("INSERT OR IGNORE INTO unknown_types VALUES(?)", (entry_type or "unknown",))
            record_session = entry.get("sessionId")
            if record_session and str(record_session) != stem:
                index.execute("INSERT OR IGNORE INTO mismatched_sessions VALUES(?)", (str(record_session),))
            usage = usage_metadata_record(entry)
            if usage is not None:
                usage_observations.append(usage)
            if not title and entry_type == "summary" and entry.get("summary"):
                title = str(entry["summary"]).strip()[:120]
            uuid = str(entry["uuid"]) if entry.get("uuid") else None
            parent_uuid = str(entry["parentUuid"]) if isinstance(entry.get("parentUuid"), str) else None
            index.execute("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                position, uuid, parent_uuid,
                str(record_session) if record_session else "", entry_type,
                int(entry.get("isSidechain") is True), int(entry.get("isMeta") is True),
                int(isinstance(entry.get("message"), dict)),
                _iso(entry.get("cwd")), _iso(entry.get("timestamp")), byte_offset,
            ))
            if uuid:
                index.execute("INSERT OR REPLACE INTO uuid_positions VALUES(?,?)", (uuid, position))
            byte_offset += len(line.encode("utf-8")) + (3 if position == 0 and bom else 0)
        index.commit()
    except AgentAdapterError:
        index.close()
        raise
    except (OSError, UnicodeError) as error:
        index.close()
        raise AgentAdapterError(host="claude-code", code="permission_denied", path=str(path), detail=str(error)) from error
    except Exception:
        index.close()
        raise
    after_index = path.stat()
    if identity(before) != identity(after_index):
        index.close()
        raise AgentAdapterError(host="claude-code", code="file_changed", path=str(path), detail="Source changed while indexing", retryable=True)
    if not saw_line:
        index.close()
        raise AgentAdapterError(host="claude-code", code="unsupported_format", path=str(path), detail="Empty transcript")

    leaf = index.execute("""SELECT position FROM entries WHERE sidechain=0 AND meta=0
        AND (session_id='' OR session_id=?)
        AND (type IN ('user','assistant') OR has_message=1)
        ORDER BY position DESC LIMIT 1""", (stem,)).fetchone()
    if leaf is None:
        leaf = index.execute("""SELECT position FROM entries WHERE sidechain=0 AND meta=0
            AND (type IN ('user','assistant') OR has_message=1)
            ORDER BY position DESC LIMIT 1""").fetchone()
    current = leaf[0] if leaf else None
    depth = 0
    while current is not None:
        row = index.execute("SELECT uuid,parent_uuid FROM entries WHERE position=?", (current,)).fetchone()
        if row is None:
            break
        uuid, parent_uuid = row
        if uuid:
            if index.execute("SELECT 1 FROM seen WHERE uuid=?", (uuid,)).fetchone():
                break
            index.execute("INSERT INTO seen VALUES(?)", (uuid,))
        index.execute("INSERT INTO active VALUES(?,?)", (current, depth))
        current = (index.execute("SELECT position FROM uuid_positions WHERE uuid=?", (parent_uuid,)).fetchone() or (None,))[0] if parent_uuid else None
        depth += 1
    index.commit()

    try:
        messages: list[dict[str, Any]] = []
        cwd = ""
        started = ""
        updated = ""
        thinking = 0
        process_summary = ""
        with path.open("rb", buffering=8192) as source_file:
            chain_rows = index.execute("""SELECT e.position,e.byte_offset FROM active a
                JOIN entries e ON e.position=a.position ORDER BY a.depth DESC""")
            for position, offset in chain_rows:
                source_file.seek(offset)
                raw = source_file.readline(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024 or not raw.endswith(b"\n"):
                    raise AgentAdapterError(host="claude-code", code="file_changed", path=str(path), detail="Active record changed during projection", retryable=True)
                try:
                    line = raw.decode("utf-8-sig" if offset == 0 else "utf-8")
                except UnicodeDecodeError as error:
                    raise AgentAdapterError(host="claude-code", code="unsupported_format", path=str(path), detail="Active record is not valid UTF-8") from error
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    raise AgentAdapterError(host="claude-code", code="unsupported_format", path=str(path), detail="Malformed JSONL record") from None
                if not isinstance(entry, dict):
                    continue
                record = _message_record(entry, stem, position)
                line_process: list[str] = []
                line_thinking = 0
                if record is None and entry.get("isSidechain") is not True and entry.get("isMeta") is not True:
                    message = entry.get("message")
                    content = message.get("content") if isinstance(message, dict) else entry.get("content")
                    _, line_process, line_thinking = _text_from_content(content)
                elif record is not None:
                    line_process = record.pop("_process", [])
                    line_thinking = int(record.pop("_thinking", 0) or 0)
                    messages.append(record)
                cwd = cwd or _iso(entry.get("cwd"))
                timestamp = _iso(entry.get("timestamp"))
                if timestamp:
                    started = started or timestamp
                    updated = timestamp
                thinking += line_thinking
                for fragment in line_process:
                    if len(process_summary) >= MAX_SUMMARY:
                        break
                    addition = ("\n" if process_summary else "") + fragment
                    process_summary += addition[:MAX_SUMMARY - len(process_summary)]
    except AgentAdapterError:
        index.close()
        raise
    except (OSError, UnicodeError) as error:
        index.close()
        raise AgentAdapterError(host="claude-code", code="permission_denied", path=str(path), detail=str(error)) from error
    after_parse = path.stat()
    if identity(before) != identity(after_parse):
        index.close()
        raise AgentAdapterError(host="claude-code", code="file_changed", path=str(path), detail="Source changed while parsing", retryable=True)

    unknown_count = index.execute("SELECT COUNT(*) FROM unknown_types").fetchone()[0]
    unknown = [row[0] for row in index.execute("SELECT value FROM unknown_types ORDER BY value LIMIT 5")]
    mismatch_count = index.execute("SELECT COUNT(*) FROM mismatched_sessions").fetchone()[0]

    if not messages and not usage_observations:
        if unknown_count:
            index.close()
            raise AgentAdapterError(
                host="claude-code",
                code="unsupported_format",
                path=str(path),
                detail=f"No readable messages; unknown record types: {', '.join(unknown)}",
            )
        index.close()
        return None

    if not title:
        first_user = next((message["content"] for message in messages if message["role"] == "user"), "")
        title = (first_user.splitlines()[0][:120] if first_user else f"Claude Code {stem}")
    if thinking:
        thinking_line = f"- **thinking** [count]: {thinking}"
        if len(process_summary) < MAX_SUMMARY:
            addition = ("\n" if process_summary else "") + thinking_line
            process_summary += addition[:MAX_SUMMARY - len(process_summary)]
    if mismatch_count:
        identity_line = f"- **identity** [warn]: {mismatch_count} record sessionId(s) differ from the file name; the file name remains authoritative"
        if len(process_summary) < MAX_SUMMARY:
            addition = ("\n" if process_summary else "") + identity_line
            process_summary += addition[:MAX_SUMMARY - len(process_summary)]

    updated = updated or started
    result = {
        "external_id": f"claude-code:{stem}",
        "title": title,
        "cwd": cwd,
        "started_at": started,
        "updated_at": updated,
        "messages": messages,
        "process_summary": bound_process_summary(process_summary, limit=MAX_SUMMARY, truncated=len(process_summary) >= MAX_SUMMARY),
        "source_path": str(path),
        "usage_observations": usage_observations,
    }
    index.close()
    return result


def _iter_transcripts(roots: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        if root.is_file() and root.suffix == ".jsonl":
            files.append(root)
        elif root.is_dir():
            target = root / "projects" if root.name == ".claude" and (root / "projects").is_dir() else root
            files.extend(sorted(target.rglob("*.jsonl")))
    return files


def read_claude_code_sessions(roots: list[str], *, source_paths: list[str] | None = None) -> list[dict[str, Any]]:
    if not roots:
        raise AgentAdapterError(host="claude-code", code="root_not_granted", detail="No authorized Claude Code root")
    normalized = [Path(root).expanduser() for root in roots]
    files = (_iter_transcripts(roots) if source_paths is None else
             [Path(p).expanduser() for p in source_paths if Path(p).suffix == ".jsonl"])
    if not files:
        raise AgentAdapterError(host="claude-code", code="root_not_granted", detail="No Claude Code transcripts found")
    sessions: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen_paths = set()
    for path in files:
        if str(path.absolute()) in seen_paths:
            continue
        seen_paths.add(str(path.absolute()))
        try:
            validate_path(path, normalized)
            parsed = _parse_file(path, normalized)
            if parsed:
                sessions.append(parsed)
        except AgentAdapterError as error:
            failures.append({"path": str(path), "code": error.code, "detail": error.detail})
        except OSError as error:
            failures.append({"path": str(path), "code": "permission_denied", "detail": str(error)})
    if sessions and failures:
        raise PartialHistoryError(host="claude-code", code="partial_sync", detail="Some source records could not be read", sessions=sessions, failures=failures)
    if failures and not sessions:
        first = failures[0]
        raise AgentAdapterError(host="claude-code", code=first["code"], path=first["path"], detail=first["detail"])
    return sessions


def normalize_claude_code_hook(payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("event") or payload.get("hook_event_name") or "unknown")
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
    transcript = str(payload.get("transcript_path") or "").strip()
    if not session_id:
        raise AgentAdapterError(host="claude-code", code="unsupported_format", detail="Hook requires a stable session ID")
    stem = Path(transcript).stem if transcript else session_id
    record: dict[str, Any] = {
        "host": "claude-code",
        "event": event,
        "session_id": session_id,
        "external_id": f"claude-code:{stem}",
        "title": f"Claude Code {stem}",
        "cwd": payload.get("cwd"),
        "source_path": transcript or None,
        "timestamp": _iso(payload.get("timestamp")),
        "messages": [],
        "process_summary": [],
        "metadata": {},
    }
    if event == "SessionStart":
        record["metadata"]["source"] = payload.get("source")
    elif event == "UserPromptSubmit":
        prompt = str(payload.get("prompt") or "").strip()
        if prompt:
            message_id = str(payload.get("prompt_id") or "prompt")
            record["messages"].append({
                "external_id": f"claude-code:{stem}:{message_id}",
                "role": "user",
                "content": prompt,
                "timestamp": record["timestamp"],
            })
    elif event == "Stop":
        reply = str(payload.get("last_assistant_message") or "").strip()
        if reply:
            record["messages"].append({
                "external_id": f"claude-code:{stem}:stop",
                "role": "assistant",
                "content": reply,
                "timestamp": record["timestamp"],
            })
        else:
            record["metadata"]["lifecycle_event"] = "Stop"
    elif event in {"PostToolUse", "SessionEnd"}:
        record["metadata"]["lifecycle_event"] = event
    return record
