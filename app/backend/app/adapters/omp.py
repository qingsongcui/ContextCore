"""
ContextCore OMP Adapter - Strictly parses official Oh My Pi JSONL session v3 tree format.

Supports:
- Optional 256-byte fixed title slot at file start
- Header line validation (version <= 3)
- ParentId tree backtracking from active leaf (prevents mixing abandoned branches)
- User / assistant message extraction
- Compaction, reset_boundary, and tool output summarization (no raw dumps)
- Path traversal and symlink boundary enforcement
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .base import (
    usage_metadata_record,
    iter_complete_jsonl,
    MAX_LINE_LENGTH,
    MAX_SUMMARY_LENGTH,
    bound_process_summary,
    AgentAdapterError,
    PartialHistoryError,
    deduplicate_roots,
    validate_file_safety,
    validate_path,
)



def _project_active_entry(entry: dict[str, Any], session_id: str, *, fallback_id: str | None = None) -> dict[str, Any]:
    """Normalize one active entry without keeping its raw tool metadata."""
    kind = entry.get("type")
    projected: dict[str, Any] = {"type": kind, "timestamp": entry.get("timestamp")}
    process_text = ""

    def add_process(line: str) -> None:
        nonlocal process_text
        if len(process_text) >= MAX_SUMMARY_LENGTH:
            return
        prefix = "\n" if process_text else ""
        available = MAX_SUMMARY_LENGTH - len(process_text) - len(prefix)
        process_text += prefix + line[:max(0, available)]

    if kind == "title_change":
        projected["title"] = entry.get("title")
    elif kind == "compaction":
        summary = entry.get("summary") or "Context compacted"
        add_process(f"- **compaction** [ok]: Context compacted: {str(summary)[:300]} (tokens before: {entry.get('tokensBefore', 0)}, kept from: {entry.get('firstKeptEntryId', '')})")
    elif kind == "reset_boundary":
        add_process("- **reset_boundary** [ok]: Reset boundary marked; prior messages preserved in archive.")
    elif kind == "message":
        msg_obj = entry.get("message")
        if not isinstance(msg_obj, dict):
            msg_obj = entry
        role = msg_obj.get("role") or entry.get("role")
        content = msg_obj.get("content") or entry.get("content") or ""
        msg_id = entry.get("id") or fallback_id or "msg"
        if role == "user":
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") in ("text", "input"):
                            parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "\n".join(parts)
            else:
                text = ""
            projected["message"] = {"external_id": f"omp:{session_id}:{msg_id}", "role": "user", "content": text}
        elif role == "assistant":
            parts: list[str] = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(block.get("text", ""))
                        elif block.get("type") == "toolCall":
                            tool = block.get("name", "unknown_tool")
                            args_repr = str(block.get("arguments", {}))[:150]
                            add_process(f"- **{tool}** [pending]: Called tool {tool}({args_repr})")
                    elif isinstance(block, str):
                        parts.append(block)
            projected["message"] = {"external_id": f"omp:{session_id}:{msg_id}", "role": "assistant", "content": "\n".join(parts)}
        elif role == "toolResult":
            tool = msg_obj.get("toolName") or entry.get("toolName") or "tool"
            status = "error" if msg_obj.get("isError") or entry.get("isError") else "ok"
            result = msg_obj.get("content") or entry.get("content") or ""
            if isinstance(result, str):
                short = result.strip().split("\n")[0][:150]
            elif isinstance(result, list):
                first = next((block.get("text", "") for block in result if isinstance(block, dict)), "")
                short = str(first).strip().split("\n")[0][:150]
            elif isinstance(result, dict):
                short = json.dumps(result, ensure_ascii=False)[:150]
            else:
                short = ""
            add_process(f"- **{tool}** [{status}]: Tool {tool} returned [{status}]: {short}")
        elif role in ("bashExecution", "pythonExecution"):
            tool = "bash" if role == "bashExecution" else "python"
            command = msg_obj.get("command") or entry.get("command") or ""
            code = msg_obj.get("exitCode", 0)
            status = "ok" if code == 0 else f"exit_{code}"
            add_process(f"- **{tool}** [{status}]: {tool} exec: {str(command)[:120]} (status: {status})")
    if process_text:
        projected["process"] = process_text
    return projected


def parse_omp_session_file(file_path: Path, roots: list[str | Path]) -> dict[str, Any]:
    """Disk-index compact parent metadata, then read active records by file offset."""
    resolved_path = validate_path(file_path, roots)
    validate_file_safety(resolved_path, host="omp")
    identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    before = resolved_path.stat()
    slot_title: str | None = None
    offset = 0
    try:
        with resolved_path.open("rb") as handle:
            sample = handle.read(256)
        if len(sample) == 256:
            try:
                slot = json.loads(sample.decode("utf-8").strip())
                if isinstance(slot, dict) and slot.get("type") == "title":
                    slot_title = slot.get("title")
                    offset = 256
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
    except OSError as exc:
        raise AgentAdapterError(host="omp", code="permission_denied", path=str(resolved_path), detail=str(exc)) from exc

    lines = iter(iter_complete_jsonl(resolved_path, offset=offset, host="omp", max_line_length=MAX_LINE_LENGTH))
    byte_offset = offset
    try:
        first_raw = next(lines)
        first = first_raw.strip()
        byte_offset += len(first_raw.encode("utf-8"))
    except StopIteration:
        raise AgentAdapterError(host="omp", code="unsupported_format", path=str(resolved_path), detail="Session file is empty.") from None
    try:
        parsed_first = json.loads(first)
    except json.JSONDecodeError as exc:
        raise AgentAdapterError(host="omp", code="unsupported_format", path=str(resolved_path), detail=f"Header is not valid JSON: {exc}") from exc

    header_line_count = 1
    if isinstance(parsed_first, dict) and parsed_first.get("type") == "title":
        slot_title = parsed_first.get("title")
        try:
            header_raw = next(lines)
            header_line = header_raw.strip()
            byte_offset += len(header_raw.encode("utf-8"))
        except StopIteration:
            raise AgentAdapterError(host="omp", code="unsupported_format", path=str(resolved_path), detail="Session file missing session header after title line.") from None
        header_line_count = 2
        try:
            header = json.loads(header_line)
        except json.JSONDecodeError as exc:
            raise AgentAdapterError(host="omp", code="unsupported_format", path=str(resolved_path), detail=f"Session header is not valid JSON: {exc}") from exc
    else:
        header = parsed_first
    if not isinstance(header, dict) or header.get("type") != "session":
        found = header.get("type") if isinstance(header, dict) else type(header).__name__
        raise AgentAdapterError(host="omp", code="unsupported_format", path=str(resolved_path), detail=f"Expected type 'session' in header, found '{found}'.")
    version = header.get("version", 1)
    if not isinstance(version, int) or version > 3:
        raise AgentAdapterError(host="omp", code="schema_version_unsupported", path=str(resolved_path), detail=f"OMP session schema version {version} is unsupported (max supported: 3).")

    session_id = str(header.get("id") or resolved_path.stem)
    cwd = header.get("cwd") or ""
    started_at = header.get("timestamp") or ""
    is_subagent = bool(header.get("parentSession"))
    explicit_title = (slot_title or header.get("title") or "").strip()
    title = explicit_title or "Untitled Session"
    def parse_json_record(raw: str, number: int) -> dict[str, Any]:
        try:
            value = json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            raise AgentAdapterError(host="omp", code="unsupported_format", path=str(resolved_path), detail=f"Invalid JSON at line {number}: {exc}") from exc
        if not isinstance(value, dict):
            raise AgentAdapterError(host="omp", code="unsupported_format", path=str(resolved_path), detail=f"Record at line {number} is not an object")
        return value

    # Empty-name SQLite databases are temporary disk databases that SQLite
    # deletes on close. A small page cache keeps large branch indexes off heap.
    index = sqlite3.connect("")
    try:
        index.execute("PRAGMA journal_mode=OFF")
        index.execute("PRAGMA synchronous=OFF")
        index.execute("PRAGMA cache_size=-256")
        index.execute("PRAGMA temp_store=FILE")
        index.execute("CREATE TABLE entries(id TEXT PRIMARY KEY, parent_id TEXT, line INTEGER, byte_offset INTEGER, usage TEXT)")
        index.execute("CREATE TABLE active(id TEXT PRIMARY KEY, depth INTEGER)")
        previous_id: Any = None
        leaf_id: Any = None
        line_number = header_line_count
        for raw in lines:
            line_number += 1
            current_offset = byte_offset
            byte_offset += len(raw.encode("utf-8"))
            if not raw.strip():
                continue
            entry = parse_json_record(raw, line_number)
            entry_id = entry.get("id")
            if not entry_id:
                entry_id = f"v1_entry_{line_number}"
                entry["id"] = entry_id
                if previous_id and "parentId" not in entry:
                    entry["parentId"] = previous_id
            entry_key = json.dumps(entry_id, ensure_ascii=False, separators=(",", ":"))
            parent_value = entry.get("parentId")
            parent_key = json.dumps(parent_value, ensure_ascii=False, separators=(",", ":")) if parent_value is not None else None
            usage = usage_metadata_record(entry)
            usage_json = json.dumps(usage, ensure_ascii=False, separators=(",", ":")) if usage else None
            index.execute("INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?)",
                          (entry_key, parent_key, line_number, current_offset, usage_json))
            previous_id = entry_id
            leaf_id = entry_key
        index.commit()

        after_index = resolved_path.stat()
        if identity(before) != identity(after_index):
            raise AgentAdapterError(host="omp", code="file_changed", path=str(resolved_path), detail="Source changed while indexing; retry a stable snapshot", retryable=True)

        current = leaf_id
        depth = 0
        while current is not None:
            row = index.execute("SELECT parent_id FROM entries WHERE id=?", (current,)).fetchone()
            if row is None:
                break
            inserted = index.execute("INSERT OR IGNORE INTO active VALUES(?,?)", (current, depth)).rowcount
            if not inserted:
                break
            current = row[0]
            depth += 1
        index.commit()

        messages: list[dict[str, Any]] = []
        formatted_summary = ""
        overflow = False
        updated_at = started_at
        with resolved_path.open("rb") as source_file:
            active_rows = index.execute("""SELECT e.line,e.byte_offset FROM active a
                JOIN entries e ON e.id=a.id ORDER BY a.depth DESC""")
            for active_line, active_offset in active_rows:
                source_file.seek(active_offset)
                raw_bytes = source_file.readline(MAX_LINE_LENGTH + 1)
                if len(raw_bytes) > MAX_LINE_LENGTH or not raw_bytes.endswith(b"\n"):
                    raise AgentAdapterError(host="omp", code="file_changed", path=str(resolved_path), detail="Active record changed during projection", retryable=True)
                try:
                    raw = raw_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise AgentAdapterError(host="omp", code="unsupported_format", path=str(resolved_path), detail=f"Invalid UTF-8 at line {active_line}") from exc
                active_entry = parse_json_record(raw, active_line)
                fallback_id = f"v1_entry_{active_line}" if not active_entry.get("id") else None
                entry = _project_active_entry(active_entry, session_id, fallback_id=fallback_id)
                timestamp = entry.get("timestamp") or updated_at
                if timestamp:
                    updated_at = timestamp
                if entry.get("type") == "title_change" and entry.get("title"):
                    explicit_title = str(entry["title"]).strip()
                    if explicit_title:
                        title = explicit_title
                message = entry.get("message")
                if message is not None:
                    message["timestamp"] = timestamp
                    messages.append(message)
                process = entry.get("process")
                if process:
                    fragment = ("\n" if formatted_summary else "") + process
                    remaining = MAX_SUMMARY_LENGTH - len(formatted_summary)
                    if remaining > 0:
                        formatted_summary += fragment[:remaining]
                    if len(fragment) > remaining:
                        overflow = True

        after_parse = resolved_path.stat()
        if identity(before) != identity(after_parse):
            raise AgentAdapterError(host="omp", code="file_changed", path=str(resolved_path), detail="Source changed while parsing; retry a stable snapshot", retryable=True)
        if overflow:
            formatted_summary = bound_process_summary(formatted_summary, truncated=True)
        usage_observations = [json.loads(row[0]) for row in index.execute(
            "SELECT usage FROM entries WHERE usage IS NOT NULL ORDER BY line")]
        project_name = os.path.basename(cwd.rstrip("/\\")) if cwd else None
        if not explicit_title and messages:
            import re as _re
            for msg in messages:
                if msg.get("role") != "user":
                    continue
                raw_u = _re.sub(r"<system-(?:reminder|notice)\b[^>]*>.*?</system-(?:reminder|notice)>", " ", str(msg.get("content") or ""), flags=_re.S)
                clean_u = " ".join(raw_u.split()).strip()
                if clean_u:
                    title = clean_u[:120]
                    break
        return {
            "external_id": f"omp:{session_id}", "title": title, "cwd": cwd,
            "project_name": project_name, "started_at": started_at, "updated_at": updated_at,
            "messages": messages, "process_summary": formatted_summary,
            "source_path": str(resolved_path), "usage_observations": usage_observations,
            "is_subagent": is_subagent,
        }
    finally:
        index.close()

def read_omp_sessions(roots: list[str], *, source_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """Read every granted source; corruption in one file is never silent success."""
    if not roots:
        raise AgentAdapterError(host="omp", code="root_not_granted", detail="No authorized roots provided.")
    sessions = []
    failures = []
    seen = set()
    single_explicit_file = source_paths is None and len(roots) == 1 and Path(roots[0]).expanduser().is_file()
    skip_subagents = source_paths is None and not single_explicit_file
    if source_paths is not None:
        candidates = [Path(p).expanduser() for p in source_paths if Path(p).suffix == ".jsonl"]
    else:
        candidates = []
        for root in deduplicate_roots(roots):
            candidates.extend([root] if root.is_file() else sorted(root.rglob("*.jsonl")))
    for candidate in candidates:
        if candidate.suffix != ".jsonl" or str(candidate.absolute()) in seen:
            continue
        seen.add(str(candidate.absolute()))
        try:
            parsed = parse_omp_session_file(candidate, roots)
            if skip_subagents and parsed.get("is_subagent"):
                continue
            sessions.append(parsed)
        except AgentAdapterError as exc:
            failures.append({"path": str(candidate), "code": exc.code, "detail": exc.detail})
    if failures:
        if sessions:
            raise PartialHistoryError(host="omp", code="partial_sync", sessions=sessions, failures=failures,
                                      detail=f"{len(failures)} sources require repair")
        first = failures[0]
        raise AgentAdapterError(host="omp", code=first["code"], path=first["path"], detail=first["detail"])
    return sessions
