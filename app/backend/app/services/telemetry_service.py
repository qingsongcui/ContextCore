"""Honest 3-state memory usage telemetry: saved -> provided -> cited.

Invariant: `provided_count` and `cited_count` are strictly observational receipts
and MUST NEVER be fed into retrieval ranking or decay formulas.
"""
from __future__ import annotations

from collections.abc import Iterable
from contextlib import nullcontext
import sqlite3
from typing import Any

from fastapi import HTTPException

from app.database import db, new_id, now_iso

USAGE_STATES = ("saved", "provided", "cited")
EVENT_TYPES = ("provided", "cited")
_SQL_CHUNK_SIZE = 400


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_usage_events (
            id TEXT PRIMARY KEY,
            content_id TEXT NOT NULL,
            content_version INTEGER NOT NULL DEFAULT 1,
            event_type TEXT NOT NULL CHECK(event_type IN ('provided', 'cited')),
            agent TEXT,
            project_id TEXT,
            task_id TEXT,
            session_key TEXT,
            surface TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_usage_content_created
        ON memory_usage_events(content_id, created_at DESC, id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_usage_type_content
        ON memory_usage_events(content_id, event_type, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_memory_usage_session_dedupe
        ON memory_usage_events(content_id, event_type, session_key)
        WHERE session_key IS NOT NULL
        """
    )


def default_usage_summary() -> dict[str, Any]:
    return {
        "state": "saved",
        "provided_count": 0,
        "cited_count": 0,
        "last_provided_at": None,
        "last_provided_agent": None,
        "last_cited_at": None,
        "last_cited_agent": None,
        "last_cited_note": None,
        "recent_events": [],
    }


def _normalize_ids(content_ids: Iterable[str] | str | None) -> list[str]:
    if content_ids is None:
        return []
    if isinstance(content_ids, str):
        clean = content_ids.strip()
        return [clean] if clean else []
    seen: list[str] = []
    seen_set: set[str] = set()
    for raw in content_ids:
        if not isinstance(raw, str):
            continue
        clean = raw.strip()
        if clean and clean not in seen_set:
            seen_set.add(clean)
            seen.append(clean)
    return seen


def _record_events(
    event_type: str,
    content_ids: Iterable[str] | str | None,
    *,
    agent: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    session_key: str | None = None,
    surface: str = "mcp",
    note: str | None = None,
    content_versions: dict[str, int] | None = None,
    dedupe_session: bool = False,
    strict: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unsupported telemetry event_type: {event_type}")
    ids = _normalize_ids(content_ids)
    if not ids:
        return []

    clean_note = (note or "").strip()[:500] or None
    clean_agent = (agent or "").strip() or None
    clean_session = (session_key or "").strip() or None
    clean_surface = (surface or "mcp").strip() or "mcp"
    created_at = now_iso()

    with nullcontext(conn) if conn is not None else db() as active_conn:
        ensure_schema(active_conn)
        meta_by_id: dict[str, tuple[int, str | None]] = {}
        for idx in range(0, len(ids), _SQL_CHUNK_SIZE):
            chunk = ids[idx : idx + _SQL_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rows = active_conn.execute(
                f"SELECT id, version, project_id FROM contents WHERE id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for r in rows:
                meta_by_id[r["id"]] = (int(r["version"] or 1), r["project_id"])

        if strict:
            missing = [cid for cid in ids if cid not in meta_by_id]
            if missing:
                raise HTTPException(status_code=404, detail=f"Memory item '{missing[0]}' not found")

        recorded: list[dict[str, Any]] = []
        for cid in ids:
            if cid not in meta_by_id:
                continue
            db_version, db_project_id = meta_by_id[cid]
            eff_version = int((content_versions or {}).get(cid) or db_version)
            eff_project_id = project_id if project_id is not None else db_project_id

            if dedupe_session and clean_session and event_type == "provided":
                dup = active_conn.execute(
                    """
                    SELECT id FROM memory_usage_events
                    WHERE content_id = ? AND content_version = ? AND event_type = 'provided'
                      AND session_key = ? AND COALESCE(agent, '') = COALESCE(?, '')
                    LIMIT 1
                    """,
                    (cid, eff_version, clean_session, clean_agent),
                ).fetchone()
                if dup:
                    continue

            ev = {
                "id": new_id("mue"),
                "content_id": cid,
                "content_version": eff_version,
                "event_type": event_type,
                "agent": clean_agent,
                "project_id": eff_project_id,
                "task_id": task_id,
                "session_key": clean_session,
                "surface": clean_surface,
                "note": clean_note,
                "created_at": created_at,
            }
            active_conn.execute(
                """
                INSERT INTO memory_usage_events (
                    id, content_id, content_version, event_type, agent,
                    project_id, task_id, session_key, surface, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ev["id"],
                    ev["content_id"],
                    ev["content_version"],
                    ev["event_type"],
                    ev["agent"],
                    ev["project_id"],
                    ev["task_id"],
                    ev["session_key"],
                    ev["surface"],
                    ev["note"],
                    ev["created_at"],
                ),
            )
            recorded.append(ev)
        return recorded


def record_provided(
    content_ids: Iterable[str] | str | None,
    *,
    agent: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    session_key: str | None = None,
    surface: str = "mcp",
    note: str | None = None,
    content_versions: dict[str, int] | None = None,
    dedupe_session: bool = False,
    strict: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Record that one or more memories were returned to an Agent in a read/bootstrap payload."""
    return _record_events(
        "provided",
        content_ids,
        agent=agent,
        project_id=project_id,
        task_id=task_id,
        session_key=session_key,
        surface=surface,
        note=note,
        content_versions=content_versions,
        dedupe_session=dedupe_session,
        strict=strict,
        conn=conn,
    )


def record_cited(
    content_ids: Iterable[str] | str | None,
    *,
    agent: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    session_key: str | None = None,
    surface: str = "mcp",
    note: str | None = None,
    content_versions: dict[str, int] | None = None,
    strict: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    """Record that an Agent explicitly cited one or more memories as decision/execution basis."""
    return _record_events(
        "cited",
        content_ids,
        agent=agent,
        project_id=project_id,
        task_id=task_id,
        session_key=session_key,
        surface=surface,
        note=note,
        content_versions=content_versions,
        dedupe_session=False,
        strict=strict,
        conn=conn,
    )


def get_usage_summaries(
    content_ids: Iterable[str],
    *,
    recent_limit: int = 5,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, Any]]:
    """Batch-compute 3-state (`saved` -> `provided` -> `cited`) telemetry summaries."""
    ids = _normalize_ids(content_ids)
    if not ids:
        return {}

    summaries: dict[str, dict[str, Any]] = {cid: default_usage_summary() for cid in ids}
    bounded_recent = max(0, min(int(recent_limit), 20))

    with nullcontext(conn) if conn is not None else db() as active_conn:
        try:
            for idx in range(0, len(ids), _SQL_CHUNK_SIZE):
                chunk = ids[idx : idx + _SQL_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = active_conn.execute(
                    f"""
                    SELECT id, content_id, content_version, event_type, agent,
                           project_id, task_id, session_key, surface, note, created_at
                    FROM memory_usage_events
                    WHERE content_id IN ({placeholders})
                    ORDER BY created_at DESC, id DESC
                    """,
                    tuple(chunk),
                ).fetchall()
                for r in rows:
                    cid = r["content_id"]
                    summary = summaries.get(cid)
                    if summary is None:
                        continue
                    etype = r["event_type"]
                    if etype == "provided":
                        summary["provided_count"] += 1
                        if summary["last_provided_at"] is None:
                            summary["last_provided_at"] = r["created_at"]
                            summary["last_provided_agent"] = r["agent"]
                    elif etype == "cited":
                        summary["cited_count"] += 1
                        if summary["last_cited_at"] is None:
                            summary["last_cited_at"] = r["created_at"]
                            summary["last_cited_agent"] = r["agent"]
                            summary["last_cited_note"] = r["note"]
                    if len(summary["recent_events"]) < bounded_recent:
                        summary["recent_events"].append(
                            {
                                "id": r["id"],
                                "event_type": etype,
                                "agent": r["agent"],
                                "task_id": r["task_id"],
                                "surface": r["surface"],
                                "note": r["note"],
                                "content_version": r["content_version"],
                                "created_at": r["created_at"],
                            }
                        )
        except sqlite3.OperationalError:
            # Table may not exist yet on an unmigrated raw connection.
            return summaries

    for summary in summaries.values():
        if summary["cited_count"] > 0:
            summary["state"] = "cited"
        elif summary["provided_count"] > 0:
            summary["state"] = "provided"
        else:
            summary["state"] = "saved"

    return summaries


def get_usage_summary(
    content_id: str,
    *,
    recent_limit: int = 5,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    return get_usage_summaries([content_id], recent_limit=recent_limit, conn=conn).get(
        content_id, default_usage_summary()
    )


def delete_usage_events(content_id: str, *, conn: sqlite3.Connection | None = None) -> None:
    with nullcontext(conn) if conn is not None else db() as active_conn:
        try:
            active_conn.execute("DELETE FROM memory_usage_events WHERE content_id = ?", (content_id,))
        except sqlite3.OperationalError:
            pass
