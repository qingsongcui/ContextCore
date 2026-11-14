"""ContextCore OpenCode adapter.

OpenCode >= 1.2 keeps sessions in one SQLite database (``opencode.db``, WAL) with
``session`` / ``message`` / ``part`` tables. Older installs used a JSON tree under
``storage/{session,message,part}``. The database wins when present; the legacy tree is a
read-only fallback. Subagent sessions (``parent_id`` not null) are excluded, and synthetic
parts injected by the ContextCore plugin are never re-captured.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator

from .base import (
    usage_metadata_record,
    MAX_SUMMARY_LENGTH,
    bound_process_summary,
    AgentAdapterError,
    PartialHistoryError,
    isolated_sqlite_snapshot,
    raw_path_str,
    validate_file_safety,
    validate_path,
    validate_path_str,
)

SKIP_PART_TYPES = frozenset({
    "step-start",
    "step-finish",
    "snapshot",
    "patch",
    "file",
    "agent",
    "subtask",
    "retry",
    "compaction",
})


def _iso_from_ms(value: Any) -> str:
    try:
        milliseconds = int(value)
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _load_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _find_database(roots: list[str]) -> Path | None:
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        candidates = [root] if root.is_file() else [root / "opencode.db", root / "opencode" / "opencode.db"]
        for candidate in candidates:
            if candidate.is_file() and candidate.name == "opencode.db":
                return candidate
    return None


def _connect_readonly(staged_path: Path) -> tuple[sqlite3.Connection, bool]:
    # Opened from a private isolated snapshot so concurrent writer WAL checkpoints
    # or -shm truncation/unlinking cannot SIGBUS this reader.
    connection = sqlite3.connect(staged_path, timeout=2.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA mmap_size = 0")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA query_only = ON")
    return connection, False


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _message_text(message: dict[str, Any], parts: Iterable[dict[str, Any]]) -> tuple[str, list[str], int, bool]:
    role = str(message.get("role") or "")
    prose: list[str] = []
    process: list[str] = []
    process_chars = 0
    dropped = False

    def append_process(line: str) -> None:
        nonlocal process_chars, dropped
        if process_chars >= MAX_SUMMARY_LENGTH:
            dropped = True
            return
        needed = len(line) + (1 if process else 0)
        if process_chars + needed > MAX_SUMMARY_LENGTH:
            dropped = True
            process_chars = MAX_SUMMARY_LENGTH
            return
        process.append(line)
        process_chars += needed

    reasoning = 0
    for part in parts:
        kind = str(part.get("type") or "")
        if kind == "text":
            if part.get("synthetic") is True or part.get("ignored") is True:
                continue
            text = str(part.get("text") or "").strip()
            if text:
                prose.append(text)
        elif kind == "reasoning":
            reasoning += 1
        elif kind == "tool":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            status = str(state.get("status") or "unknown")
            tool = str(part.get("tool") or "tool")
            output = state.get("output") or state.get("error") or ""
            append_process(f"- **{tool}** [{status}]: {output}")
        elif kind in SKIP_PART_TYPES:
            continue
    return "\n".join(prose).strip(), process, reasoning, dropped


def _build_session(
    session: dict[str, Any],
    messages: Iterable[dict[str, Any]],
    parts_by_message: dict[str, list[dict[str, Any]]],
    source: str,
) -> dict[str, Any] | None:
    session_id = str(session.get("id") or "")
    if not session_id:
        return None
    normalized: list[dict[str, Any]] = []
    usage_observations = []
    process: list[str] = []
    reasoning = 0
    summary_truncated = False
    for message in messages:
        message_id = str(message.get("id") or "")
        data = _load_json(message.get("data"))
        usage_record = usage_metadata_record({"id": message_id, "timestamp": _iso_from_ms(message.get("time_created")), "info": data})
        if usage_record:
            usage_observations.append(usage_record)
        prose, part_process, part_reasoning, dropped = _message_text(data, parts_by_message.get(message_id, []))
        process.extend(part_process)
        reasoning += part_reasoning
        summary_truncated = summary_truncated or dropped
        role = str(data.get("role") or "")
        if role not in {"user", "assistant"} or not prose:
            continue
        normalized.append({
            "external_id": f"opencode:{session_id}:{message_id}",
            "role": role,
            "content": prose,
            "timestamp": _iso_from_ms(message.get("time_created")) or _iso_from_ms((data.get("time") or {}).get("created")),
        })
    if not normalized and not usage_observations:
        return None
    title = str(session.get("title") or "").strip()
    if not title:
        first_user = next((item["content"] for item in normalized if item["role"] == "user"), "")
        title = first_user.splitlines()[0][:120] if first_user else f"OpenCode {session_id}"
    if reasoning:
        process.append(f"- **reasoning** [count]: {reasoning}")
    started = _iso_from_ms(session.get("time_created"))
    updated = _iso_from_ms(session.get("time_updated")) or started
    return {
        "external_id": f"opencode:{session_id}",
        "title": title,
        "cwd": str(session.get("directory") or ""),
        "started_at": started,
        "updated_at": updated,
        "messages": normalized,
        "process_summary": bound_process_summary("\n".join(process), truncated=summary_truncated),
        "source_path": f"{source}#{session_id}" if source else None,
        "usage_observations": usage_observations,
    }


def _build_database_session(session: dict[str, Any], connection: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    """Build one normalized session while iterating its SQLite rows in place."""
    session_id = str(session.get("id") or "")
    if not session_id:
        return None
    normalized: list[dict[str, Any]] = []
    usage_observations: list[dict[str, Any]] = []
    process_text = ""
    reasoning = 0
    summary_truncated = False
    message_rows = connection.execute(
        "SELECT id, time_created, data FROM message WHERE session_id = ? ORDER BY time_created, id",
        (session_id,),
    )
    for row in message_rows:
        message = dict(row)
        message_id = str(message.get("id") or "")
        data = _load_json(message.get("data"))
        usage = usage_metadata_record({
            "id": message_id,
            "timestamp": _iso_from_ms(message.get("time_created")),
            "info": data,
        })
        if usage:
            usage_observations.append(usage)
        part_rows = connection.execute(
            "SELECT data FROM part WHERE session_id = ? AND message_id = ? ORDER BY time_created, id",
            (session_id, message_id),
        )
        part_values = (_load_json(part["data"]) for part in part_rows)
        prose, part_process, part_reasoning, dropped = _message_text(data, part_values)
        reasoning += part_reasoning
        summary_truncated = summary_truncated or dropped
        for line in part_process:
            fragment = ("\n" if process_text else "") + line
            if len(process_text) + len(fragment) > MAX_SUMMARY_LENGTH:
                summary_truncated = True
                break
            process_text += fragment
        role = str(data.get("role") or "")
        if role in {"user", "assistant"} and prose:
            normalized.append({
                "external_id": f"opencode:{session_id}:{message_id}",
                "role": role,
                "content": prose,
                "timestamp": _iso_from_ms(message.get("time_created")) or _iso_from_ms((data.get("time") or {}).get("created")),
            })
    if not normalized and not usage_observations:
        return None
    title = str(session.get("title") or "").strip()
    if not title:
        first_user = next((item["content"] for item in normalized if item["role"] == "user"), "")
        title = first_user.splitlines()[0][:120] if first_user else f"OpenCode {session_id}"
    if reasoning:
        fragment = ("\n" if process_text else "") + f"- **reasoning** [count]: {reasoning}"
        if len(process_text) + len(fragment) > MAX_SUMMARY_LENGTH:
            summary_truncated = True
        else:
            process_text += fragment
    started = _iso_from_ms(session.get("time_created"))
    updated = _iso_from_ms(session.get("time_updated")) or started
    return {
        "external_id": f"opencode:{session_id}", "title": title,
        "cwd": str(session.get("directory") or ""), "started_at": started,
        "updated_at": updated, "messages": normalized,
        "process_summary": bound_process_summary(process_text, truncated=summary_truncated),
        "source_path": f"{source}#{session_id}" if source else None,
        "usage_observations": usage_observations,
    }


def _iter_database_sessions(db_path: Path, roots: list[Path]) -> Iterator[dict[str, Any]]:
    try:
        resolved_db = validate_path(db_path, roots)
    except AgentAdapterError as exc:
        raise AgentAdapterError(
            host="opencode",
            code=exc.code,
            path=exc.path or str(db_path),
            detail=exc.detail,
            retryable=exc.retryable,
        ) from exc
    with isolated_sqlite_snapshot(resolved_db, host="opencode") as staged_db:
        connection, immutable = _connect_readonly(staged_db)
        try:
            connection.execute("BEGIN")
            tables = _tables(connection)
            missing = {"session", "message", "part"} - tables
            if missing:
                raise AgentAdapterError(
                    host="opencode",
                    code="schema_version_unsupported",
                    path=str(db_path),
                    detail=f"opencode.db is missing tables: {', '.join(sorted(missing))}",
                )
            session_columns = _columns(connection, "session")
            where_parent = "WHERE parent_id IS NULL" if "parent_id" in session_columns else ""
            rows = connection.execute(
                f"SELECT id, title, directory, time_created, time_updated FROM session {where_parent} ORDER BY time_updated DESC"
            )
            yielded = False
            failures: list[dict[str, Any]] = []
            for row in rows:
                session = dict(row)
                session_id = str(session.get("id") or "")
                try:
                    built = _build_database_session(session, connection, str(db_path))
                except sqlite3.Error as error:
                    failures.append({"path": str(db_path), "code": "unsupported_format", "detail": f"{session_id}: {error}"})
                    continue
                if built:
                    if immutable:
                        built["process_summary"] = (built["process_summary"] + "\n- **freshness** [warn]: WAL was not readable; recent writes may be missing").strip()
                    yielded = True
                    yield built
            if yielded and failures:
                raise PartialHistoryError(host="opencode", code="partial_sync", detail="Some source records could not be read", failures=failures)
            if failures and not yielded:
                raise AgentAdapterError(
                    host="opencode",
                    code=failures[0]["code"],
                    path=str(db_path),
                    detail=failures[0]["detail"],
                    retryable=bool(failures[0].get("retryable", False)),
                )
        finally:
            connection.close()


def _parse_database(db_path: Path, roots: list[Path]) -> list[dict[str, Any]]:
    """Compatibility list API over the streaming database reader."""
    sessions: list[dict[str, Any]] = []
    try:
        sessions.extend(_iter_database_sessions(db_path, roots))
    except PartialHistoryError as exc:
        raise PartialHistoryError(host=exc.host, code=exc.code, path=exc.path, detail=exc.detail,
                                  retryable=exc.retryable, sessions=sessions, failures=exc.failures) from exc
    return sessions


def _iter_json_files(directory: str | Path, *, sort_entries: bool = False) -> Iterator[str]:
    """Walk legacy storage with O(depth) memory; sort only within one session."""
    dir_str = raw_path_str(directory)
    if sort_entries:
        try:
            with os.scandir(dir_str) as it:
                matched = sorted(
                    entry.path
                    for entry in it
                    if entry.is_file(follow_symlinks=True) and entry.name.endswith(".json")
                )
        except OSError as exc:
            raise AgentAdapterError(host="opencode", code="permission_denied", path=dir_str, detail=str(exc)) from exc
        for path in matched:
            yield path
        return
    try:
        stack = [os.scandir(dir_str)]
    except OSError as exc:
        raise AgentAdapterError(host="opencode", code="permission_denied", path=dir_str, detail=str(exc)) from exc
    while stack:
        try:
            entry = next(stack[-1])
        except StopIteration:
            stack.pop().close()
            continue
        except OSError as exc:
            iterator = stack.pop()
            path = getattr(iterator, "path", dir_str)
            iterator.close()
            for pending in stack:
                pending.close()
            stack.clear()
            raise AgentAdapterError(host="opencode", code="permission_denied", path=str(path), detail=str(exc)) from exc
        try:
            if entry.is_dir(follow_symlinks=False):
                stack.append(os.scandir(entry.path))
            elif entry.is_file(follow_symlinks=True) and entry.name.endswith(".json"):
                yield entry.path
        except OSError as exc:
            for pending in stack:
                pending.close()
            stack.clear()
            raise AgentAdapterError(host="opencode", code="permission_denied", path=entry.path, detail=str(exc)) from exc


def _legacy_json(path: str | Path, roots: list[str | Path]) -> dict[str, Any]:
    raw_p = raw_path_str(path)
    validate_path_str(raw_p, roots)
    validate_file_safety(raw_p, host="opencode")
    try:
        with open(raw_p, "r", encoding="utf-8") as fh:
            return _load_json(fh.read())
    except (OSError, UnicodeError) as exc:
        raise AgentAdapterError(host="opencode", code="unsupported_format", path=raw_p, detail=str(exc)) from exc


def _is_under_dir(candidate_path: str, parent_real: str) -> bool:
    try:
        cand_real = os.path.realpath(candidate_path, strict=os.path.ALLOW_MISSING)
        return cand_real != parent_real and os.path.commonpath([cand_real, parent_real]) == parent_real
    except (OSError, ValueError):
        return False


def _iter_legacy_sessions(root: str | Path, roots: list[str | Path], *, source_paths: list[str] | None = None) -> Iterator[dict[str, Any]]:
    root_str = raw_path_str(root)
    storage_str = root_str if os.path.basename(root_str.rstrip("/\\")) == "storage" else os.path.join(root_str, "storage")
    session_dir_str = os.path.join(storage_str, "session")
    if not os.path.isdir(session_dir_str):
        return
    root_strs: list[str | Path] = [raw_path_str(r) for r in roots]
    if source_paths is None:
        session_files: Iterable[str] = _iter_json_files(session_dir_str)
    else:
        # Targeted compatibility reads accept session records directly. Service
        # sync passes the storage directory and streams the tree instead.
        session_dir_real = os.path.realpath(session_dir_str, strict=os.path.ALLOW_MISSING)
        session_files = (
            path for path in source_paths
            if os.path.splitext(path)[1] == ".json" and _is_under_dir(path, session_dir_real)
        )
    for session_file in session_files:
        session = _legacy_json(session_file, root_strs)
        session_stem = os.path.splitext(os.path.basename(session_file))[0]
        session_id = str(session.get("id") or session_stem)
        if session.get("parentID"):
            continue
        message_dir_str = os.path.join(storage_str, "message", session_id)
        messages: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        if os.path.isdir(message_dir_str):
            for message_file in _iter_json_files(message_dir_str, sort_entries=True):
                data = _legacy_json(message_file, root_strs)
                msg_stem = os.path.splitext(os.path.basename(message_file))[0]
                message_id = str(data.get("id") or msg_stem)
                messages.append({"id": message_id, "time_created": (data.get("time") or {}).get("created"), "data": json.dumps(data)})
                part_dir_str = os.path.join(storage_str, "part", message_id)
                if os.path.isdir(part_dir_str):
                    grouped[message_id] = [_legacy_json(part_file, root_strs) for part_file in _iter_json_files(part_dir_str, sort_entries=True)]
        built = _build_session(
            {"id": session_id, "title": session.get("title"), "directory": session.get("directory"),
             "time_created": (session.get("time") or {}).get("created"), "time_updated": (session.get("time") or {}).get("updated")},
            messages,
            grouped,
            session_file,
        )
        if built:
            yield built


def _parse_legacy(root: Path, roots: list[Path], source_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """Compatibility list API over the per-session legacy iterator."""
    return list(_iter_legacy_sessions(root, roots, source_paths=source_paths))



def iter_opencode_sessions(roots: list[str], *, source_paths: list[str] | None = None) -> Iterator[dict[str, Any]]:
    """Yield one normalized session at a time, preserving partial-source errors."""
    if not roots:
        raise AgentAdapterError(host="opencode", code="root_not_granted", detail="No authorized OpenCode root")
    normalized = [Path(os.path.expanduser(raw_path_str(root))) for root in roots]
    databases: dict[str, Path] = {}
    explicit_legacy_dirs: dict[str, str] = {}
    if source_paths is not None:
        for raw_path in source_paths:
            exp = os.path.expanduser(raw_path)
            if os.path.basename(exp) == "opencode.db" and os.path.isfile(exp):
                canon = os.path.realpath(exp)
                databases[canon] = Path(canon)
            elif os.path.isdir(exp) and os.path.isdir(os.path.join(exp, "session")):
                canon = os.path.realpath(exp)
                explicit_legacy_dirs[canon] = canon
    else:
        for root in roots:
            candidate = _find_database([root])
            if candidate:
                canon = os.path.realpath(raw_path_str(candidate))
                databases[canon] = Path(canon)

    failures: list[dict[str, Any]] = []
    yielded = False
    for path in databases.values():
        try:
            validate_path(path, normalized)
            for session in _iter_database_sessions(path, normalized):
                yielded = True
                yield session
        except PartialHistoryError as exc:
            failures.extend(exc.failures)
        except (AgentAdapterError, sqlite3.Error) as exc:
            failures.append({
                "path": raw_path_str(path),
                "code": getattr(exc, "code", "unsupported_format"),
                "detail": str(exc),
                "retryable": bool(getattr(exc, "retryable", False)),
            })

    if failures:
        if yielded:
            raise PartialHistoryError(host="opencode", code="partial_sync", failures=failures,
                                      detail="Some databases could not be read")
        first = failures[0]
        retryable = all(bool(item.get("retryable", False)) for item in failures)
        raise AgentAdapterError(
            host="opencode",
            code=first["code"],
            path=first["path"],
            detail=first["detail"],
            retryable=retryable,
        )
    if databases:
        return

    # Legacy JSON storage is emitted one session at a time. A service-planned
    # storage directory means the exact tree was snapshotted as one atomic source;
    # direct callers may instead provide exact source files without another rglob.
    legacy_roots: list[str] = list(explicit_legacy_dirs.values())
    directory_snapshot = bool(legacy_roots)
    if not legacy_roots:
        for root in normalized:
            r_str = raw_path_str(root)
            if not os.path.isdir(r_str):
                continue
            storage_str = r_str if os.path.basename(r_str.rstrip("/\\")) == "storage" else os.path.join(r_str, "storage")
            if os.path.isdir(os.path.join(storage_str, "session")):
                legacy_roots.append(storage_str)
    legacy_found = False
    for storage in legacy_roots:
        legacy_found = True
        try:
            yield from _iter_legacy_sessions(
                storage, normalized,
                source_paths=None if directory_snapshot else source_paths,
            )
        except AgentAdapterError as exc:
            failures.append({"path": exc.path or str(storage), "code": exc.code, "detail": exc.detail})
    if failures:
        if yielded or legacy_found:
            raise PartialHistoryError(host="opencode", code="partial_sync", failures=failures,
                                      detail="Some legacy session records could not be read")
        first = failures[0]
        raise AgentAdapterError(host="opencode", code=first["code"], path=first["path"], detail=first["detail"])
    if legacy_found:
        return
    raise AgentAdapterError(host="opencode", code="root_not_granted", detail="No readable opencode.db and no legacy storage tree")


def read_opencode_sessions(roots: list[str], *, source_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """Compatibility API that materializes the streaming result for existing callers."""
    sessions: list[dict[str, Any]] = []
    try:
        sessions.extend(iter_opencode_sessions(roots, source_paths=source_paths))
    except PartialHistoryError as exc:
        raise PartialHistoryError(host=exc.host, code=exc.code, path=exc.path, detail=exc.detail,
                                  retryable=exc.retryable, sessions=sessions, failures=exc.failures) from exc
    return sessions

def normalize_opencode_hook(payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("event") or "unknown")
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "").strip()
    if not session_id:
        raise AgentAdapterError(host="opencode", code="unsupported_format", detail="Hook requires a stable session ID")
    record: dict[str, Any] = {
        "host": "opencode",
        "event": event,
        "session_id": session_id,
        "external_id": f"opencode:{session_id}",
        "title": f"OpenCode {session_id}",
        "cwd": payload.get("cwd"),
        "source_path": None,
        "timestamp": str(payload.get("timestamp") or ""),
        "messages": [],
        "process_summary": [],
        "metadata": {},
    }
    if event in {"chat.message", "chat-message", "UserPromptSubmit"}:
        prompt = str(payload.get("prompt") or payload.get("text") or "").strip()
        if prompt:
            message_id = str(payload.get("message_id") or "prompt")
            record["messages"].append({
                "external_id": f"opencode:{session_id}:{message_id}",
                "role": "user",
                "content": prompt,
                "timestamp": record["timestamp"],
            })
    elif event in {"session.idle", "session-idle", "Stop"}:
        record["metadata"]["lifecycle_event"] = "session.idle"
    return record
