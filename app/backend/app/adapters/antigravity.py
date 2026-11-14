"""
ContextCore Antigravity Adapter.

Ingests local transcripts from Google Antigravity 2.0, Antigravity IDE, and Antigravity CLI.
Observed and community verified across:
- grapeot/ai_session_export (MIT License, https://github.com/grapeot/ai_session_export)
- vshulcz/deja-vu (MIT License, https://github.com/vshulcz/deja-vu)

Sources on disk:
- ~/.gemini/antigravity/brain/<session_id>/.system_generated/logs/transcript*.jsonl
- ~/.gemini/antigravity-cli/brain/<session_id>/.system_generated/logs/transcript*.jsonl
- ~/.gemini/antigravity-ide/brain/<session_id>/.system_generated/logs/transcript*.jsonl

Key behaviors:
- Stable session identity extracted from directory parent (not 'logs').
- User prompt extracted from USER_INPUT with <USER_REQUEST> unwrap.
- Planner response extracted from PLANNER_RESPONSE.
- Full textual fidelity: no arbitrary 4000-character truncation.
- Tolerates live incomplete trailing write at EOF; rejects corrupt lines in middle.
- Deduplicates transcript.jsonl vs transcript_full.jsonl across overlapping roots.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .base import (
    iter_complete_jsonl,
    MAX_SUMMARY_LENGTH,
    bound_process_summary,
    AgentAdapterError,
    PartialHistoryError,
    deduplicate_roots,
    validate_file_safety,
    validate_path,
)

USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)


def _explicit_workspace(item: dict[str, Any]) -> str:
    """Read session metadata only; file/tool targets and quoted text are not cwd."""
    metadata = item.get("metadata")
    for source in (item, metadata if isinstance(metadata, dict) else {}):
        for key in ("cwd", "working_directory", "directory"):
            value = source.get(key)
            if isinstance(value, str) and value.strip().startswith("/"):
                return os.path.normpath(value.strip())
        uris = source.get("workspaceUris")
        if isinstance(uris, list) and len(uris) == 1 and isinstance(uris[0], str):
            uri = urlsplit(uris[0])
            if uri.scheme == "file" and uri.netloc in ("", "localhost") and uri.path.startswith("/"):
                return os.path.normpath(unquote(uri.path))
    return ""


def infer_transcript_cwd(path: str | Path, roots: list[str | Path] | None = None) -> str:
    """Fast metadata-only pass to infer workspace cwd from an Antigravity transcript file on disk."""
    p = Path(path).expanduser()
    if not p.is_file():
        return ""
    try:
        if roots is not None:
            p = validate_path(p, roots)
        validate_file_safety(p, host="antigravity")
        lines = iter_complete_jsonl(p, encoding="utf-8-sig", host="antigravity")
    except (AgentAdapterError, OSError):
        return ""
    # Missing metadata is valid for general conversations.
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        cwd = _explicit_workspace(item)
        if cwd:
            return cwd
    return ""


def _conversation_external_id(
    path: Path | None,
    session_id: str,
    surface: str | None = None,
    *extra_paths: Path | str | None,
) -> str:
    """Keep desktop ids stable. Infer surface from path first so hook and directory sync agree."""
    has_any_path = False
    for candidate in (path, *extra_paths):
        if not candidate:
            continue
        raw = str(candidate).strip()
        if not raw:
            continue
        has_any_path = True
        names = {part.lower() for part in Path(raw).parts}
        if "antigravity-cli" in names:
            return f"antigravity:cli:{session_id}"
        if "antigravity-ide" in names:
            return f"antigravity:ide:{session_id}"
        if "antigravity" in names:
            return f"antigravity:{session_id}"
    if not has_any_path and surface:
        norm_surface = surface.strip().lower()
        if norm_surface in ("cli", "antigravity-cli"):
            return f"antigravity:cli:{session_id}"
        if norm_surface in ("ide", "antigravity-ide"):
            return f"antigravity:ide:{session_id}"
    return f"antigravity:{session_id}"


def _extract_session_id(path: Path) -> str:
    """
    Extract the session UUID/identifier from the directory structure.
    Avoids using generic directory names like 'logs' or '.system_generated'.
    """
    for parent in path.parents:
        name = parent.name.strip()
        if name and name not in ("logs", ".system_generated", "brain"):
            return name
    return path.stem


def _extract_user_text(content: str) -> str:
    """Strip Antigravity XML wrappers, keeping the inner text of <USER_REQUEST> blocks."""
    if not content:
        return ""
    matches = USER_REQUEST_RE.findall(content)
    if matches:
        return "\n\n".join(match.strip() for match in matches if match.strip()).strip()
    return content.strip()


def _discover_session_targets(roots: list[str | Path]) -> list[list[Path]]:
    """
    Discover candidate transcripts under authorized roots.
    Returns a list of session targets. Each target is an ordered list of candidate transcript Paths:
    - Explicit file root: [validated_file_path] (strict single target, no directory companion search)
    - Directory root: candidates grouped by session directory, ordered:
      [transcript_full.jsonl (if present), transcript.jsonl (if present), ...other candidates by size desc]
    """
    deduped = deduplicate_roots(roots)
    explicit_file_targets: list[list[Path]] = []
    found_by_dir: dict[Path, dict[str, Path]] = {}

    for root_path in deduped:
        if root_path.is_file():
            if "transcript" in root_path.name and root_path.suffix == ".jsonl":
                valid_file = validate_path(root_path, roots)
                explicit_file_targets.append([valid_file])
        elif root_path.is_dir():
            for candidate in root_path.rglob("transcript*.jsonl"):
                if candidate.is_file() and candidate.suffix == ".jsonl":
                    parent_dir = candidate.parent
                    found_by_dir.setdefault(parent_dir, {})[candidate.name] = validate_path(candidate, roots)

    dir_targets: list[list[Path]] = []
    for parent_dir, candidates in found_by_dir.items():
        ordered_candidates: list[Path] = []
        if "transcript_full.jsonl" in candidates:
            ordered_candidates.append(candidates["transcript_full.jsonl"])
        if "transcript.jsonl" in candidates and candidates["transcript.jsonl"] not in ordered_candidates:
            ordered_candidates.append(candidates["transcript.jsonl"])

        remaining = [
            p for name, p in candidates.items()
            if p not in ordered_candidates
        ]
        remaining.sort(key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)
        ordered_candidates.extend(remaining)

        if ordered_candidates:
            dir_targets.append(ordered_candidates)

    return explicit_file_targets + dir_targets


def _iter_transcripts(roots: list[str | Path]) -> list[Path]:
    """Return primary candidate transcript for each discovered session target."""
    targets = _discover_session_targets(roots)
    return [t[0] for t in targets if t]


def _parse_transcript(path: Path, *, allow_empty: bool = False) -> dict[str, Any] | None:
    """Parse a single Antigravity transcript JSONL file into a normalized session record."""
    validate_file_safety(path, host="antigravity")

    messages: list[dict[str, Any]] = []
    summary: list[str] = []
    usage_observations: list[dict[str, Any]] = []
    token_usage: dict[str, int] = {}
    session_id = _extract_session_id(path)
    cwd = ""
    first_user_text = ""
    has_valid_antigravity_records = False
    try:
        lines = iter_complete_jsonl(path, encoding="utf-8-sig", host="antigravity")
    except UnicodeDecodeError as exc:
        raise AgentAdapterError(
            host="antigravity",
            code="unsupported_format",
            path=str(path),
            detail=f"Antigravity transcript is not valid UTF-8: {exc}",
        ) from exc
    except Exception as exc:
        raise AgentAdapterError(
            host="antigravity",
            code="unknown",
            path=str(path),
            detail=f"Failed to read Antigravity transcript: {exc}",
        ) from exc

    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            # Every line here is newline-committed; corruption remains visible.
            raise AgentAdapterError(
                host="antigravity",
                code="unsupported_format",
                path=str(path),
                detail=f"Invalid JSON at line {line_number}: {exc}",
            ) from exc

        if not isinstance(item, dict):
            continue
        has_valid_antigravity_records = True

        kind = str(item.get("type") or "step")
        content = item.get("content")
        raw_text = content.strip() if isinstance(content, str) else ""

        if not cwd:
            cwd = _explicit_workspace(item)

        # Extract token usage when present on steps
        step_usage: dict[str, int] = {}
        usage_obj = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        for tok_key in ("input_tokens", "output_tokens", "cache_read_tokens"):
            val = item.get(tok_key)
            if val is None:
                val = usage_obj.get(tok_key)
            if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
                step_usage[tok_key] = val
                token_usage[tok_key] = token_usage.get(tok_key, 0) + val

        if step_usage:
            ts_str = str(item.get("created_at") or "").strip()
            if ts_str:
                try:
                    parsed_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if parsed_dt.tzinfo is not None:
                        usage_observations.append({
                            "id": f"{session_id}:{item.get('step_index', line_number)}",
                            "timestamp": ts_str,
                            "model": item.get("model") or item.get("model_name") or item.get("modelName"),
                            "usage": dict(step_usage),
                        })
                except ValueError:
                    pass

        if kind == "USER_INPUT" and raw_text:
            text = _extract_user_text(raw_text)
            if text:
                if not first_user_text:
                    first_user_text = text
                messages.append({
                    "external_id": f"{session_id}:{item.get('step_index', line_number)}",
                    "role": "user",
                    "content": text,
                    "timestamp": str(item.get("created_at") or ""),
                })
        elif kind == "PLANNER_RESPONSE":
            tool_calls = item.get("tool_calls") if isinstance(item.get("tool_calls"), list) else []
            extracted_tools = 0
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_name = str(tc.get("name") or tc.get("tool") or tc.get("tool_name") or "tool").strip()[:MAX_SUMMARY_LENGTH]
                tc_args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                tc_summary = str(tc_args.get("toolSummary") or tc.get("summary") or "").strip()[:MAX_SUMMARY_LENGTH]
                if tc_summary:
                    summary.append(f"- tool_call: {tc_name} ({tc_summary})")
                else:
                    summary.append(f"- tool_call: {tc_name}")
                extracted_tools += 1
            if raw_text:
                messages.append({
                    "external_id": f"{session_id}:{item.get('step_index', line_number)}",
                    "role": "assistant",
                    "content": raw_text,
                    "timestamp": str(item.get("created_at") or ""),
                })
            elif extracted_tools == 0:
                status_desc = item.get("status") or "recorded"
                summary.append(f"- {kind}: {status_desc}")
        else:
            status_desc = item.get("status") or "recorded"
            summary.append(f"- {kind}: {status_desc}")

    if not messages:
        if allow_empty and has_valid_antigravity_records:
            return None
        raise AgentAdapterError(
            host="antigravity",
            code="unsupported_format",
            path=str(path),
            detail="Antigravity transcript has no extractable user or planner text",
        )

    title = first_user_text.splitlines()[0][:120] if first_user_text else session_id

    result: dict[str, Any] = {
        "external_id": _conversation_external_id(path, session_id),
        "title": title,
        "cwd": cwd,
        "started_at": messages[0]["timestamp"],
        "updated_at": messages[-1]["timestamp"],
        "messages": messages,
        "process_summary": bound_process_summary("\n".join(summary[:50]), truncated=len(summary) > 50),
        "source_path": str(path),
        "usage_observations": usage_observations,
    }
    if token_usage:
        result["token_usage"] = token_usage
    return result


def _dialogue_freshness(session: dict[str, Any]) -> tuple[float, int]:
    """Compare native dialogue positions, not filename preference or tool-file mtime."""
    latest = 0.0
    step = 0
    for message in session["messages"]:
        timestamp = message.get("timestamp")
        if timestamp:
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    latest = max(latest, parsed.timestamp())
            except ValueError:
                pass
        suffix = str(message.get("external_id") or "").rsplit(":", 1)[-1]
        if suffix.isdecimal():
            step = max(step, int(suffix))
    return latest, step


def read_antigravity_sessions(roots: list[str | Path], *, source_paths: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Scan authorized roots and return parsed Antigravity sessions.
    - If root is a single explicit file: strict parse, raises AgentAdapterError directly on corrupt line.
    - If roots are directories: falls back to validated companion transcript (e.g. transcript.jsonl)
      if primary candidate has an unformatted/corrupt line.
    - If genuine corruption occurs where all candidates for a session target fail:
      collects sanitized failure metadata and raises PartialHistoryError with valid sessions and failures.
    """
    if not roots:
        raise AgentAdapterError(
            host="antigravity",
            code="root_not_granted",
            detail="No authorized roots provided.",
        )

    deduped = deduplicate_roots(roots)
    if source_paths is None:
        targets = _discover_session_targets(roots)
    else:
        # These exact paths were already discovered and scheduled by the sync
        # planner. Re-scan only the caller-provided files, never the whole tree.
        selected_files: dict[Path, dict[str, Path]] = {}
        explicit_targets: list[list[Path]] = []
        for raw_path in source_paths:
            candidate = Path(raw_path).expanduser()
            if "transcript" not in candidate.name or candidate.suffix != ".jsonl" or not candidate.is_file():
                continue
            valid = validate_path(candidate, roots)
            if len(deduped) == 1 and deduped[0].is_file():
                explicit_targets.append([valid])
            else:
                selected_files.setdefault(valid.parent, {})[valid.name] = valid
        targets = explicit_targets
        for candidates in selected_files.values():
            ordered: list[Path] = []
            for preferred in ("transcript_full.jsonl", "transcript.jsonl"):
                if preferred in candidates:
                    ordered.append(candidates[preferred])
            ordered.extend(sorted((path for name, path in candidates.items() if name not in {"transcript_full.jsonl", "transcript.jsonl"}),
                                  key=lambda path: path.stat().st_size if path.exists() else 0, reverse=True))
            if ordered:
                targets.append(ordered)
    if not targets:
        raise AgentAdapterError(
            host="antigravity",
            code="root_not_granted",
            path=str(roots[0]),
            detail="No Antigravity transcript files were found under the granted roots",
        )

    # Single explicit file root: strict mode without directory fallback
    if len(deduped) == 1 and deduped[0].is_file() and len(targets) == 1 and len(targets[0]) == 1:
        res = _parse_transcript(targets[0][0], allow_empty=False)
        if res is None:
            raise AgentAdapterError(
                host="antigravity",
                code="unsupported_format",
                path=str(targets[0][0]),
                detail="Antigravity transcript has no extractable user or planner text",
            )
        return [res]

    sessions: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    seen_external_ids: set[str] = set()
    first_error: AgentAdapterError | None = None

    for candidate_list in targets:
        if not candidate_list:
            continue
        parsed_session: dict[str, Any] | None = None
        last_error: AgentAdapterError | None = None
        primary_path = candidate_list[0]
        is_empty_session = False

        for candidate_path in candidate_list:
            try:
                candidate_session = _parse_transcript(candidate_path, allow_empty=True)
                if candidate_session is None:
                    is_empty_session = True
                elif parsed_session is None or _dialogue_freshness(candidate_session) > _dialogue_freshness(parsed_session):
                    parsed_session = candidate_session
            except AgentAdapterError as exc:
                last_error = exc
            except Exception as exc:
                last_error = AgentAdapterError(
                    host="antigravity",
                    code="unknown",
                    path=str(candidate_path),
                    detail=f"Failed to read Antigravity transcript: {exc}",
                )

        if is_empty_session and parsed_session is None and last_error is None:
            # Legitimately empty session (tool-only / system run without dialogue); skip cleanly
            continue

        if parsed_session is not None:
            eid = parsed_session["external_id"]
            if eid not in seen_external_ids:
                seen_external_ids.add(eid)
                sessions.append(parsed_session)
        else:
            if first_error is None and last_error is not None:
                first_error = last_error
            code = last_error.code if last_error else "unsupported_format"
            detail = last_error.detail if last_error else "Failed to parse transcript"
            failures.append({
                "path": str(primary_path),
                "code": code,
                "detail": detail,
            })

    if failures:
        if sessions:
            raise PartialHistoryError(
                host="antigravity",
                code="unsupported_format",
                detail=f"partial_sync: {len(sessions)} sessions imported; {len(failures)} files require repair",
                sessions=sessions,
                failures=failures,
            )
        else:
            # All targets failed
            if len(failures) == 1 and first_error is not None:
                raise first_error
            raise PartialHistoryError(
                host="antigravity",
                code="unsupported_format",
                detail=f"partial_sync: 0 sessions imported; {len(failures)} files require repair",
                sessions=[],
                failures=failures,
            )

    return sessions


def _extract_hook_prompt_text(payload: dict[str, Any]) -> str:
    """Extract user prompt utterance across flat or nested Antigravity hook payloads."""
    containers: list[dict[str, Any]] = [payload]
    for key in ("payload", "data", "event", "hook_event"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
            inner_nested = nested.get("payload")
            if isinstance(inner_nested, dict):
                containers.append(inner_nested)

    for container in containers:
        for key in ("prompt", "user_prompt", "userPrompt", "input", "query", "user_message"):
            val = container.get(key)
            if isinstance(val, str) and val.strip():
                cleaned = _extract_user_text(val)
                if cleaned:
                    return cleaned
        msg = container.get("message")
        if isinstance(msg, str) and msg.strip():
            cleaned = _extract_user_text(msg)
            if cleaned:
                return cleaned
        elif isinstance(msg, dict):
            content = msg.get("content") or msg.get("text")
            if isinstance(content, str) and content.strip():
                cleaned = _extract_user_text(content)
                if cleaned:
                    return cleaned
        msgs = container.get("messages")
        if isinstance(msgs, list):
            for item in reversed(msgs):
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or item.get("type") or "").lower()
                if role in ("user", "user_input", "human", ""):
                    content = item.get("content") or item.get("text")
                    if isinstance(content, str) and content.strip():
                        cleaned = _extract_user_text(content)
                        if cleaned:
                            return cleaned
    return ""


def _allowed_hook_transcript_roots() -> list[Path]:
    """Return authorized non-symlink roots where Antigravity transcripts may reside."""
    roots: list[Path] = []
    home = Path.home()
    gemini_dir = home / ".gemini"
    try:
        if not gemini_dir.is_symlink():
            for default_name in ("antigravity", "antigravity-cli", "antigravity-ide"):
                roots.append(gemini_dir / default_name)
            if gemini_dir.is_dir():
                for child in gemini_dir.glob("antigravity*"):
                    if not child.is_symlink() and child.is_dir():
                        roots.append(child)
    except OSError:
        pass

    env_roots = os.environ.get("SENRELAY_ANTIGRAVITY_ROOTS", "").strip()
    if env_roots:
        for raw_root in env_roots.split(os.pathsep):
            if raw_root.strip():
                roots.append(Path(raw_root.strip()).expanduser())

    try:
        from app.config import settings
        import sqlite3
        db_file = Path(settings.db_path)
        if db_file.is_file() and not db_file.is_symlink():
            conn = sqlite3.connect(db_file, timeout=5.0)
            try:
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA mmap_size = 0")
                conn.execute("PRAGMA query_only = ON")
                row = conn.execute(
                    "SELECT config FROM connections WHERE id = 'antigravity' AND enabled = 1"
                ).fetchone()
                if row and row[0]:
                    cfg = json.loads(row[0]) if isinstance(row[0], str) else {}
                    if isinstance(cfg, dict):
                        for p in cfg.get("paths") or cfg.get("source_paths") or []:
                            if isinstance(p, str) and p.strip():
                                roots.append(Path(p.strip()).expanduser())
            finally:
                conn.close()
    except Exception:
        pass

    valid_roots: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        try:
            if root.is_symlink() or not root.exists():
                continue
            resolved = root.resolve(strict=True)
            if resolved not in seen:
                seen.add(resolved)
                valid_roots.append(root)
        except OSError:
            continue
    return valid_roots


def _is_valid_hook_transcript_file(candidate: Path, allowed_roots: list[Path]) -> bool:
    """Ensure candidate is a non-symlink transcript*.jsonl inside an allowed root with no symlinked ancestors."""
    try:
        if (
            candidate.suffix != ".jsonl"
            or not candidate.name.startswith("transcript")
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            return False
        resolved_candidate = candidate.resolve(strict=True)
        for raw_root in allowed_roots:
            if raw_root.is_symlink():
                continue
            resolved_root = raw_root.resolve(strict=True)
            if resolved_root.is_file():
                if resolved_candidate == resolved_root:
                    return True
                continue
            try:
                rel_parts = resolved_candidate.relative_to(resolved_root).parts
            except ValueError:
                continue
            # Also walk the un-resolved path under raw_root if candidate was passed relative to raw_root
            try:
                raw_rel = candidate.relative_to(raw_root).parts
                cur_raw = raw_root
                symlink_found = False
                for part in raw_rel:
                    cur_raw = cur_raw / part
                    if cur_raw.is_symlink():
                        symlink_found = True
                        break
                if symlink_found:
                    continue
            except ValueError:
                pass
            cur = resolved_root
            symlink_found = False
            for part in rel_parts:
                cur = cur / part
                if cur.is_symlink():
                    symlink_found = True
                    break
            if not symlink_found:
                return True
    except OSError:
        return False
    return False


def _resolve_hook_transcript_candidates(transcript_path: Any, artifact_dir: Any) -> list[Path]:
    """Return ordered validated non-symlink transcript files (preferring transcript_full.jsonl)."""
    raw_candidates: list[Path] = []
    if isinstance(transcript_path, str) and transcript_path.strip():
        tp = Path(transcript_path.strip()).expanduser()
        if tp.name == "transcript.jsonl":
            raw_candidates.append(tp.with_name("transcript_full.jsonl"))
        raw_candidates.append(tp)
    if isinstance(artifact_dir, str) and artifact_dir.strip():
        base = Path(artifact_dir.strip()).expanduser()
        raw_candidates.extend([
            base / ".system_generated" / "logs" / "transcript_full.jsonl",
            base / ".system_generated" / "logs" / "transcript.jsonl",
            base / "transcript_full.jsonl",
            base / "transcript.jsonl",
        ])
    allowed_roots = _allowed_hook_transcript_roots()
    if not allowed_roots:
        return []
    validated: list[Path] = []
    seen: set[Path] = set()
    for candidate in raw_candidates:
        if _is_valid_hook_transcript_file(candidate, allowed_roots):
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                validated.append(candidate)
    return validated


def _resolve_hook_transcript_file(transcript_path: Any, artifact_dir: Any) -> Path | None:
    """Resolve an existing validated non-symlink transcript file on disk from hook metadata."""
    candidates = _resolve_hook_transcript_candidates(transcript_path, artifact_dir)
    return candidates[0] if candidates else None


def normalize_antigravity_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize official Antigravity hook payloads.
    Events: SessionStart, PreInvocation, PostToolUse, Stop.
    """
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    raw_event = (
        payload.get("hook_event_name")
        or payload.get("event")
        or payload.get("hookEventName")
        or payload.get("eventName")
        or inner.get("hook_event_name")
        or inner.get("event")
        or inner.get("hookEventName")
        or inner.get("eventName")
        or ""
    )

    if raw_event in ("session-start", "SessionStart"):
        event_name = "SessionStart"
    elif raw_event in ("pre-invoke", "PreInvocation"):
        event_name = "PreInvocation"
    elif raw_event in ("tool", "PostToolUse"):
        event_name = "PostToolUse"
    elif raw_event in ("stop", "Stop"):
        event_name = "Stop"
    else:
        event_name = str(raw_event)

    conversation_id = (
        payload.get("conversationId")
        or payload.get("conversation_id")
        or payload.get("session_id")
        or payload.get("sessionId")
        or inner.get("conversationId")
        or inner.get("conversation_id")
        or inner.get("session_id")
        or inner.get("sessionId")
    )
    if not conversation_id:
        raise AgentAdapterError(host="antigravity", code="unsupported_format", detail="Hook requires a stable conversation ID")
    timestamp = payload.get("timestamp") or inner.get("timestamp") or ""
    transcript_path = (
        payload.get("transcriptPath")
        or payload.get("transcript_path")
        or inner.get("transcriptPath")
        or inner.get("transcript_path")
    )
    artifact_dir = (
        payload.get("artifactDirectoryPath")
        or payload.get("artifact_directory")
        or inner.get("artifactDirectoryPath")
        or inner.get("artifact_directory")
    )
    surface = (
        payload.get("surface")
        or payload.get("client")
        or inner.get("surface")
        or inner.get("client")
    )
    workspace_paths = (
        payload.get("workspacePaths")
        or payload.get("workspace_paths")
        or inner.get("workspacePaths")
        or inner.get("workspace_paths")
        or []
    )
    cwd = (
        payload.get("cwd")
        or inner.get("cwd")
        or (str(workspace_paths[0]) if isinstance(workspace_paths, list) and workspace_paths else "")
    )
    hint_path = Path(str(transcript_path or artifact_dir)) if (transcript_path or artifact_dir) else None
    external_id = _conversation_external_id(
        hint_path,
        str(conversation_id),
        str(surface) if surface else None,
        transcript_path,
        artifact_dir,
        payload.get("source_path"),
        inner.get("source_path"),
    )

    cleaned_prompt = _extract_hook_prompt_text(payload)
    messages: list[dict[str, Any]] = []
    if cleaned_prompt:
        messages.append({
            "external_id": f"{conversation_id}:hook:{event_name or 'prompt'}",
            "role": "user",
            "content": cleaned_prompt,
            "timestamp": timestamp,
        })

    validated_candidates = _resolve_hook_transcript_candidates(transcript_path, artifact_dir)
    resolved_transcript: Path | None = validated_candidates[0] if validated_candidates else None
    parsed_transcript: dict[str, Any] | None = None
    if not messages and validated_candidates:
        for candidate in validated_candidates:
            try:
                parsed = _parse_transcript(candidate, allow_empty=True)
                if parsed and (parsed_transcript is None or _dialogue_freshness(parsed) > _dialogue_freshness(parsed_transcript)):
                    resolved_transcript = candidate
                    parsed_transcript = parsed
            except Exception:
                continue
        if parsed_transcript:
            messages = list(parsed_transcript["messages"])
            if not cwd:
                cwd = parsed_transcript.get("cwd") or ""

    source_path = str(resolved_transcript) if resolved_transcript is not None else None
    user_messages = [m for m in messages if m.get("role") == "user" and str(m.get("content") or "").strip()]
    title = (
        payload.get("title")
        or inner.get("title")
        or (user_messages[0]["content"].splitlines()[0][:120] if user_messages else f"Antigravity {conversation_id}")
    )
    parsed_summary = parsed_transcript.get("process_summary") if parsed_transcript else None
    record: dict[str, Any] = {
        "host": "antigravity",
        "event": event_name,
        "session_id": conversation_id,
        "external_id": external_id,
        "title": title,
        "cwd": cwd,
        "timestamp": timestamp,
        "messages": messages,
        "process_summary": parsed_summary if parsed_summary else [],
        "source_path": source_path,
        "metadata": {
            "model_name": payload.get("modelName") or inner.get("modelName"),
            "workspace_paths": workspace_paths,
            "transcript_path": transcript_path,
            "artifact_directory": artifact_dir,
            "limitations": [
                "Antigravity hook payload does not include user prompt text; natural language trigger requires slash command.",
                "History is read separately from authorized transcript roots; hook metadata is not dialogue.",
            ],
        },
    }
    if parsed_transcript:
        if parsed_transcript.get("token_usage"):
            record["token_usage"] = parsed_transcript["token_usage"]
        if parsed_transcript.get("usage_observations"):
            record["usage_observations"] = parsed_transcript["usage_observations"]
        if parsed_transcript.get("started_at"):
            record["started_at"] = parsed_transcript["started_at"]
        if parsed_transcript.get("updated_at"):
            record["updated_at"] = parsed_transcript["updated_at"]

    if event_name == "PreInvocation":
        record["metadata"]["supports_inject_steps"] = True

    elif event_name == "PostToolUse":
        tool_name = payload.get("tool_name") or payload.get("toolName") or inner.get("tool_name") or inner.get("toolName") or "tool"
        if isinstance(record["process_summary"], list):
            record["process_summary"].append({
                "tool": tool_name,
                "action": "post_tool_use",
                "summary": f"Antigravity executed tool {tool_name}",
                "status": "ok",
                "timestamp": timestamp,
            })
        elif isinstance(record["process_summary"], str):
            record["process_summary"] = f"{record['process_summary']}\n- post_tool_use: {tool_name}".strip()

    elif event_name == "Stop":
        record["metadata"]["decision"] = payload.get("decision") if "decision" in payload else inner.get("decision")
        record["metadata"]["fully_idle"] = payload.get("fullyIdle", inner.get("fullyIdle", True))

    return record
