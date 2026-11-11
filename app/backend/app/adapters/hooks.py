"""
ContextCore Hook Normalizer - Unified ingestion for official host hook payloads.
"""

from __future__ import annotations

from typing import Any, Mapping

from .antigravity import normalize_antigravity_hook
from .base import AgentAdapterError
from .codex import normalize_codex_hook
from .cursor import normalize_cursor_hook
from .claude_code import normalize_claude_code_hook
from .opencode import normalize_opencode_hook


def parse_hook(
    host: str,
    payload: dict[str, Any],
    headers: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Parse and normalize an incoming hook event from a supported host into standard records.
    Does not infer missing raw dialogue. Preserves concrete fields and notes known limitations.
    """
    host_lower = host.lower()

    if host_lower == "codex":
        return [normalize_codex_hook(payload)]

    elif host_lower == "cursor":
        return [normalize_cursor_hook(payload)]

    elif host_lower == "antigravity":
        return [normalize_antigravity_hook(payload)]

    elif host_lower == "claude-code":
        return [normalize_claude_code_hook(payload)]

    elif host_lower == "opencode":
        return [normalize_opencode_hook(payload)]

    elif host_lower == "omp":
        # OMP extension in-process events
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        raw_event = (
            payload.get("type")
            or payload.get("event")
            or inner.get("type")
            or inner.get("event")
            or "unknown"
        )
        session_id = payload.get("session_id") or payload.get("sessionId") or inner.get("session_id")
        if not session_id:
            raise AgentAdapterError(host="omp", code="unsupported_format", detail="Hook requires a stable session ID")
        timestamp = payload.get("timestamp") or inner.get("timestamp") or ""

        record: dict[str, Any] = {
            "host": "omp",
            "event": raw_event,
            "session_id": session_id,
            "external_id": f"omp:{session_id}",
            "title": payload.get("title") or f"OMP {session_id}",
            "cwd": payload.get("cwd") or inner.get("cwd"),
            "timestamp": timestamp,
            "messages": [],
            "process_summary": [],
            "metadata": {},
        }

        if raw_event == "message":
            message = payload.get("message") or inner.get("message")
            if isinstance(message, dict) and message.get("role") in ("user", "assistant"):
                record["messages"].append({
                    "external_id": message.get("id") or payload.get("id"),
                    "role": message["role"],
                    "content": message.get("content", ""),
                    "timestamp": timestamp,
                })
        elif raw_event in ("user_message", "intent_detected"):
            user_content = (
                payload.get("content")
                or payload.get("text")
                or payload.get("prompt")
                or inner.get("content")
                or inner.get("text")
                or inner.get("prompt")
                or ""
            )
            record["messages"].append({
                "external_id": f"omp:{session_id}:user:{payload['id']}" if payload.get("id") else None,
                "role": "user",
                "content": user_content,
                "timestamp": timestamp,
            })
        elif raw_event == "assistant_message":
            asst_content = (
                payload.get("content")
                or payload.get("text")
                or inner.get("content")
                or inner.get("text")
                or ""
            )
            record["messages"].append({
                "external_id": f"omp:{session_id}:asst:{payload['id']}" if payload.get("id") else None,
                "role": "assistant",
                "content": asst_content,
                "timestamp": timestamp,
            })
        elif raw_event == "tool":
            tool_name = payload.get("name") or inner.get("name") or "tool"
            record["process_summary"].append({
                "tool": tool_name,
                "action": "call",
                "summary": str(payload.get("summary") or inner.get("summary") or f"Executed tool {tool_name}")[:150],
                "status": payload.get("status") or inner.get("status", "ok"),
                "timestamp": timestamp,
            })
        elif raw_event in ("before_agent_start", "session_stop"):
            record["metadata"]["lifecycle_event"] = raw_event

        return [record]

    else:
        raise AgentAdapterError(
            host=host,
            code="not_installed",
            detail=f"Unsupported host for hook ingestion: {host}",
        )
