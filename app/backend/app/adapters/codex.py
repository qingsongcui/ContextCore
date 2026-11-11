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

import json
import re
from pathlib import Path
from typing import Any

from .base import (
    usage_metadata_record,
    iter_complete_jsonl,
    MAX_SUMMARY_LENGTH,
    bound_process_summary,
    AgentAdapterError,
    PartialHistoryError,
    deduplicate_roots,
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


def read_codex_sessions(roots: list[str | Path], *, allow_execution: bool = False, source_paths: list[str] | None = None,
                        session_titles: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Scan authorized roots and return parsed Codex sessions."""
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
    if local_files:
        titles = session_titles
        if titles is None:
            wanted = {_rollout_session_id(path, roots) for path in local_files}
            titles = _load_session_titles(roots, {item for item in wanted if item})
        sessions: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        seen_external_ids: set[str] = set()

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

            eid = s["external_id"]
            if eid not in seen_external_ids:
                seen_external_ids.add(eid)
                sessions.append(s)

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
