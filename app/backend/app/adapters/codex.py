"""
ContextCore Codex Adapter.

History prefers the official app-server when execution is explicitly allowed,
and provides a robust local JSONL parser for rollout-*.jsonl session archives.
Community verified across:
- grapeot/ai_session_export (MIT License, https://github.com/grapeot/ai_session_export)
- vshulcz/deja-vu (MIT License, https://github.com/vshulcz/deja-vu)

Supports:
- session_meta, turn_context, event_msg (item_completed, user_message, agent_message)
- response_item (message with user / assistant role)
- Full textual original fidelity: no arbitrary 4000-character truncation
- Thread titles loaded from session_index.jsonl or first user query line
- Process summary for tools/reasoning without wholesale dumping of private payloads
- Distinguishes live incomplete trailing line at EOF vs corruption in the middle
- Restricts directory scans to Codex rollout files and session trees
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping

from .base import (
    usage_metadata_record,
    iter_complete_jsonl,
    MAX_SUMMARY_LENGTH,
    bound_process_summary,
    AgentAdapterError,
    PartialHistoryError,
    deduplicate_roots,
    isolated_sqlite_snapshot,
    validate_file_safety,
    validate_path,
)

SESSION_ID_PATTERN = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$")

KNOWN_CODEX_TYPES: frozenset[str] = frozenset({
    "session_meta",
    "turn_context",
    "event_msg",
    "response_item",
    "world_state",
    "compacted",
    "token_usage_record",
    "inter_agent_communication_metadata",
    "realtime_item",
    "retained_context",
})

KNOWN_EVENT_MSG_TYPES: frozenset[str] = frozenset({
    "task_started",
    "task_complete",
    "thread_settings_applied",
    "thread_goal_updated",
    "turn_aborted",
    "token_count",
    "user_message",
    "agent_message",
    "item_completed",
})

KNOWN_RESPONSE_ITEM_TYPES: frozenset[str] = frozenset({
    "message",
    "reasoning",
    "custom_tool_call",
    "custom_tool_call_output",
    "function_call",
    "function_call_output",
    "agent_message",
    "additional_tools",
    "configuration_update",
    "compaction",
})


def _text(value: Any) -> str:
    """Extract plain text from string, list of text blocks, or dict."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, dict):
                sub = str(part.get("text") or part.get("content") or "").strip()
                if sub:
                    parts.append(sub)
            elif isinstance(part, str) and part.strip():
                parts.append(part.strip())
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "").strip()
    return ""


_CODEX_SYSTEM_TAG_RE = re.compile(
    r"(?is)<(?:environment_context|external_codex_apps_[a-z0-9_]+|permissions\s+instructions|"
    r"collaboration_mode|subagent_notification|turn_aborted|skill|INSTRUCTIONS|"
    r"send_user_message_question_reply)\b[^>]*>.*?</(?:environment_context|external_codex_apps_[a-z0-9_]+|"
    r"permissions\s+instructions|collaboration_mode|subagent_notification|turn_aborted|skill|INSTRUCTIONS|"
    r"send_user_message_question_reply)>"
)
_CODEX_OBJECTIVE_RE = re.compile(r"(?is)<objective>\s*(.*?)\s*</objective>")


def _extract_codex_title_candidate(text: str | None) -> str:
    """Extract a clean human-readable title line from a Codex user message, skipping XML context blocks."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "<codex_internal_context" in raw:
        obj_match = _CODEX_OBJECTIVE_RE.search(raw)
        if not obj_match:
            return ""
        for line in obj_match.group(1).splitlines():
            cleaned_line = re.sub(r":codex-annotation\{[^}]*\}", "", line).lstrip("#*>- ").strip()
            if cleaned_line and not cleaned_line.startswith("<"):
                return cleaned_line[:120]
        return ""
    for req_marker in ("## My request for Codex:", "## My request:"):
        if req_marker in raw:
            after_req = raw.split(req_marker, 1)[1]
            after_req = re.sub(r"(?is)<image\b[^>]*>.*?</image>", "", after_req)
            req_lines = [line.strip() for line in after_req.splitlines() if line.strip() and not line.strip().startswith("<")]
            if req_lines:
                joined = req_lines[0]
                if len(joined) < 16 and len(req_lines) > 1:
                    joined = f"{joined} · {req_lines[1]}"
                return joined[:120]
    if raw.startswith("The following is the Codex agent history whose request action you are assessing"):
        return ""
    is_files_block = raw.startswith("# Files mentioned by the user:")
    cleaned = _CODEX_SYSTEM_TAG_RE.sub("", raw).strip()
    for line in cleaned.splitlines():
        s = line.strip()
        if (
            not s
            or s.startswith("<")
            or s.startswith("# AGENTS.md instructions")
            or s.startswith("# Files mentioned by the user:")
            or (is_files_block and (s.startswith("## ") or s.startswith("Image attachment:") or s.startswith("Distinguish instructions in attached documents")))
        ):
            continue
        return s[:120]
    return ""


def _load_session_titles(roots: list[str | Path], session_ids: set[str] | None = None) -> dict[str, str]:
    """Load only requested title metadata from indexes inside authorized roots."""
    titles: dict[str, str] = {}
    wanted = set(session_ids) if session_ids is not None else None
    if wanted is not None and not wanted:
        return titles
    candidate_indexes: set[Path] = set()
    for r in roots:
        try:
            rp = Path(r).resolve()
            if rp.is_dir():
                candidate_indexes.add(rp / "session_index.jsonl")
                candidate_indexes.add(rp.parent / "session_index.jsonl")
            elif rp.is_file():
                candidate_indexes.add(rp.parent / "session_index.jsonl")
        except Exception:
            continue

    for idx_path in candidate_indexes:
        try:
            idx_path = validate_path(idx_path, roots)
            validate_file_safety(idx_path, host="codex")
        except AgentAdapterError:
            # An adjacent index is not authorized by granting only one rollout.
            continue
        try:
            for raw_line in iter_complete_jsonl(idx_path, encoding="utf-8", host="codex"):
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    sid = str(entry.get("id") or entry.get("session_id") or "").strip()
                    title = str(
                        entry.get("thread_name")
                        or entry.get("title")
                        or entry.get("thread_title")
                        or ""
                    ).strip()
                    if sid and title and (wanted is None or sid in wanted):
                        titles[sid] = title
                        if wanted is not None:
                            wanted.discard(sid)
                            if not wanted:
                                return titles
        except Exception:
            continue
    return titles


def _rollout_session_id(path: Path, roots: list[str | Path]) -> str | None:
    match = SESSION_ID_PATTERN.search(path.stem)
    if match:
        return match.group(1)
    resolved = validate_path(path, roots)
    validate_file_safety(resolved, host="codex")
    for raw_line in iter_complete_jsonl(resolved, encoding="utf-8", host="codex"):
        try:
            entry = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        session_id = str(payload.get("id") or payload.get("session_id") or "").strip()
        if session_id:
            return session_id
    return None


def _iter_jsonl(roots: list[str | Path]) -> list[Path]:
    """
    Discover candidate Codex rollout JSONL files under authorized roots.
    Targets rollout-*.jsonl files or files in sessions/ / archived_sessions/.
    """
    deduped = deduplicate_roots(roots)
    files: list[Path] = []
    seen: set[str] = set()

    for root_path in deduped:
        if root_path.is_file():
            if root_path.suffix == ".jsonl":
                canon = str(root_path.resolve())
                if canon not in seen:
                    seen.add(canon)
                    files.append(validate_path(root_path, roots))
        elif root_path.is_dir():
            candidates: list[Path] = list(root_path.rglob("rollout*.jsonl"))
            for sub in ("sessions", "archived_sessions"):
                sub_dir = root_path / sub
                if sub_dir.is_dir():
                    candidates.extend(sub_dir.rglob("*.jsonl"))
            if root_path.name in ("sessions", "archived_sessions"):
                candidates.extend(root_path.glob("*.jsonl"))

            for cand in candidates:
                if cand.suffix != ".jsonl":
                    continue
                canon = str(cand.resolve())
                if canon not in seen:
                    seen.add(canon)
                    files.append(validate_path(cand, roots))
    return files


def _parse_codex_rollout(path: Path, titles: dict[str, str] | None = None) -> dict[str, Any] | None:
    """
    Parse a single Codex rollout JSONL file into a normalized session record.
    Returns None for legitimate empty sessions (header-only, compaction-only,
    or live new sessions without user/assistant dialogue).
    Raises AgentAdapterError for unsupported format or corrupt records.
    """
    validate_file_safety(path, host="codex")

    messages: list[dict[str, Any]] = []
    usage_observations = []
    usage_model = None
    seen_msg_ids: set[str] = set()
    summary: list[str] = []
    cwd = ""
    match = SESSION_ID_PATTERN.search(path.stem)
    filename_id = match.group(1) if match else path.stem
    session_id = filename_id

    started_at = ""
    first_user_text = ""

    has_session_meta = False
    authoritative_meta_found = False
    has_valid_codex_records = False
    unknown_record_reason: str | None = None
    for line_number, raw_line in enumerate(iter_complete_jsonl(path, encoding="utf-8", host="codex"), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            # Unterminated writes were already excluded by iter_complete_jsonl.
            raise AgentAdapterError(
                host="codex",
                code="unsupported_format",
                path=str(path),
                detail=f"Invalid JSON at line {line_number}: {exc}",
            ) from exc

        if not isinstance(item, dict):
            unknown_record_reason = f"Record at line {line_number} is not a JSON object"
            continue

        kind = item.get("type")
        if not kind or kind not in KNOWN_CODEX_TYPES:
            unknown_record_reason = f"Unrecognized record schema '{kind}' at line {line_number}"
            continue

        has_valid_codex_records = True
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        item_ts = str(item.get("timestamp") or "")
        if kind == "turn_context" and isinstance(payload.get("model"), str):
            usage_model = payload["model"]
        usage_record = usage_metadata_record(item)
        if usage_record:
            usage_record.setdefault("id", f"{session_id}:usage:{line_number}")
            if usage_model and isinstance(usage_record.get("payload"), dict):
                usage_record["payload"].setdefault("model", usage_model)
            usage_observations.append(usage_record)

        if kind == "session_meta":
            mid = str(payload.get("id") or payload.get("session_id") or "")
            mcwd = str(payload.get("cwd") or "")
            mts = str(payload.get("timestamp") or item_ts or "")
            if not has_session_meta:
                has_session_meta = True
                if mcwd:
                    cwd = mcwd
                if mts and not started_at:
                    started_at = mts
                if mid and (mid == filename_id or not match):
                    session_id = mid
                    authoritative_meta_found = True
                elif not match:
                    session_id = mid
            else:
                # Replayed source metadata must not overwrite initial owning session identity or cwd.
                if not authoritative_meta_found and mid and mid == filename_id:
                    session_id = mid
                    if mcwd:
                        cwd = mcwd
                    authoritative_meta_found = True
                summary.append("- event: replayed_session_meta")
        elif kind == "turn_context":
            if not cwd:
                cwd = str(payload.get("cwd") or cwd)
        elif kind == "event_msg":
            payload_type = payload.get("type")
            if not payload_type or payload_type not in KNOWN_EVENT_MSG_TYPES:
                unknown_record_reason = f"Unrecognized event_msg type '{payload_type}' at line {line_number}"
                continue

            if payload_type == "user_message":
                text = str(payload.get("message") or "").strip()
                if text:
                    msg_id = str(payload.get("id") or f"{session_id}:{line_number}")
                    if msg_id not in seen_msg_ids:
                        seen_msg_ids.add(msg_id)
                        if not first_user_text:
                            first_user_text = _extract_codex_title_candidate(text)
                        messages.append({
                            "external_id": msg_id,
                            "role": "user",
                            "content": text,
                            "timestamp": item_ts,
                        })
            elif payload_type == "agent_message":
                text = str(payload.get("message") or "").strip()
                if text:
                    msg_id = str(payload.get("id") or f"{session_id}:{line_number}")
                    if msg_id not in seen_msg_ids:
                        seen_msg_ids.add(msg_id)
                        messages.append({
                            "external_id": msg_id,
                            "role": "assistant",
                            "content": text,
                            "timestamp": item_ts,
                        })
            elif payload_type == "item_completed":
                inner_item = payload.get("item")
                if isinstance(inner_item, dict):
                    itype = inner_item.get("type")
                    if itype == "UserMessage":
                        text = _text(inner_item.get("content"))
                        if text:
                            msg_id = str(inner_item.get("id") or f"{session_id}:{line_number}")
                            if msg_id not in seen_msg_ids:
                                seen_msg_ids.add(msg_id)
                                if not first_user_text:
                                    first_user_text = _extract_codex_title_candidate(text)
                                messages.append({
                                    "external_id": msg_id,
                                    "role": "user",
                                    "content": text,
                                    "timestamp": item_ts,
                                })
                    elif itype == "AgentMessage":
                        text = _text(inner_item.get("content"))
                        if text:
                            msg_id = str(inner_item.get("id") or f"{session_id}:{line_number}")
                            if msg_id not in seen_msg_ids:
                                seen_msg_ids.add(msg_id)
                                messages.append({
                                    "external_id": msg_id,
                                    "role": "assistant",
                                    "content": text,
                                    "timestamp": item_ts,
                                })
                    else:
                        summary.append(f"- item: {itype}")
            elif payload_type and payload_type != "token_count":
                summary.append(f"- event: {payload_type}")
        elif kind == "response_item":
            payload_type = payload.get("type")
            role = str(payload.get("role") or "")
            if (
                payload_type is not None
                and payload_type not in KNOWN_RESPONSE_ITEM_TYPES
                and role not in {"user", "assistant", "developer", "system"}
            ):
                unknown_record_reason = f"Unrecognized response_item type '{payload_type}' at line {line_number}"
                continue

            text = _text(payload.get("content"))
            if role in {"user", "assistant"} and text:
                msg_id = str(payload.get("id") or f"{session_id}:{line_number}")
                if msg_id not in seen_msg_ids:
                    seen_msg_ids.add(msg_id)
                    if role == "user" and not first_user_text:
                        first_user_text = _extract_codex_title_candidate(text)
                    messages.append({
                        "external_id": msg_id,
                        "role": role,
                        "content": text,
                        "timestamp": item_ts,
                    })
            elif payload_type == "agent_message":
                author = payload.get("author")
                recipient = payload.get("recipient")
                if author and recipient:
                    summary.append(f"- agent_message: {author} -> {recipient}")
                else:
                    summary.append("- agent_message: item")
            elif payload_type == "compaction":
                summary.append("- compaction")
            elif payload.get("name") or (payload_type not in {None, "message"}):
                summary.append(f"- {payload_type or 'tool'}: {payload.get('name') or role or 'item'}")
        elif kind == "compacted":
            summary.append("- event: compacted")
        elif kind == "inter_agent_communication_metadata":
            summary.append("- event: inter_agent_comm")
        elif kind == "realtime_item":
            pt = payload.get("type") or "session"
            summary.append(f"- realtime: {pt}")
        elif kind == "retained_context":
            summary.append("- event: retained_context")
        elif kind in {"world_state", "token_usage_record"}:
            pass

    if unknown_record_reason:
        raise AgentAdapterError(
            host="codex",
            code="unsupported_format",
            path=str(path),
            detail=unknown_record_reason,
        )

    if not messages and usage_observations and has_session_meta:
        # Accounting may exist without displayable text (e.g. tool-only API turn).
        return {"external_id": f"codex:{session_id}", "title": session_id, "cwd": cwd,
                "started_at": started_at, "updated_at": usage_observations[-1].get("timestamp", started_at),
                "messages": [], "process_summary": "", "source_path": str(path),
                "usage_observations": usage_observations}
    if not messages:
        # Distinguish legitimately empty session (header-only, compaction-only,
        # live new session) from truly unknown or corrupt record schema.
        if has_session_meta and has_valid_codex_records:
            return None
        raise AgentAdapterError(
            host="codex",
            code="unsupported_format",
            path=str(path),
            detail="Codex local file has no extractable user or assistant text",
        )

    title = (titles.get(session_id) if titles else None)
    if not title:
        if first_user_text:
            title = first_user_text.splitlines()[0][:120]
        else:
            assistant_fallback = next(
                (m["content"].splitlines()[0][:120] for m in messages if m.get("role") == "assistant" and m.get("content")),
                "",
            )
            raw_fallback = messages[0]["content"].splitlines()[0][:120] if messages and messages[0].get("content") else ""
            title = assistant_fallback or raw_fallback or session_id

    return {
        "external_id": f"codex:{session_id}",
        "title": title,
        "cwd": cwd,
        "started_at": started_at or messages[0]["timestamp"],
        "updated_at": messages[-1]["timestamp"],
        "messages": messages,
        "process_summary": bound_process_summary("\n".join(summary[:50]), truncated=len(summary) > 50),
        "source_path": str(path),
        "usage_observations": usage_observations,
    }


_STATE_DB_RE = re.compile(r"^state_(\d+)\.sqlite$")
_THREAD_HISTORY_DB_RE = re.compile(r"^thread_history_(\d+)\.sqlite$")


def _is_codex_sqlite_file(path: str | Path) -> bool:
    name = os.path.basename(str(path))
    return bool(_STATE_DB_RE.match(name) or _THREAD_HISTORY_DB_RE.match(name))


def _sqlite_version_key(path: Path) -> tuple[int, str]:
    m = _STATE_DB_RE.match(path.name) or _THREAD_HISTORY_DB_RE.match(path.name)
    ver = int(m.group(1)) if m else 0
    return (-ver, path.name)


def _ms_or_sec_to_iso(ms_val: Any, sec_val: Any = None) -> str:
    for candidate, scale in ((ms_val, 1000.0), (sec_val, 1.0)):
        if candidate is None:
            continue
        try:
            num = float(candidate)
            if num <= 0:
                continue
            return datetime.fromtimestamp(num / scale, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    return ""


def _is_subagent_thread_row(row: Mapping[str, Any]) -> bool:
    if str(row.get("attached_thread_parent_id") or "").strip():
        return True
    if str(row.get("thread_source") or "").strip() in {"subagent", "guardian_review"}:
        return True
    if str(row.get("source") or "").lstrip().startswith('{"subagent"'):
        return True
    return False


class _CodexSqliteHandle:
    """Class-based context manager for staged Codex SQLite connections."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._snap: Any = None
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        snap = isolated_sqlite_snapshot(self.path, host="codex")
        staged_path = snap.__enter__()
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(staged_path, timeout=2.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA mmap_size = 0")
            conn.execute("PRAGMA trusted_schema = OFF")
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.OperationalError as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            snap.__exit__(None, None, None)
            raise AgentAdapterError(
                host="codex",
                code="file_changed",
                path=str(self.path),
                detail=f"Codex SQLite database busy or WAL unreadable: {exc}",
                retryable=True,
            ) from exc
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            snap.__exit__(None, None, None)
            raise AgentAdapterError(
                host="codex",
                code="unsupported_format",
                path=str(self.path),
                detail=f"Corrupt Codex SQLite database: {exc}",
            ) from exc
        except BaseException:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            snap.__exit__(None, None, None)
            raise
        self._snap = snap
        self._conn = conn
        return conn

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        try:
            if self._conn is not None:
                self._conn.close()
        finally:
            self._conn = None
            if self._snap is not None:
                try:
                    self._snap.__exit__(exc_type, exc_val, exc_tb)
                finally:
                    self._snap = None
        return False


def _open_codex_sqlite(path: Path) -> _CodexSqliteHandle:
    return _CodexSqliteHandle(path)


def _discover_codex_sqlite_groups(
    roots: list[str | Path],
    source_paths: list[str] | None = None,
) -> list[tuple[list[Path], list[Path]]]:
    """Discover authorized (state_*.sqlite, thread_history_*.sqlite) pairs grouped by directory."""
    by_dir: dict[Path, set[Path]] = {}

    def _try_add(cand: Path) -> None:
        if not _is_codex_sqlite_file(cand):
            return
        resolved = validate_path(cand, roots)
        if resolved.is_file():
            by_dir.setdefault(resolved.parent, set()).add(resolved)

    if source_paths is None:
        for root_path in deduplicate_roots(roots):
            if root_path.is_file():
                if _is_codex_sqlite_file(root_path):
                    _try_add(root_path)
            elif root_path.is_dir():
                try:
                    with os.scandir(root_path) as it:
                        for entry in it:
                            if _is_codex_sqlite_file(entry.name) and entry.is_file(follow_symlinks=True):
                                _try_add(Path(entry.path))
                except OSError:
                    continue
    else:
        for raw in source_paths:
            cand = Path(raw).expanduser()
            if _is_codex_sqlite_file(cand):
                _try_add(cand)
        # Pair authorized companion state_*.sqlite / thread_history_*.sqlite in the same directory
        # even when incremental sync selected only the changed side of the pair.
        for parent_dir in list(by_dir):
            try:
                with os.scandir(parent_dir) as it:
                    for entry in it:
                        if not _is_codex_sqlite_file(entry.name):
                            continue
                        try:
                            sibling = validate_path(Path(entry.path), roots)
                        except AgentAdapterError:
                            continue
                        if sibling.is_file():
                            by_dir[parent_dir].add(sibling)
            except OSError:
                continue

    groups: list[tuple[list[Path], list[Path]]] = []
    for parent_dir in sorted(by_dir, key=lambda p: str(p)):
        members = by_dir[parent_dir]
        state_dbs = sorted((p for p in members if _STATE_DB_RE.match(p.name)), key=_sqlite_version_key)
        hist_dbs = sorted((p for p in members if _THREAD_HISTORY_DB_RE.match(p.name)), key=_sqlite_version_key)
        if state_dbs or hist_dbs:
            groups.append((state_dbs, hist_dbs))
    return groups


def iter_codex_sqlite_sessions(
    roots: list[str | Path],
    *,
    source_paths: list[str] | None = None,
    session_titles: dict[str, str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream normalized Codex sessions from state_*.sqlite + thread_history_*.sqlite."""
    groups = _discover_codex_sqlite_groups(roots, source_paths=source_paths)
    for state_dbs, hist_dbs in groups:
        thread_meta: dict[str, tuple[Path, dict[str, Any]]] = {}
        ordered_tids: list[str] = []
        seen_state_tids: set[str] = set()
        excluded_tids: set[str] = set()

        for sdb in state_dbs:
            with _open_codex_sqlite(sdb) as conn:
                try:
                    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    if "threads" not in tables:
                        raise AgentAdapterError(
                            host="codex",
                            code="unsupported_format",
                            path=str(sdb),
                            detail="Codex state SQLite database is missing 'threads' table",
                        )
                    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(threads)")}
                    coalesce_terms = [
                        expr
                        for col_name, expr in (
                            ("updated_at_ms", "updated_at_ms"),
                            ("updated_at", "updated_at * 1000"),
                            ("created_at_ms", "created_at_ms"),
                            ("created_at", "created_at * 1000"),
                        )
                        if col_name in cols
                    ]
                    order_expr = f"COALESCE({', '.join(coalesce_terms)}, 0) DESC" if coalesce_terms else "rowid DESC"
                    for row in conn.execute(f"SELECT * FROM threads ORDER BY {order_expr}"):
                        row_dict = dict(row)
                        tid = str(row_dict.get("id") or "").strip()
                        if not tid or tid in seen_state_tids:
                            continue
                        seen_state_tids.add(tid)
                        if _is_subagent_thread_row(row_dict):
                            excluded_tids.add(tid)
                            continue
                        thread_meta[tid] = (sdb, row_dict)
                        ordered_tids.append(tid)
                except sqlite3.DatabaseError as exc:
                    raise AgentAdapterError(
                        host="codex",
                        code="unsupported_format",
                        path=str(sdb),
                        detail=f"Failed reading Codex state SQLite database: {exc}",
                    ) from exc

        hist_conns: list[tuple[Path, sqlite3.Connection]] = []
        with ExitStack() as hist_stack:
            for hdb in hist_dbs:
                hconn = hist_stack.enter_context(_open_codex_sqlite(hdb))
                hist_conns.append((hdb, hconn))
                try:
                    tables = {str(r[0]) for r in hconn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    if "thread_items" not in tables:
                        raise AgentAdapterError(
                            host="codex",
                            code="unsupported_format",
                            path=str(hdb),
                            detail="Codex thread_history SQLite database is missing 'thread_items' table",
                        )
                    for (orphan_tid_raw,) in hconn.execute("SELECT DISTINCT thread_id FROM thread_items"):
                        orphan_tid = str(orphan_tid_raw or "").strip()
                        if orphan_tid and orphan_tid not in seen_state_tids and orphan_tid not in excluded_tids:
                            seen_state_tids.add(orphan_tid)
                            ordered_tids.append(orphan_tid)
                except sqlite3.DatabaseError as exc:
                    raise AgentAdapterError(
                        host="codex",
                        code="unsupported_format",
                        path=str(hdb),
                        detail=f"Failed reading Codex thread_history SQLite database: {exc}",
                    ) from exc

            titles = (
                session_titles
                if session_titles is not None
                else _load_session_titles(roots, set(ordered_tids))
            )

            for tid in ordered_tids:
                meta_entry = thread_meta.get(tid)
                state_db_path = meta_entry[0] if meta_entry else None
                meta = meta_entry[1] if meta_entry else {}
                meta_started = _ms_or_sec_to_iso(meta.get("created_at_ms"), meta.get("created_at"))
                meta_updated = _ms_or_sec_to_iso(meta.get("updated_at_ms"), meta.get("updated_at"))

                messages: list[dict[str, Any]] = []
                seen_msg_ids: set[str] = set()
                summary: list[str] = []
                inferred_cwd = ""
                first_user_text = ""
                used_hist_db: Path | None = None
                is_guardian_or_subagent_orphan = False

                for hdb, hconn in hist_conns:
                    try:
                        cursor = hconn.execute(
                            """
                            SELECT item_id, rollout_ordinal, created_at_ms, completed_at_ms, item_type, item_json
                            FROM thread_items
                            WHERE thread_id = ?
                            ORDER BY rollout_ordinal ASC
                            """,
                            (tid,),
                        )
                        rows = cursor.fetchall()
                    except sqlite3.DatabaseError as exc:
                        raise AgentAdapterError(
                            host="codex",
                            code="unsupported_format",
                            path=str(hdb),
                            detail=f"Corrupt thread_items row in Codex SQLite database: {exc}",
                        ) from exc
                    if not rows:
                        continue
                    used_hist_db = hdb
                    for item_row in rows:
                        itype = str(item_row["item_type"] or "").strip()
                        raw_json = item_row["item_json"]
                        item_obj: dict[str, Any] = {}
                        if isinstance(raw_json, str) and raw_json.strip():
                            try:
                                parsed = json.loads(raw_json)
                                if isinstance(parsed, dict):
                                    item_obj = parsed
                            except json.JSONDecodeError:
                                item_obj = {}
                        created_iso = _ms_or_sec_to_iso(item_row["created_at_ms"])
                        completed_iso = _ms_or_sec_to_iso(item_row["completed_at_ms"])

                        if itype == "userMessage":
                            text = _text(item_obj.get("content") or item_obj.get("text"))
                            if text:
                                if (
                                    meta_entry is None
                                    and not messages
                                    and text.startswith("The following is the Codex agent history whose request action you are assessing")
                                ):
                                    is_guardian_or_subagent_orphan = True
                                    break
                                msg_id = str(item_obj.get("id") or item_row["item_id"] or f"{tid}:{item_row['rollout_ordinal']}")
                                if msg_id not in seen_msg_ids:
                                    seen_msg_ids.add(msg_id)
                                    if not first_user_text:
                                        first_user_text = _extract_codex_title_candidate(text)
                                    messages.append(
                                        {
                                            "external_id": msg_id,
                                            "role": "user",
                                            "content": text,
                                            "timestamp": created_iso or completed_iso or meta_started,
                                        }
                                    )
                        elif itype == "agentMessage":
                            text = _text(item_obj.get("text") or item_obj.get("content"))
                            if text:
                                msg_id = str(item_obj.get("id") or item_row["item_id"] or f"{tid}:{item_row['rollout_ordinal']}")
                                if msg_id not in seen_msg_ids:
                                    seen_msg_ids.add(msg_id)
                                    messages.append(
                                        {
                                            "external_id": msg_id,
                                            "role": "assistant",
                                            "content": text,
                                            "timestamp": completed_iso or created_iso or meta_updated or meta_started,
                                        }
                                    )
                        elif itype == "commandExecution":
                            cmd_cwd = str(item_obj.get("cwd") or "").strip()
                            if cmd_cwd and not inferred_cwd:
                                inferred_cwd = cmd_cwd
                            status = str(item_obj.get("status") or "done")
                            summary.append(f"- commandExecution ({status})")
                        elif itype in {"mcpToolCall", "dynamicToolCall"}:
                            tool_name = str(item_obj.get("tool") or item_obj.get("name") or itype)
                            summary.append(f"- {itype}: {tool_name}")
                        elif itype:
                            summary.append(f"- {itype}")
                    break

                if is_guardian_or_subagent_orphan or not messages:
                    continue

                cwd = str(meta.get("cwd") or inferred_cwd or "").strip()
                title = (
                    (titles.get(tid) if titles else None)
                    or str(meta.get("name") or "").strip()
                    or _extract_codex_title_candidate(str(meta.get("title") or ""))
                    or str(meta.get("title") or "").strip()
                    or first_user_text
                    or _extract_codex_title_candidate(str(meta.get("first_user_message") or ""))
                )
                if not title:
                    assistant_fallback = next(
                        (m["content"].splitlines()[0][:120] for m in messages if m.get("role") == "assistant" and m.get("content")),
                        "",
                    )
                    raw_fallback = messages[0]["content"].splitlines()[0][:120] if messages and messages[0].get("content") else ""
                    title = assistant_fallback or raw_fallback or tid

                msg_first_ts = next((m["timestamp"] for m in messages if m.get("timestamp")), "")
                msg_last_ts = next((m["timestamp"] for m in reversed(messages) if m.get("timestamp")), "")
                started_at = meta_started or msg_first_ts
                updated_at = meta_updated or msg_last_ts or started_at
                if meta_updated and msg_last_ts:
                    try:
                        dt_meta = datetime.fromisoformat(meta_updated.replace("Z", "+00:00"))
                        dt_msg = datetime.fromisoformat(msg_last_ts.replace("Z", "+00:00"))
                        updated_at = msg_last_ts if dt_msg > dt_meta else meta_updated
                    except ValueError:
                        updated_at = meta_updated

                yield {
                    "external_id": f"codex:{tid}",
                    "title": title[:120] if title else tid,
                    "cwd": cwd,
                    "started_at": started_at,
                    "updated_at": updated_at,
                    "messages": messages,
                    "process_summary": bound_process_summary("\n".join(summary[:50]), truncated=len(summary) > 50),
                    "source_path": str(used_hist_db or state_db_path or ""),
                    "usage_observations": [],
                }


def read_codex_sessions(roots: list[str | Path], *, allow_execution: bool = False, source_paths: list[str] | None = None,
                        session_titles: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Scan authorized roots and return parsed Codex sessions (JSONL rollouts + SQLite history)."""
    if not roots:
        raise AgentAdapterError(
            host="codex",
            code="root_not_granted",
            detail="No authorized roots provided.",
        )

    if source_paths is None:
        local_files = _iter_jsonl(roots)
    else:
        # The caller has already planned exact source files. Do not repeat a
        # recursive discovery pass for every budgeted source group.
        local_files = [Path(p).expanduser() for p in source_paths
                       if Path(p).name != "session_index.jsonl" and Path(p).suffix == ".jsonl"]

    sqlite_groups: list[tuple[list[Path], list[Path]]] = []
    sqlite_discovery_failures: list[dict[str, Any]] = []
    try:
        sqlite_groups = _discover_codex_sqlite_groups(roots, source_paths=source_paths)
    except AgentAdapterError as exc:
        sqlite_discovery_failures.append({
            "path": exc.path or (source_paths[0] if source_paths else str(roots[0])),
            "code": exc.code,
            "detail": exc.detail,
        })

    if local_files or sqlite_groups or sqlite_discovery_failures:
        sessions_by_eid: dict[str, dict[str, Any]] = {}
        ordered_eids: list[str] = []
        failures: list[dict[str, Any]] = list(sqlite_discovery_failures)

        def _merge_session(candidate: dict[str, Any]) -> None:
            eid = candidate["external_id"]
            prev = sessions_by_eid.get(eid)
            if prev is None:
                sessions_by_eid[eid] = candidate
                ordered_eids.append(eid)
                return
            prev_len = len(prev.get("messages") or [])
            cand_len = len(candidate.get("messages") or [])
            if cand_len > prev_len or (cand_len == prev_len and not prev.get("cwd") and candidate.get("cwd")):
                # Preserve usage_observations from JSONL if SQLite lacks usage rows
                if prev.get("usage_observations") and not candidate.get("usage_observations"):
                    candidate = {**candidate, "usage_observations": prev["usage_observations"]}
                sessions_by_eid[eid] = candidate
            elif candidate.get("usage_observations") and not prev.get("usage_observations"):
                sessions_by_eid[eid] = {**prev, "usage_observations": candidate["usage_observations"]}

        if local_files:
            titles = session_titles
            if titles is None:
                wanted: set[str] = set()
                for path in local_files:
                    try:
                        sid = _rollout_session_id(path, roots)
                        if sid:
                            wanted.add(sid)
                    except AgentAdapterError:
                        # Let the per-file loop below classify and record any corrupt/oversized file
                        continue
                titles = _load_session_titles(roots, wanted)
            for path in local_files:
                try:
                    validate_path(path, roots)
                    s = _parse_codex_rollout(path, titles)
                except AgentAdapterError as exc:
                    failures.append({
                        "path": str(path),
                        "code": exc.code,
                        "detail": exc.detail,
                    })
                    continue

                if s is None:
                    # Legitimately empty session (header-only, live new, compaction-only)
                    continue
                _merge_session(s)

        if sqlite_groups:
            try:
                for s in iter_codex_sqlite_sessions(roots, source_paths=source_paths, session_titles=session_titles):
                    _merge_session(s)
            except AgentAdapterError as exc:
                failures.append({
                    "path": exc.path or str((sqlite_groups[0][1] or sqlite_groups[0][0])[0]),
                    "code": exc.code,
                    "detail": exc.detail,
                })

        sessions = [sessions_by_eid[eid] for eid in ordered_eids]

        if failures:
            if sessions:
                raise PartialHistoryError(
                    host="codex",
                    code="unsupported_format",
                    path=failures[0]["path"] if len(failures) == 1 else None,
                    detail=f"partial_sync: {len(sessions)} sessions imported; {len(failures)} files require repair",
                    sessions=sessions,
                    failures=failures,
                )
            else:
                first_fail = failures[0]
                raise AgentAdapterError(
                    host="codex",
                    code=first_fail["code"],
                    path=first_fail["path"],
                    detail=first_fail["detail"],
                )

        return sessions
    if not allow_execution:
        raise AgentAdapterError(
            host="codex",
            code="root_not_granted",
            detail="No local Codex JSONL files were found under the granted roots",
        )

    try:
        from app.transport import CodexAppServerUnavailableError, fetch_codex_sessions_sync
    except ImportError as exc:
        raise AgentAdapterError(
            host="codex",
            code="app_server_unavailable",
            detail="Codex app-server transport module is not available.",
        ) from exc

    try:
        return fetch_codex_sessions_sync([str(r) for r in roots], allow_execution=True)
    except CodexAppServerUnavailableError as exc:
        raise AgentAdapterError(
            host="codex",
            code="app_server_unavailable",
            detail=f"Codex app-server is unavailable: {exc}",
        ) from exc


def normalize_codex_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize official Codex hook payload into standard event representation.
    Official hooks: SessionStart, UserPromptSubmit, Stop, PostToolUse, SessionEnd.
    """
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    raw_event = (
        payload.get("hook_event_name")
        or payload.get("event")
        or payload.get("hookEventName")
        or inner.get("hook_event_name")
        or inner.get("event")
        or ""
    )

    if raw_event in ("prompt", "UserPromptSubmit"):
        event_name = "UserPromptSubmit"
    elif raw_event in ("stop", "Stop"):
        event_name = "Stop"
    elif raw_event in ("tool", "PostToolUse"):
        event_name = "PostToolUse"
    elif raw_event in ("end", "SessionEnd"):
        event_name = "SessionEnd"
    else:
        event_name = raw_event

    session_id = payload.get("session_id") or inner.get("session_id")
    if not session_id:
        raise AgentAdapterError(host="codex", code="unsupported_format", detail="Hook requires a stable session ID")
    turn_id = payload.get("turn_id") or inner.get("turn_id")
    cwd = payload.get("cwd") or inner.get("cwd") or ""
    timestamp = payload.get("timestamp") or inner.get("timestamp") or ""
    record: dict[str, Any] = {
        "host": "codex",
        "event": event_name,
        "session_id": session_id,
        "external_id": f"codex:{session_id}",
        "title": payload.get("title") or f"Codex {session_id}",
        "cwd": cwd,
        "timestamp": timestamp,
        "messages": [],
        "process_summary": [],
        "metadata": {
            "model": payload.get("model"),
            "permission_mode": payload.get("permission_mode"),
        },
    }

    if event_name == "UserPromptSubmit":
        prompt = payload.get("prompt") or inner.get("prompt") or ""
        record["messages"].append({
            "external_id": f"codex:{session_id}:{turn_id}:user" if turn_id else None,
            "role": "user",
            "content": prompt,
            "timestamp": timestamp,
        })

    elif event_name == "Stop":
        assistant_msg = payload.get("last_assistant_message") or inner.get("last_assistant_message") or ""
        record["messages"].append({
            "external_id": f"codex:{session_id}:{turn_id}:assistant" if turn_id else None,
            "role": "assistant",
            "content": assistant_msg,
            "timestamp": timestamp,
        })

    elif event_name == "PostToolUse":
        tool_name = payload.get("tool_name") or inner.get("tool_name") or "unknown_tool"
        tool_input = str(payload.get("tool_input") or inner.get("tool_input") or "")[:150]
        tool_output = str(payload.get("tool_output") or inner.get("tool_output") or "")[:150]
        record["process_summary"].append({
            "tool": tool_name,
            "action": "call",
            "summary": f"Tool {tool_name}({tool_input}) -> {tool_output}",
            "status": "ok",
            "timestamp": timestamp,
        })

    return record
