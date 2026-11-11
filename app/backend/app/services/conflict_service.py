from typing import Any
from fastapi import HTTPException
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode
from app.services.content_service import _read_content, update_content, create_content, escape_like


def list_conflicts(project_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    clauses = []
    params = []

    if project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM conflicts {where} ORDER BY created_at DESC"

    with db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [row_to_dict(r) for r in rows]


def get_conflict(conflict_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conflict not found")
        return row_to_dict(row)


def create_conflict(
    project_id: str | None,
    existing_id: str,
    proposed: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    cid = new_id("conf")
    now = now_iso()

    with db() as conn:
        # Check existing content exists
        row = conn.execute("SELECT id FROM contents WHERE id = ?", (existing_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Existing content '{existing_id}' not found")

        conn.execute(
            """
            INSERT INTO conflicts (
                id, project_id, existing_id, proposed, reason, status, resolution, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
            """,
            (cid, project_id, existing_id, encode(proposed), reason, now),
        )
        log_audit(actor="daemon", action="conflict.create", target=cid, conn=conn)
    return get_conflict(cid)


def resolve_conflict(
    conflict_id: str,
    resolution: str,
    expected_version: int | None = None,
    merged_title: str | None = None,
    merged_content: str | None = None,
    merged_category: str | None = None,
) -> dict[str, Any]:
    if resolution not in ("keep", "replace", "both", "merge"):
        raise HTTPException(status_code=422, detail="Resolution must be 'keep', 'replace', 'both', or 'merge'")
    with db() as conn:
        # Claim the conflict and apply its content mutation in one transaction.
        # Concurrent web/Agent resolutions must not both create alternatives.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Conflict not found")
        conflict = row_to_dict(row)
        if conflict["status"] == "resolved":
            if conflict["resolution"] != resolution:
                raise HTTPException(status_code=409, detail="Conflict was already resolved differently")
            return conflict
        existing = None
        if conflict.get("existing_id"):
            try:
                existing = _read_content(conn, conflict["existing_id"])
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                existing = None
        if existing is not None and expected_version is not None and existing["version"] != expected_version:
            raise HTTPException(status_code=409, detail="Content changed; review the current version before resolving")
        proposed = conflict["proposed"] if isinstance(conflict["proposed"], dict) else {}
        prop_tags = proposed.get("tags") if isinstance(proposed.get("tags"), list) else None
        prop_quote = (
            proposed.get("quote")
            if isinstance(proposed.get("quote"), str) and proposed.get("quote").strip()
            else None
        )
        now = now_iso()
        if existing is None:
            if resolution in ("replace", "both", "merge"):
                final_title = (
                    ((merged_title or "").strip() or proposed.get("title") or "Untitled")
                    if resolution == "merge"
                    else (proposed.get("title") or "Untitled")
                )
                final_content = (
                    (merged_content.strip() if merged_content is not None and merged_content.strip() else str(proposed.get("content") or ""))
                    if resolution == "merge"
                    else str(proposed.get("content") or "")
                )
                final_category = (
                    (merged_category or proposed.get("category") or "knowledge")
                    if resolution == "merge"
                    else (proposed.get("category") or "knowledge")
                )
                created = create_content(
                    title=final_title,
                    content=final_content,
                    category=final_category,
                    tags=prop_tags,
                    project_id=conflict.get("project_id"),
                    kind="memory",
                    quote=prop_quote,
                    _connection=conn,
                )
                if prop_quote:
                    _attach_conflict_quote_source(
                        conn,
                        content_id=created["id"],
                        project_id=conflict.get("project_id"),
                        prop_tags=prop_tags,
                        prop_quote=prop_quote,
                        now=now,
                    )
        elif resolution == "replace":
            merged_tags = None
            if prop_tags:
                base_tags = [
                    t
                    for t in (existing.get("tags") or [])
                    if isinstance(t, str)
                    and not t.strip().casefold().startswith(("role:", "message_index:", "source_version:", "stance:"))
                ]
                merged_tags = list(dict.fromkeys([*base_tags, *prop_tags]))
            update_content(
                content_id=existing["id"],
                title=proposed["title"],
                content=proposed["content"],
                category=proposed["category"],
                tags=merged_tags,
                expected_version=existing["version"],
                _connection=conn,
            )
            if prop_quote:
                _attach_conflict_quote_source(
                    conn,
                    content_id=existing["id"],
                    project_id=existing.get("project_id"),
                    prop_tags=prop_tags,
                    prop_quote=prop_quote,
                    now=now,
                )
        elif resolution == "merge":
            final_title = (merged_title or "").strip() or proposed.get("title") or existing["title"]
            final_content = (
                merged_content.strip()
                if merged_content is not None and merged_content.strip()
                else f"{existing.get('content', '').strip()}\n\n{proposed.get('content', '').strip()}".strip()
            )
            final_category = merged_category or proposed.get("category") or existing["category"]
            update_content(
                content_id=existing["id"],
                title=final_title,
                content=final_content,
                category=final_category,
                expected_version=existing["version"],
                _connection=conn,
            )
        elif resolution == "both":
            created = create_content(
                title=proposed["title"],
                content=proposed["content"],
                category=proposed["category"],
                tags=prop_tags,
                project_id=existing["project_id"],
                kind="memory",
                related_ids=[existing["id"]],
                _connection=conn,
            )
            if prop_quote:
                _attach_conflict_quote_source(
                    conn,
                    content_id=created["id"],
                    project_id=existing.get("project_id"),
                    prop_tags=prop_tags,
                    prop_quote=prop_quote,
                    now=now,
                )
        conn.execute(
            "UPDATE conflicts SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
            (resolution, now, conflict_id),
        )
        log_audit(actor="user", action="conflict.resolve", target=conflict_id, conn=conn)
        return row_to_dict(conn.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone())


def _attach_conflict_quote_source(
    conn,
    *,
    content_id: str,
    project_id: str | None,
    prop_tags: list[str] | None,
    prop_quote: str,
    now: str,
) -> None:
    updated = conn.execute(
        "UPDATE content_sources SET quote = ? WHERE content_id = ?",
        (prop_quote, content_id),
    )
    if updated.rowcount > 0:
        return
    src_ver = next(
        (
            t.split(":", 1)[1]
            for t in (prop_tags or [])
            if isinstance(t, str) and t.startswith("source_version:") and t.split(":", 1)[1]
        ),
        None,
    )
    src_conv = None
    if src_ver:
        src_conv = conn.execute(
            "SELECT id, title, agent FROM conversations WHERE project_id IS ? AND content_hash = ? LIMIT 1",
            (project_id, src_ver),
        ).fetchone()
    if src_conv is None and project_id is not None:
        src_conv = conn.execute(
            "SELECT id, title, agent FROM conversations WHERE project_id = ? AND messages LIKE ? ESCAPE '\\' ORDER BY updated_at DESC LIMIT 1",
            (project_id, f"%{escape_like(prop_quote)}%"),
        ).fetchone()
    if src_conv is not None:
        conn.execute(
            """
            INSERT INTO content_sources (
                id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at
            ) VALUES (?, ?, ?, 'conversation', ?, ?, 0, ?, ?)
            """,
            (
                new_id("src"),
                content_id,
                src_conv["id"],
                src_conv["title"] or "",
                src_conv["agent"],
                prop_quote,
                now,
            ),
        )
