import hashlib
import re
from contextlib import nullcontext
from typing import Any
from fastapi import HTTPException
from app.services.handoff_service import invalidate_handoffs
from app.services import governance_guardrails, telemetry_service
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode, decode
from app.date_filters import upper_date_bound

VALID_CONTENT_STATUSES = ("active", "outdated", "historical")
VALID_CONTENT_CATEGORIES = ("preference", "background", "decision", "task", "lesson", "knowledge")


def _enrich_content_item(
    item: dict[str, Any],
    usage_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    item["pinned"] = "pinned" in tags
    item["scope"] = "global" if not item.get("project_id") else "project"
    if "source_count" not in item or item["source_count"] is None:
        sources = item.get("sources")
        if isinstance(sources, list):
            item["source_count"] = sum(1 for s in sources if isinstance(s, dict) and not s.get("deleted"))
        else:
            item["source_count"] = 0
    raw_app = item.get("applicability")
    if isinstance(raw_app, str):
        raw_app = decode(raw_app, {})
    item["applicability"] = raw_app if isinstance(raw_app, dict) else {}
    item["origin_authority"] = item.get("origin_authority") or "manual"
    item["superseded_by_id"] = item.get("superseded_by_id")
    item["outdated_at"] = item.get("outdated_at")
    item["outdated_reason"] = item.get("outdated_reason")
    item["promoted_from_task_id"] = item.get("promoted_from_task_id")
    item["canonical_key"] = item.get("canonical_key")
    item["usage_summary"] = (
        usage_summary
        or item.get("usage_summary")
        or telemetry_service.default_usage_summary()
    )
    return item


_MULTI_WS_RE = re.compile(r"\s+")
_NEEDS_WS_NORM_RE = re.compile(r"[^\S ]|  ")


def compute_content_hash(project_id: str | None, category: str, title: str, content: str) -> str:
    raw = f"content:{project_id or ''}:{category}:{title.strip()}:{content.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_normalized_content_hash(project_id: str | None, category: str, title: str, content: str) -> str:
    t = (title or "").strip()
    c = (content or "").strip()
    if _NEEDS_WS_NORM_RE.search(t):
        t = _MULTI_WS_RE.sub(" ", t)
    if _NEEDS_WS_NORM_RE.search(c):
        c = _MULTI_WS_RE.sub(" ", c)
    raw = f"content:{project_id or ''}:{category}:{t}:{c}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_content_tombstone(
    conn,
    project_id: str | None,
    category: str,
    title: str,
    content: str,
    *,
    include_cross_category: bool = False,
) -> bool:
    return governance_guardrails.is_tombstoned_memory(
        conn,
        project_id,
        category,
        title,
        content,
        include_cross_category=include_cross_category,
    )


def escape_like(value: str) -> str:
    """Escape %, _, and backslash for SQLite LIKE queries using backslash as escape char."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_excluded_categories(exclude_category: str | list[str] | None) -> list[str]:
    if not exclude_category:
        return []
    if isinstance(exclude_category, str):
        parts = [p.strip() for p in exclude_category.split(",")]
        return [p for p in parts if p]
    return [str(p).strip() for p in exclude_category if str(p or "").strip()]


def _get_content_sources(conn, content_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM content_sources WHERE content_id = ? ORDER BY created_at ASC",
        (content_id,),
    ).fetchall()
    result = []
    for r in rows:
        src_id = r["source_id"]
        src_kind = r["source_kind"]
        title = r["source_title"]
        deleted = bool(r["deleted"])

        if src_kind == "document":
            doc = conn.execute("SELECT id, title FROM contents WHERE id = ?", (src_id,)).fetchone()
            if not doc:
                deleted = True
            elif not title:
                title = doc["title"]
        elif src_kind == "conversation":
            conv = conn.execute("SELECT id, title FROM conversations WHERE id = ?", (src_id,)).fetchone()
            if not conv:
                deleted = True
            elif not title:
                title = conv["title"]

        result.append({
            "id": src_id,
            "kind": src_kind,
            "title": title,
            "agent": r["source_agent"],
            "deleted": deleted,
            "quote": None if deleted else r["quote"],
        })
    return result


def _get_content_sources_batch(
    conn,
    content_ids: list[str],
    *,
    quote_max_chars: int = 240,
) -> dict[str, list[dict[str, Any]]]:
    clean_ids = list(dict.fromkeys(cid for cid in content_ids if cid))
    if not clean_ids:
        return {}
    out: dict[str, list[dict[str, Any]]] = {cid: [] for cid in clean_ids}
    placeholders = ",".join("?" for _ in clean_ids)
    rows = conn.execute(
        f"""
        SELECT cs.content_id,
               cs.source_id,
               cs.source_kind,
               cs.source_title,
               cs.source_agent,
               cs.deleted,
               substr(cs.quote, 1, ?) AS quote_preview,
               doc.id AS doc_exists_id,
               doc.title AS doc_title,
               conv.id AS conv_exists_id,
               conv.title AS conv_title
        FROM content_sources cs
        LEFT JOIN contents doc ON cs.source_kind = 'document' AND doc.id = cs.source_id
        LEFT JOIN conversations conv ON cs.source_kind = 'conversation' AND conv.id = cs.source_id
        WHERE cs.content_id IN ({placeholders})
        ORDER BY cs.created_at ASC
        """,
        (quote_max_chars, *clean_ids),
    ).fetchall()
    for r in rows:
        src_kind = r["source_kind"]
        title = r["source_title"]
        deleted = bool(r["deleted"])
        if src_kind == "document":
            if not r["doc_exists_id"]:
                deleted = True
            elif not title:
                title = r["doc_title"]
        elif src_kind == "conversation":
            if not r["conv_exists_id"]:
                deleted = True
            elif not title:
                title = r["conv_title"]
        out.setdefault(r["content_id"], []).append({
            "id": r["source_id"],
            "kind": src_kind,
            "title": title,
            "agent": r["source_agent"],
            "deleted": deleted,
            "quote": None if deleted else r["quote_preview"],
        })
    return out


def _get_content_versions(conn, content_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM content_versions WHERE content_id = ? ORDER BY version ASC",
        (content_id,),
    ).fetchall()
    result = []
    for r in rows:
        result.append({
            "version": r["version"],
            "content": r["content"],
            "title": r["title"],
            "actor": r["actor"],
            "created_at": r["created_at"],
        })
    return result


def list_content(
    project_id: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    exclude_category: str | list[str] | None = None,
    agent: str | None = None,
    tag: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    pinned: bool | None = None,
    pinned_first: bool = False,
    summary: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200):
        raise HTTPException(422, "limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise HTTPException(422, "offset must be nonnegative")
    if status and status != "all" and status not in VALID_CONTENT_STATUSES:
        raise HTTPException(422, f"Invalid status filter '{status}'")

    clauses = []
    params: list[Any] = []

    if scope == "global":
        clauses.append("c.project_id IS NULL")
    elif scope == "effective" and project_id:
        clauses.append("(c.project_id = ? OR c.project_id IS NULL)")
        params.append(project_id)
    elif project_id:
        clauses.append("c.project_id = ?")
        params.append(project_id)
    if kind:
        clauses.append("c.kind = ?")
        params.append(kind)
    if category:
        clauses.append("c.category = ?")
        params.append(category)
    excluded_cats = _normalize_excluded_categories(exclude_category)
    if excluded_cats:
        if len(excluded_cats) == 1:
            clauses.append("c.category != ?")
            params.append(excluded_cats[0])
        else:
            placeholders = ",".join("?" for _ in excluded_cats)
            clauses.append(f"c.category NOT IN ({placeholders})")
            params.extend(excluded_cats)
    if agent:
        clauses.append("c.agent = ?")
        params.append(agent)
    if status and status != "all":
        clauses.append("c.status = ?")
        params.append(status)
    if since:
        clauses.append("c.created_at >= ?")
        params.append(since)
    if until:
        operator, boundary = upper_date_bound(until)
        clauses.append(f"c.created_at {operator} ?")
        params.append(boundary)
    if query:
        escaped_query = escape_like(query.strip())
        q = f"%{escaped_query}%"
        clauses.append("(c.title LIKE ? ESCAPE '\\' OR c.content LIKE ? ESCAPE '\\')")
        params.extend([q, q])
    if tag:
        clauses.append("""EXISTS (
            SELECT 1 FROM json_each(
                CASE WHEN json_valid(c.tags) THEN
                    CASE WHEN json_type(c.tags)='array' THEN c.tags ELSE '[]' END
                ELSE '[]' END
            ) AS tag_value WHERE tag_value.value = ?
        )""")
        params.append(tag)
    if pinned is True:
        clauses.append("""EXISTS (
            SELECT 1 FROM json_each(
                CASE WHEN json_valid(c.tags) THEN
                    CASE WHEN json_type(c.tags)='array' THEN c.tags ELSE '[]' END
                ELSE '[]' END
            ) AS p_tag WHERE p_tag.value = 'pinned'
        )""")
    elif pinned is False:
        clauses.append("""NOT EXISTS (
            SELECT 1 FROM json_each(
                CASE WHEN json_valid(c.tags) THEN
                    CASE WHEN json_type(c.tags)='array' THEN c.tags ELSE '[]' END
                ELSE '[]' END
            ) AS p_tag WHERE p_tag.value = 'pinned'
        )""")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    columns = (
        "c.id,c.project_id,c.kind,c.category,c.title,c.tags,c.agent,c.status,c.version,"
        "c.related_ids,c.applicability,c.origin_authority,c.superseded_by_id,"
        "c.outdated_at,c.outdated_reason,c.promoted_from_task_id,c.canonical_key,"
        "c.created_at,c.updated_at"
        if summary else "c.*"
    )
    order_clause = (
        "ORDER BY CASE WHEN EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(c.tags) AND json_type(c.tags)='array' THEN c.tags ELSE '[]' END) AS pt WHERE pt.value='pinned') THEN 0 ELSE 1 END ASC, c.updated_at DESC, c.id DESC"
        if pinned_first else "ORDER BY c.updated_at DESC,c.id DESC"
    )
    sql = f"SELECT {columns} FROM contents c {where} {order_clause}"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend((limit, offset))

    with db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        raw_items = [row_to_dict(r) for r in rows]
        usage_map = telemetry_service.get_usage_summaries([it["id"] for it in raw_items], conn=conn)
        result = []
        for item in raw_items:
            if summary:
                item["content"] = ""
                item["sources"] = []
            else:
                item["sources"] = _get_content_sources(conn, item["id"])
            result.append(_enrich_content_item(item, usage_map.get(item["id"])))
        return result


def content_index(
    project_id: str | None = None,
    kind: str | None = None,
    category: str | None = None,
    exclude_category: str | list[str] | None = None,
    agent: str | None = None,
    tag: str | None = None,
    query: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    pinned: bool | None = None,
    pinned_first: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Return one stable metadata page with a short SQL-projected content preview."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise HTTPException(422, "offset must be nonnegative")
    if status and status != "all" and status not in VALID_CONTENT_STATUSES:
        raise HTTPException(422, f"Invalid status filter '{status}'")

    clauses: list[str] = []
    params: list[Any] = []
    if scope == "global":
        clauses.append("c.project_id IS NULL")
    elif scope == "effective" and project_id:
        clauses.append("(c.project_id = ? OR c.project_id IS NULL)")
        params.append(project_id)
    elif project_id:
        clauses.append("c.project_id = ?")
        params.append(project_id)
    if kind:
        clauses.append("c.kind = ?")
        params.append(kind)
    if category:
        clauses.append("c.category = ?")
        params.append(category)
    excluded_cats = _normalize_excluded_categories(exclude_category)
    if excluded_cats:
        if len(excluded_cats) == 1:
            clauses.append("c.category != ?")
            params.append(excluded_cats[0])
        else:
            placeholders = ",".join("?" for _ in excluded_cats)
            clauses.append(f"c.category NOT IN ({placeholders})")
            params.extend(excluded_cats)
    if agent:
        clauses.append("c.agent = ?")
        params.append(agent)
    if status and status != "all":
        clauses.append("c.status = ?")
        params.append(status)
    if since:
        clauses.append("c.created_at >= ?")
        params.append(since)
    if until:
        operator, boundary = upper_date_bound(until)
        clauses.append(f"c.created_at {operator} ?")
        params.append(boundary)
    if query:
        escaped_query = escape_like(query.strip())
        q = f"%{escaped_query}%"
        clauses.append("(c.title LIKE ? ESCAPE '\\' OR c.content LIKE ? ESCAPE '\\')")
        params.extend((q, q))
    if tag:
        # Exact JSON array membership runs in SQLite; tags are never parsed for
        # every row in Python and similarly named tags cannot match.
        clauses.append("""EXISTS (
            SELECT 1 FROM json_each(
                CASE WHEN json_valid(c.tags) THEN
                    CASE WHEN json_type(c.tags)='array' THEN c.tags ELSE '[]' END
                ELSE '[]' END
            ) AS tag_value WHERE tag_value.value = ?
        )""")
        params.append(tag)
    if pinned is True:
        clauses.append("""EXISTS (
            SELECT 1 FROM json_each(
                CASE WHEN json_valid(c.tags) THEN
                    CASE WHEN json_type(c.tags)='array' THEN c.tags ELSE '[]' END
                ELSE '[]' END
            ) AS p_tag WHERE p_tag.value = 'pinned'
        )""")
    elif pinned is False:
        clauses.append("""NOT EXISTS (
            SELECT 1 FROM json_each(
                CASE WHEN json_valid(c.tags) THEN
                    CASE WHEN json_type(c.tags)='array' THEN c.tags ELSE '[]' END
                ELSE '[]' END
            ) AS p_tag WHERE p_tag.value = 'pinned'
        )""")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order_clause = (
        "ORDER BY CASE WHEN EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(c.tags) AND json_type(c.tags)='array' THEN c.tags ELSE '[]' END) AS pt WHERE pt.value='pinned') THEN 0 ELSE 1 END ASC, c.updated_at DESC, c.id DESC"
        if pinned_first else "ORDER BY c.updated_at DESC,c.id DESC"
    )
    with db() as conn:
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) FROM contents c {where}", tuple(params)).fetchone()[0]
        cursor = conn.execute(
            """SELECT c.id,c.project_id,c.kind,c.category,c.title,c.tags,c.agent,c.status,
                      c.version,c.related_ids,c.applicability,c.origin_authority,
                      c.superseded_by_id,c.outdated_at,c.outdated_reason,
                      c.promoted_from_task_id,c.canonical_key,c.created_at,c.updated_at,
                      substr(c.content,1,240) AS content_preview
               FROM contents c """ + where + f" {order_clause} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        raw_items = [row_to_dict(row) for row in cursor]
        page_ids = [it["id"] for it in raw_items]
        sources_map = _get_content_sources_batch(conn, page_ids, quote_max_chars=240)
        usage_map = telemetry_service.get_usage_summaries(page_ids, conn=conn)
        items = []
        for it in raw_items:
            it["sources"] = sources_map.get(it["id"], [])
            items.append(_enrich_content_item(it, usage_map.get(it["id"])))
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _read_content(conn, content_id: str, *, include_versions: bool = True) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Content not found")
    item = row_to_dict(row)
    item["sources"] = _get_content_sources(conn, item["id"])
    item["versions"] = _get_content_versions(conn, item["id"]) if include_versions else None
    usage_map = telemetry_service.get_usage_summaries([item["id"]], conn=conn)
    return _enrich_content_item(item, usage_map.get(item["id"]))


def get_content(content_id: str, *, include_versions: bool = True) -> dict[str, Any]:
    with db() as conn:
        return _read_content(conn, content_id, include_versions=include_versions)


def content_versions_page(content_id: str, *, limit: int = 20, offset: int = 0) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise HTTPException(422, "limit must be between 1 and 200")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise HTTPException(422, "offset must be nonnegative")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM contents WHERE id=?", (content_id,)).fetchone():
            raise HTTPException(404, detail="Content not found")
        total = conn.execute("SELECT COUNT(*) FROM content_versions WHERE content_id=?", (content_id,)).fetchone()[0]
        rows = conn.execute(
            """SELECT version,title,actor,created_at FROM content_versions
               WHERE content_id=? ORDER BY version ASC LIMIT ? OFFSET ?""",
            (content_id, limit, offset),
        )
        items = [dict(row) for row in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_content_version(content_id: str, version: int) -> dict[str, Any]:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise HTTPException(422, "version must be positive")
    with db() as conn:
        row = conn.execute(
            "SELECT version,title,content,actor,created_at FROM content_versions WHERE content_id=? AND version=?",
            (content_id, version),
        ).fetchone()
        if not row:
            if not conn.execute("SELECT 1 FROM contents WHERE id=?", (content_id,)).fetchone():
                raise HTTPException(404, detail="Content not found")
            raise HTTPException(404, detail=f"Version {version} not found")
        return dict(row)


def _attach_provenance_source(
    conn,
    *,
    content_id: str,
    source_id: str | None,
    source_kind: str | None,
    agent: str | None,
    quote: str | None,
    now: str,
) -> None:
    if not source_id or not source_kind:
        return
    if source_kind not in ("conversation", "document"):
        raise HTTPException(status_code=422, detail=f"Unsupported source_kind '{source_kind}'")
    existing = conn.execute(
        """
        SELECT id FROM content_sources
        WHERE content_id = ? AND source_id = ? AND source_kind = ? AND COALESCE(quote, '') = COALESCE(?, '')
        LIMIT 1
        """,
        (content_id, source_id, source_kind, quote),
    ).fetchone()
    if existing:
        return

    source_title = ""
    if source_kind == "conversation":
        conv = conn.execute("SELECT title FROM conversations WHERE id = ?", (source_id,)).fetchone()
        if conv:
            source_title = conv["title"]
    elif source_kind == "document":
        doc = conn.execute("SELECT title FROM contents WHERE id = ?", (source_id,)).fetchone()
        if doc:
            source_title = doc["title"]

    conn.execute(
        """
        INSERT INTO content_sources (
            id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (new_id("src"), content_id, source_id, source_kind, source_title, agent, quote, now),
    )


def _record_current_version_snapshot(
    conn,
    content_id: str,
    actor: str,
    created_at: str,
) -> None:
    """Persist a content_versions snapshot matching contents.version for a content row."""
    conn.execute(
        """
        INSERT INTO content_versions (
            id, content_id, version, title, content, actor, created_at
        )
        SELECT ?, id, version, title, content, ?, ?
        FROM contents WHERE id = ?
        """,
        (new_id("ver"), actor, created_at, content_id),
    )


def create_content(
    title: str,
    content: str = "",
    project_id: str | None = None,
    kind: str = "memory",
    category: str = "knowledge",
    tags: list[str] | None = None,
    pinned: bool = False,
    scope: str | None = None,
    agent: str | None = None,
    source_id: str | None = None,
    source_kind: str | None = None,
    quote: str | None = None,
    related_ids: list[str] | None = None,
    supersedes_id: str | None = None,
    applicability: dict[str, Any] | None = None,
    origin_authority: str | None = None,
    promoted_from_task_id: str | None = None,
    *,
    _connection=None,
    _deduplicate_import: bool = False,
) -> dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=422, detail="Title cannot be empty")

    if scope == "global":
        project_id = None

    effective_tags = list(tags or [])
    if pinned and "pinned" not in effective_tags:
        effective_tags.append("pinned")

    eff_related = list(related_ids or [])
    if supersedes_id and supersedes_id not in eff_related:
        eff_related.append(supersedes_id)

    norm_applicability = governance_guardrails.normalize_applicability(applicability)
    eff_origin = (origin_authority or ("import" if agent == "import" else "manual")).strip() or "manual"
    c_hash = compute_content_hash(project_id, category, clean_title, content)
    can_key = governance_guardrails.canonical_memory_key(project_id, category, clean_title, content)
    now = now_iso()
    cid = new_id("cnt")

    with nullcontext(_connection) if _connection is not None else db() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if project_id:
            proj_row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj_row:
                raise HTTPException(status_code=404, detail=f"Target project '{project_id}' not found")
        # Check tombstone
        if _has_content_tombstone(conn, project_id, category, clean_title, content):
            raise HTTPException(status_code=410, detail="Content was previously deleted and cannot be recreated")

        if _deduplicate_import and kind == 'document' and agent == 'import':
            existing = conn.execute(
                "SELECT id FROM contents WHERE content_hash=? AND kind='document' AND agent='import' AND status='active' ORDER BY id LIMIT 1",
                (c_hash,),
            ).fetchone()
            if existing:
                return _read_content(conn, existing['id']) if _connection is not None else get_content(existing['id'])

        # Validate superseding target before insert
        if supersedes_id:
            old = conn.execute("SELECT * FROM contents WHERE id = ?", (supersedes_id,)).fetchone()
            if not old:
                raise HTTPException(status_code=404, detail=f"Target content for supersession '{supersedes_id}' not found")
            if old["status"] != "active":
                raise HTTPException(status_code=409, detail=f"Target content for supersession '{supersedes_id}' is not active (status: {old['status']})")
            if old["project_id"] != project_id:
                raise HTTPException(status_code=400, detail="Cannot supersede content from another project domain")

        conn.execute(
            """
            INSERT INTO contents (
                id, project_id, kind, category, title, content, tags, agent,
                status, version, related_ids, content_hash,
                applicability, origin_authority, promoted_from_task_id, canonical_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cid,
                project_id,
                kind,
                category,
                clean_title,
                content,
                encode(effective_tags),
                agent,
                encode(eff_related),
                c_hash,
                encode(norm_applicability),
                eff_origin,
                promoted_from_task_id,
                can_key,
                now,
                now,
            ),
        )

        if supersedes_id:
            conn.execute(
                "UPDATE contents SET status = 'historical', superseded_by_id = ?, version = COALESCE(version, 1) + 1, updated_at = ? WHERE id = ?",
                (cid, now, supersedes_id),
            )
            _record_current_version_snapshot(conn, supersedes_id, agent or "user", now)
            invalidate_handoffs(conn, supersedes_id)
            log_audit(actor=agent or "user", action="content.supersede", target=supersedes_id, conn=conn)

        # Record initial version
        conn.execute(
            """
            INSERT INTO content_versions (
                id, content_id, version, title, content, actor, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (new_id("ver"), cid, clean_title, content, agent or "user", now),
        )

        # Record source if provided
        if source_id and source_kind:
            _attach_provenance_source(
                conn,
                content_id=cid,
                source_id=source_id,
                source_kind=source_kind,
                agent=agent,
                quote=quote,
                now=now,
            )

        if project_id:
            from app.services import project_service
            project_service.touch_project_activity(conn, project_id, now)
        log_audit(actor=agent or "user", action="content.create", target=cid, conn=conn)
        if _connection is not None:
            return _read_content(conn, cid)
    return get_content(cid)


def update_content(
    content_id: str,
    *,
    title: str | None = None,
    content: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    pinned: bool | None = None,
    scope: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    project_id: str | None = None,
    applicability: dict[str, Any] | None = None,
    origin_authority: str | None = None,
    outdated_reason: str | None = None,
    expected_version: int,
    _connection=None,
) -> dict[str, Any]:
    if status is not None and status not in VALID_CONTENT_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid content status '{status}'")

    with nullcontext(_connection) if _connection is not None else db() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")

        current = row_to_dict(row)
        current_version = current["version"]

        if current_version != expected_version:
            raise HTTPException(
                status_code=409,
                detail=f"Version conflict: current version is {current_version}, expected {expected_version}",
            )

        new_version = current_version + 1
        now = now_iso()

        new_title = title.strip() if title is not None else current["title"]
        if not new_title:
            raise HTTPException(status_code=422, detail="Title cannot be empty")
        new_content = content if content is not None else current["content"]
        new_category = category if category is not None else current["category"]
        new_tags = list(tags if tags is not None else current.get("tags", []))
        if pinned is True and "pinned" not in new_tags:
            new_tags.append("pinned")
        elif pinned is False and "pinned" in new_tags:
            new_tags = [t for t in new_tags if t != "pinned"]
        new_agent = agent if agent is not None else current.get("agent")
        new_status = status if status is not None else current["status"]
        if current["status"] == "historical" and current.get("superseded_by_id") and new_status == "active":
            raise HTTPException(
                status_code=409,
                detail="Superseded historical content cannot be reactivated directly; use version restore or create a new correction",
            )
        if scope == "global":
            new_project_id = None
        elif project_id is not None:
            proj_row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj_row:
                raise HTTPException(status_code=404, detail=f"Target project '{project_id}' not found")
            new_project_id = project_id
        else:
            new_project_id = current.get("project_id")

        new_applicability = (
            governance_guardrails.normalize_applicability(applicability)
            if applicability is not None
            else (current.get("applicability") if isinstance(current.get("applicability"), dict) else {})
        )
        new_origin = (origin_authority or current.get("origin_authority") or "manual").strip() or "manual"
        if new_status == "outdated":
            new_outdated_at = current.get("outdated_at") or now
            new_outdated_reason = (
                (outdated_reason.strip() or None)
                if outdated_reason is not None
                else current.get("outdated_reason")
            )
        elif new_status == "active":
            new_outdated_at = None
            new_outdated_reason = None
        else:
            new_outdated_at = current.get("outdated_at")
            new_outdated_reason = (
                (outdated_reason.strip() or None)
                if outdated_reason is not None
                else current.get("outdated_reason")
            )

        c_hash = compute_content_hash(new_project_id, new_category, new_title, new_content)
        can_key = governance_guardrails.canonical_memory_key(new_project_id, new_category, new_title, new_content)

        # Tombstone check on new content body
        if _has_content_tombstone(conn, new_project_id, new_category, new_title, new_content):
            raise HTTPException(status_code=410, detail="Content body was previously deleted and tombstoned")

        cursor = conn.execute(
            """
            UPDATE contents SET
                title = ?,
                content = ?,
                category = ?,
                tags = ?,
                agent = ?,
                status = ?,
                project_id = ?,
                version = ?,
                content_hash = ?,
                applicability = ?,
                origin_authority = ?,
                outdated_at = ?,
                outdated_reason = ?,
                canonical_key = ?,
                updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (
                new_title,
                new_content,
                new_category,
                encode(new_tags),
                new_agent,
                new_status,
                new_project_id,
                new_version,
                c_hash,
                encode(new_applicability),
                new_origin,
                new_outdated_at,
                new_outdated_reason,
                can_key,
                now,
                content_id,
                expected_version,
            ),
        )
        if cursor.rowcount == 0:
            check = conn.execute("SELECT version FROM contents WHERE id = ?", (content_id,)).fetchone()
            if check:
                raise HTTPException(
                    status_code=409,
                    detail=f"Version conflict: current version is {check['version']}, expected {expected_version}",
                )
            raise HTTPException(status_code=404, detail="Content not found")

        # Record new version snapshot
        conn.execute(
            """
            INSERT INTO content_versions (
                id, content_id, version, title, content, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("ver"), content_id, new_version, new_title, new_content, agent or "user", now),
        )

        if new_status != current["status"]:
            invalidate_handoffs(conn, content_id)

        if new_project_id:
            from app.services import project_service
            project_service.touch_project_activity(conn, new_project_id, now)
        if project_id is not None and project_id != current.get("project_id"):
            log_audit(actor=agent or "user", action="content.reassign", target=f"{content_id}->{project_id}", conn=conn)
        else:
            log_audit(actor=agent or "user", action="content.update", target=content_id, conn=conn)
        if _connection is not None:
            return _read_content(conn, content_id)
    return get_content(content_id)


def mark_outdated(
    content_id: str,
    *,
    reason: str | None = None,
    actor: str = "user",
    expected_version: int | None = None,
    _connection=None,
) -> dict[str, Any]:
    """Retire a memory item to `status='outdated'` so daily Agent injection halts while audit history stays intact."""
    clean_reason = (reason or "").strip() or None
    with nullcontext(_connection) if _connection is not None else db() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        cur_ver = int(row["version"] or 1)
        if expected_version is not None and cur_ver != expected_version:
            raise HTTPException(
                status_code=409,
                detail=f"Version conflict: current version is {cur_ver}, expected {expected_version}",
            )
        new_ver = cur_ver + 1
        now = now_iso()
        updated = conn.execute(
            """
            UPDATE contents
            SET status = 'outdated', outdated_at = ?, outdated_reason = ?, version = ?, updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (now, clean_reason, new_ver, now, content_id, cur_ver),
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=409, detail="Concurrent version conflict while marking content outdated")
        conn.execute(
            """
            INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("ver"), content_id, new_ver, row["title"], row["content"], actor or "user", now),
        )
        invalidate_handoffs(conn, content_id)
        log_audit(actor=actor or "user", action="content.mark_outdated", target=content_id, conn=conn)
        if _connection is not None:
            return _read_content(conn, content_id)
    return get_content(content_id)


def correct_content(
    content_id: str,
    *,
    content: str,
    title: str | None = None,
    category: str | None = None,
    tags: list[str] | None = None,
    pinned: bool | None = None,
    applicability: dict[str, Any] | None = None,
    actor: str = "user",
    quote: str | None = None,
    source_id: str | None = None,
    source_kind: str | None = None,
    expected_version: int | None = None,
    _connection=None,
) -> dict[str, Any]:
    """Explicitly correct a memory: create a new active `user_explicit` record and retire the old one to `historical`."""
    clean_content = (content or "").strip()
    if not clean_content:
        raise HTTPException(status_code=422, detail="Corrected content cannot be empty")

    with nullcontext(_connection) if _connection is not None else db() as conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        old_row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
        if not old_row:
            raise HTTPException(status_code=404, detail="Content not found")

        old = row_to_dict(old_row)
        cur_ver = int(old["version"] or 1)
        if expected_version is not None and cur_ver != expected_version:
            raise HTTPException(
                status_code=409,
                detail=f"Version conflict: current version is {cur_ver}, expected {expected_version}",
            )
        if old["status"] == "historical":
            raise HTTPException(
                status_code=409,
                detail=f"Content '{content_id}' is already historical and cannot be superseded again",
            )

        new_title = (title or "").strip() or old["title"]
        new_category = category or old["category"]
        new_project_id = old.get("project_id")
        new_kind = old.get("kind") or "memory"

        base_tags = list(old.get("tags") or [])
        new_tags = list(tags) if tags is not None else list(base_tags)
        if pinned is True and "pinned" not in new_tags:
            new_tags.append("pinned")
        elif pinned is False and "pinned" in new_tags:
            new_tags = [t for t in new_tags if t != "pinned"]

        new_applicability = (
            governance_guardrails.normalize_applicability(applicability)
            if applicability is not None
            else (old.get("applicability") if isinstance(old.get("applicability"), dict) else {})
        )
        new_related = list(dict.fromkeys([*(old.get("related_ids") or []), content_id]))
        c_hash = compute_content_hash(new_project_id, new_category, new_title, clean_content)
        can_key = governance_guardrails.canonical_memory_key(new_project_id, new_category, new_title, clean_content)

        if _has_content_tombstone(conn, new_project_id, new_category, new_title, clean_content):
            raise HTTPException(status_code=410, detail="Corrected content body was previously deleted and tombstoned")

        now = now_iso()
        new_cid = new_id("cnt")
        conn.execute(
            """
            INSERT INTO contents (
                id, project_id, kind, category, title, content, tags, agent,
                status, version, related_ids, content_hash,
                applicability, origin_authority, promoted_from_task_id, canonical_key,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, 'user_explicit', ?, ?, ?, ?)
            """,
            (
                new_cid,
                new_project_id,
                new_kind,
                new_category,
                new_title,
                clean_content,
                encode(new_tags),
                actor or "user",
                encode(new_related),
                c_hash,
                encode(new_applicability),
                old.get("promoted_from_task_id"),
                can_key,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (new_id("ver"), new_cid, new_title, clean_content, actor or "user", now),
        )

        updated_old = conn.execute(
            """
            UPDATE contents
            SET status = 'historical', superseded_by_id = ?, version = COALESCE(version, 1) + 1, updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (new_cid, now, content_id, cur_ver),
        )
        if updated_old.rowcount != 1:
            raise HTTPException(status_code=409, detail="Concurrent version conflict while correcting content")
        _record_current_version_snapshot(conn, content_id, actor or "user", now)

        if source_id and source_kind:
            _attach_provenance_source(
                conn,
                content_id=new_cid,
                source_id=source_id,
                source_kind=source_kind,
                agent=actor or "user",
                quote=quote,
                now=now,
            )
        else:
            old_sources = conn.execute(
                "SELECT * FROM content_sources WHERE content_id = ? ORDER BY created_at ASC",
                (content_id,),
            ).fetchall()
            for src in old_sources:
                conn.execute(
                    """
                    INSERT INTO content_sources (
                        id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("src"),
                        new_cid,
                        src["source_id"],
                        src["source_kind"],
                        src["source_title"],
                        src["source_agent"],
                        src["deleted"],
                        src["quote"],
                        now,
                    ),
                )

        invalidate_handoffs(conn, content_id)
        log_audit(actor=actor or "user", action="content.supersede", target=content_id, conn=conn)
        log_audit(actor=actor or "user", action="content.correct", target=new_cid, conn=conn)
        if _connection is not None:
            return _read_content(conn, new_cid)
    return get_content(new_cid)


def _create_conflict_safe(
    conn,
    *,
    project_id: str | None,
    existing_id: str,
    proposed: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Create a pending conflict via `conflict_service.create_conflict` without SQLite write-lock deadlock."""
    from app.services import conflict_service

    if conn is None or not conn.in_transaction:
        return conflict_service.create_conflict(
            project_id=project_id,
            existing_id=existing_id,
            proposed=proposed,
            reason=reason,
        )

    cid = new_id("conf")
    now = now_iso()
    conn.execute(
        """
        INSERT INTO conflicts (
            id, project_id, existing_id, proposed, reason, status, resolution, created_at, resolved_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, NULL)
        """,
        (cid, project_id, existing_id, encode(proposed), reason, now),
    )
    log_audit(actor="daemon", action="conflict.create", target=cid, conn=conn)
    row = conn.execute("SELECT * FROM conflicts WHERE id = ?", (cid,)).fetchone()
    return row_to_dict(row)


def reconcile_long_term_memory(
    *,
    title: str,
    content: str,
    project_id: str | None = None,
    category: str = "knowledge",
    tags: list[str] | None = None,
    pinned: bool = False,
    scope: str | None = None,
    applicability: dict[str, Any] | None = None,
    origin_authority: str | None = None,
    agent: str | None = None,
    actor: str | None = None,
    is_agent_write: bool = True,
    is_user_correction: bool = False,
    supersedes_id: str | None = None,
    ambiguous: bool = False,
    source_id: str | None = None,
    source_kind: str | None = None,
    quote: str | None = None,
    promoted_from_task_id: str | None = None,
    related_ids: list[str] | None = None,
    cwd: str | None = None,
    auto_route_temp_constraint: bool = True,
    _connection=None,
) -> dict[str, Any]:
    """Governed long-term memory reconciliation state machine:
    `ROUTE_TO_WORKING_TASK` | `REJECTED` | `DROP_TOMBSTONED` | `NOOP` | `SUPERSEDE` | `OPEN_CONFLICT` | `ADD`.
    """
    clean_title = (title or "").strip()
    clean_content = (content or "").strip()
    if not clean_title or not clean_content:
        raise HTTPException(status_code=422, detail="Memory title and content are required")

    if scope == "global":
        project_id = None

    effective_actor = (actor or agent or ("user" if not is_agent_write else "agent")).strip()
    effective_tags = list(tags or [])
    if pinned and "pinned" not in effective_tags:
        effective_tags.append("pinned")

    # Gate 0: Write Admission Filter
    verdict = governance_guardrails.classify_write_candidate(
        title=clean_title,
        content=clean_content,
        category=category,
        origin_authority=origin_authority,
        is_agent_write=is_agent_write,
        actor=effective_actor,
        kind="memory",
    )
    if verdict["action"] == "route_to_working_task":
        routed_task = None
        if auto_route_temp_constraint and project_id:
            from app.services import working_memory_service

            routed_task = working_memory_service.add_temp_constraint_to_active_task(
                project_id,
                verdict["extracted_constraint"] or clean_content,
                cwd=cwd,
                title_hint=clean_title,
                agent=agent or effective_actor,
                conn=_connection,
            )
        return {
            "ok": True,
            "action": "ROUTE_TO_WORKING_TASK",
            "status": "routed_to_working_task",
            "reason": verdict["reason"],
            "working_task": routed_task,
            "memory": None,
        }

    if not verdict["allowed"]:
        return {
            "ok": False,
            "action": "REJECTED",
            "status": "rejected",
            "reason": verdict["reason"],
            "memory": None,
        }

    eff_origin = verdict["origin_authority"]
    if is_user_correction and eff_origin == "manual":
        eff_origin = "user_explicit"

    # Inspect tombstones and active candidates
    conflict_target_id: str | None = None
    conflict_reason: str | None = None
    noop_target_id: str | None = None
    supersede_target_id: str | None = supersedes_id

    with nullcontext(_connection) if _connection is not None else db() as read_conn:
        if _has_content_tombstone(
            read_conn,
            project_id,
            category,
            clean_title,
            clean_content,
            include_cross_category=True,
        ):
            return {
                "ok": False,
                "action": "DROP_TOMBSTONED",
                "status": "tombstoned",
                "reason": "memory_tombstoned",
                "memory": None,
            }

        if supersede_target_id:
            target = read_conn.execute(
                "SELECT * FROM contents WHERE id = ?", (supersede_target_id,)
            ).fetchone()
            if not target:
                raise HTTPException(status_code=404, detail=f"Target content '{supersede_target_id}' not found")
            if target["status"] != "active":
                raise HTTPException(
                    status_code=409,
                    detail=f"Target content '{supersede_target_id}' is not active (status: {target['status']})",
                )
            if ambiguous:
                conflict_target_id = target["id"]
                conflict_reason = f"Ambiguous supersession proposed against '{target['title']}'"
        else:
            candidates = read_conn.execute(
                """
                SELECT * FROM contents
                WHERE project_id IS ? AND kind = 'memory' AND status = 'active' AND category != 'task'
                ORDER BY updated_at DESC, id DESC
                LIMIT 100
                """,
                (project_id,),
            ).fetchall()

            for cand_row in candidates:
                cand = row_to_dict(cand_row)
                relation, reason = governance_guardrails.compare_memory_claims(
                    cand, clean_title, clean_content, category
                )
                if relation == "equivalent":
                    noop_target_id = cand["id"]
                    break
                if relation == "contradiction":
                    if is_user_correction and not ambiguous:
                        supersede_target_id = cand["id"]
                    elif (
                        ambiguous
                        or cand["category"] in governance_guardrails.PROTECTED_CONFLICT_CATEGORIES
                        or category in governance_guardrails.PROTECTED_CONFLICT_CATEGORIES
                        or eff_origin not in governance_guardrails.AUTHORITATIVE_ORIGINS
                    ):
                        conflict_target_id = cand["id"]
                        conflict_reason = reason or f"Contradicts active {cand['category']} '{cand['title']}'"
                    else:
                        supersede_target_id = cand["id"]
                    break

    if noop_target_id:
        with nullcontext(_connection) if _connection is not None else db() as write_conn:
            if source_id and source_kind:
                if not write_conn.in_transaction:
                    write_conn.execute("BEGIN IMMEDIATE")
                _attach_provenance_source(
                    write_conn,
                    content_id=noop_target_id,
                    source_id=source_id,
                    source_kind=source_kind,
                    agent=agent or effective_actor,
                    quote=quote,
                    now=now_iso(),
                )
            existing_mem = _read_content(write_conn, noop_target_id)
        return {
            "ok": True,
            "action": "NOOP",
            "status": "active",
            "id": existing_mem["id"],
            "memory": existing_mem,
        }

    if conflict_target_id:
        proposed_payload: dict[str, Any] = {
            "title": clean_title,
            "content": clean_content,
            "category": category,
            "tags": effective_tags,
            "quote": quote,
            "origin_authority": eff_origin,
            "pinned": bool(pinned or "pinned" in effective_tags),
        }
        if promoted_from_task_id:
            proposed_payload["promoted_from_task_id"] = promoted_from_task_id
        if applicability:
            proposed_payload["applicability"] = governance_guardrails.normalize_applicability(applicability)
        conflict_record = _create_conflict_safe(
            _connection,
            project_id=project_id,
            existing_id=conflict_target_id,
            proposed=proposed_payload,
            reason=conflict_reason or "Conflicting memory requires user review",
        )
        return {
            "ok": True,
            "action": "OPEN_CONFLICT",
            "status": "conflict_pending",
            "id": conflict_target_id,
            "existing_id": conflict_target_id,
            "conflict_id": conflict_record["id"],
            "conflict": conflict_record,
            "memory": None,
        }

    created = create_content(
        title=clean_title,
        content=clean_content,
        project_id=project_id,
        kind="memory",
        category=category,
        tags=effective_tags,
        pinned=pinned,
        agent=agent or effective_actor,
        source_id=source_id,
        source_kind=source_kind,
        quote=quote,
        related_ids=related_ids,
        supersedes_id=supersede_target_id,
        applicability=applicability,
        origin_authority=eff_origin,
        promoted_from_task_id=promoted_from_task_id,
        _connection=_connection,
    )
    action_name = "SUPERSEDE" if supersede_target_id else "ADD"
    return {
        "ok": True,
        "action": action_name,
        "status": created["status"],
        "id": created["id"],
        "superseded_id": supersede_target_id,
        "memory": created,
    }


def _tombstone_content_history(conn, item, actor: str, reason: str, now: str) -> None:
    """Retain deletion identities, never historical bodies, before cascading cleanup."""
    def remember(digest, project_id):
        conn.execute(
            """INSERT OR IGNORE INTO tombstones
               (id,entity_type,dedupe_key,scope_project_id,deleted_at,deleted_by,reason)
               VALUES (?,'content',?,?,?,?,?)""",
            (new_id("tomb"), digest, project_id, now, actor, reason),
        )

    remember(item["id"], item.get("project_id"))
    raw_cur = item.get("content_hash") or compute_content_hash(
        item.get("project_id"), item["category"], item["title"], item["content"]
    )
    remember(raw_cur, item.get("project_id"))
    norm_cur = compute_normalized_content_hash(
        item.get("project_id"), item["category"], item["title"], item["content"]
    )
    if norm_hash_diff := (norm_cur if norm_cur != raw_cur else None):
        remember(norm_hash_diff, item.get("project_id"))
    for identity in conn.execute(
        "SELECT content_hash,project_id FROM content_identity_history WHERE content_id=?", (item["id"],),
    ):
        remember(identity["content_hash"], identity["project_id"])
    # Legacy version rows contain bodies but no historical project/category.
    # Protect their known current-domain identities without loading all bodies.
    for version in conn.execute(
        "SELECT title,content FROM content_versions WHERE content_id=? ORDER BY version", (item["id"],),
    ):
        v_raw = compute_content_hash(item.get("project_id"), item["category"], version["title"], version["content"])
        remember(v_raw, item.get("project_id"))
        v_norm = compute_normalized_content_hash(
            item.get("project_id"), item["category"], version["title"], version["content"]
        )
        if v_norm != v_raw:
            remember(v_norm, item.get("project_id"))
    governance_guardrails.record_memory_tombstones(conn, item, actor=actor, reason=reason, now=now)


def delete_content(content_id: str, actor: str = "user", expected_version: int | None = None) -> dict[str, bool]:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")

        if expected_version is not None and row["version"] != expected_version:
            raise HTTPException(
                status_code=409,
                detail=f"Version conflict: current version is {row['version']}, expected {expected_version}",
            )

        item = row_to_dict(row)
        _tombstone_content_history(conn, item, actor, "user_deleted", now_iso())
        # Mark sources pointing to this content as deleted
        conn.execute(
            "UPDATE content_sources SET deleted = 1 WHERE source_id = ?", (content_id,)
        )

        invalidate_handoffs(conn, content_id)
        telemetry_service.delete_usage_events(content_id, conn=conn)
        # Delete versions and content
        conn.execute("DELETE FROM content_versions WHERE content_id = ?", (content_id,))
        conn.execute("DELETE FROM content_sources WHERE content_id = ?", (content_id,))
        if expected_version is None:
            deleted = conn.execute("DELETE FROM contents WHERE id = ?", (content_id,))
        else:
            deleted = conn.execute(
                "DELETE FROM contents WHERE id = ? AND version = ?", (content_id, expected_version)
            )
        if deleted.rowcount != 1:
            current = conn.execute("SELECT version FROM contents WHERE id = ?", (content_id,)).fetchone()
            if current:
                raise HTTPException(
                    status_code=409,
                    detail=f"Version conflict: current version is {current['version']}, expected {expected_version}",
                )
            raise HTTPException(status_code=404, detail="Content not found")

        log_audit(actor=actor, action="content.delete", target=content_id, conn=conn)
        return {"ok": True}


def restore_version(content_id: str, version: int, expected_version: int) -> dict[str, Any]:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")

        current = row_to_dict(row)
        current_version = current["version"]

        if current_version != expected_version:
            raise HTTPException(
                status_code=409,
                detail=f"Version conflict: current version is {current_version}, expected {expected_version}",
            )

        ver_row = conn.execute(
            "SELECT * FROM content_versions WHERE content_id = ? AND version = ?",
            (content_id, version),
        ).fetchone()
        if not ver_row:
            raise HTTPException(status_code=404, detail=f"Version {version} not found")

        now = now_iso()
        new_version = current_version + 1
        restored_title = ver_row["title"]
        restored_content = ver_row["content"]
        cur_cat = current.get("category", "knowledge")

        c_hash = compute_content_hash(current.get("project_id"), cur_cat, restored_title, restored_content)
        can_key = governance_guardrails.canonical_memory_key(
            current.get("project_id"), cur_cat, restored_title, restored_content
        )
        if _has_content_tombstone(conn, current.get("project_id"), cur_cat, restored_title, restored_content):
            raise HTTPException(status_code=410, detail="Restored content snapshot was previously deleted and tombstoned")

        cursor = conn.execute(
            """
            UPDATE contents SET
                title = ?,
                content = ?,
                version = ?,
                content_hash = ?,
                canonical_key = ?,
                updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (restored_title, restored_content, new_version, c_hash, can_key, now, content_id, expected_version),
        )
        if cursor.rowcount == 0:
            check = conn.execute("SELECT version FROM contents WHERE id = ?", (content_id,)).fetchone()
            if check:
                raise HTTPException(
                    status_code=409,
                    detail=f"Version conflict: current version is {check['version']}, expected {expected_version}",
                )
            raise HTTPException(status_code=404, detail="Content not found")

        conn.execute(
            """
            INSERT INTO content_versions (
                id, content_id, version, title, content, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (new_id("ver"), content_id, new_version, restored_title, restored_content, f"restore_v{version}", now),
        )

        log_audit(actor="user", action="content.restore", target=f"{content_id}@v{version}", conn=conn)
    return get_content(content_id)


def batch_content(
    ids: list[str],
    action: str,
    tags: list[str] | None = None,
    category: str | None = None,
    expected_versions: dict[str, int] | None = None,
) -> dict[str, Any]:
    if not ids:
        return {"ok": True, "count": 0}

    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=422, detail="ids must not contain duplicates")

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = now_iso()
        count = 0

        if expected_versions is not None and set(expected_versions) != set(ids):
            raise HTTPException(status_code=422, detail="expected_versions must match every requested id")

        # Validate the complete selection before mutation without retaining bodies.
        for cid in ids:
            row = conn.execute("SELECT version FROM contents WHERE id = ?", (cid,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Content item '{cid}' not found")
            expected = expected_versions.get(cid) if expected_versions is not None else None
            if expected is not None and row["version"] != expected:
                raise HTTPException(
                    status_code=409,
                    detail=f"Version conflict for {cid}: current version is {row['version']}, expected {expected}",
                )

        if action == "delete":
            for cid in ids:
                row = conn.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone()
                item = row_to_dict(row)
                _tombstone_content_history(conn, item, "user", "batch_delete", now)
                invalidate_handoffs(conn, cid)
                telemetry_service.delete_usage_events(cid, conn=conn)
                conn.execute("UPDATE content_sources SET deleted = 1 WHERE source_id = ?", (cid,))
                conn.execute("DELETE FROM content_versions WHERE content_id = ?", (cid,))
                conn.execute("DELETE FROM content_sources WHERE content_id = ?", (cid,))
                if expected_versions is None:
                    deleted = conn.execute("DELETE FROM contents WHERE id = ?", (cid,))
                else:
                    deleted = conn.execute(
                        "DELETE FROM contents WHERE id = ? AND version = ?", (cid, expected_versions[cid])
                    )
                if deleted.rowcount != 1:
                    raise HTTPException(status_code=409, detail=f"Version conflict for {cid}")
                log_audit(actor="user", action="content.batch_delete", target=cid, conn=conn)
                count += 1

        elif action == "retag":
            if tags is None:
                raise HTTPException(status_code=422, detail="tags parameter is required for retag action")
            for cid in ids:
                row = conn.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone()
                new_ver = row["version"] + 1
                if expected_versions is not None:
                    updated = conn.execute(
                        "UPDATE contents SET tags = ?, version = ?, updated_at = ? WHERE id = ? AND version = ?",
                        (encode(tags), new_ver, now, cid, row["version"]),
                    )
                    if updated.rowcount != 1:
                        raise HTTPException(status_code=409, detail=f"Version conflict for {cid}")
                else:
                    conn.execute(
                        "UPDATE contents SET tags = ?, version = ?, updated_at = ? WHERE id = ?",
                        (encode(tags), new_ver, now, cid),
                    )
                conn.execute(
                    """
                    INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
                    VALUES (?, ?, ?, ?, ?, 'batch_retag', ?)
                    """,
                    (new_id("ver"), cid, new_ver, row["title"], row["content"], now),
                )
                log_audit(actor="user", action="content.batch_retag", target=cid, conn=conn)
                count += 1

        elif action == "recategorize":
            if not category:
                raise HTTPException(status_code=422, detail="category parameter is required for recategorize action")
            for cid in ids:
                row = conn.execute("SELECT * FROM contents WHERE id=?", (cid,)).fetchone()
                new_ver = row["version"] + 1
                c_hash = compute_content_hash(row["project_id"], category, row["title"], row["content"])
                can_key = governance_guardrails.canonical_memory_key(
                    row["project_id"], category, row["title"], row["content"] or ""
                )
                if _has_content_tombstone(conn, row["project_id"], category, row["title"], row["content"]):
                    raise HTTPException(status_code=410, detail="Category change would restore permanently deleted content")
                if expected_versions is not None:
                    updated = conn.execute(
                        "UPDATE contents SET category = ?, version = ?, content_hash = ?, canonical_key = ?, updated_at = ? WHERE id = ? AND version = ?",
                        (category, new_ver, c_hash, can_key, now, cid, row["version"]),
                    )
                    if updated.rowcount != 1:
                        raise HTTPException(status_code=409, detail=f"Version conflict for {cid}")
                else:
                    conn.execute(
                        "UPDATE contents SET category = ?, version = ?, content_hash = ?, canonical_key = ?, updated_at = ? WHERE id = ?",
                        (category, new_ver, c_hash, can_key, now, cid),
                    )
                conn.execute(
                    """
                    INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
                    VALUES (?, ?, ?, ?, ?, 'batch_recategorize', ?)
                    """,
                    (new_id("ver"), cid, new_ver, row["title"], row["content"], now),
                )
                log_audit(actor="user", action="content.batch_recategorize", target=cid, conn=conn)
                count += 1

        else:
            raise HTTPException(status_code=422, detail=f"Unsupported batch action: {action}")

        return {"ok": True, "count": count}


def merge_content(
    ids: list[str],
    title: str,
    content: str,
    project_id: str | None = None,
    expected_versions: dict[str, int] | None = None,
    category: str | None = None,
) -> dict[str, Any]:
    if len(ids) < 2:
        raise HTTPException(status_code=422, detail="At least 2 content items are required to merge")
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=422, detail="Merged title cannot be empty")
    clean_project_id = project_id.strip() if isinstance(project_id, str) and project_id.strip() else None

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if clean_project_id:
            proj_row = conn.execute("SELECT id FROM projects WHERE id = ?", (clean_project_id,)).fetchone()
            if not proj_row:
                raise HTTPException(status_code=404, detail=f"Target project '{clean_project_id}' not found")
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=422, detail="ids must not contain duplicates")
        if expected_versions is not None and set(expected_versions) != set(ids):
            raise HTTPException(status_code=422, detail="expected_versions must match every source id")

        now = now_iso()
        sources_to_link = []

        # Strictly verify ALL source ids exist; reject whole transaction if any missing
        for cid in ids:
            row = conn.execute("SELECT * FROM contents WHERE id = ?", (cid,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Content item '{cid}' not found")
            expected = expected_versions.get(cid) if expected_versions is not None else None
            if expected is not None and row["version"] != expected:
                raise HTTPException(
                    status_code=409,
                    detail=f"Version conflict for {cid}: current version is {row['version']}, expected {expected}",
                )
            item = row_to_dict(row)
            sources_to_link.append(item)

        if category is not None and category.strip():
            final_category = category.strip()
            if final_category not in VALID_CONTENT_CATEGORIES:
                raise HTTPException(status_code=422, detail=f"Invalid category '{final_category}'")
        else:
            distinct_cats = {
                str(s.get("category") or "").strip()
                for s in sources_to_link
                if s.get("category") and s.get("category") != "task"
            }
            final_category = next(iter(distinct_cats)) if len(distinct_cats) == 1 else "knowledge"

        c_hash = compute_content_hash(clean_project_id, final_category, clean_title, content)
        can_key = governance_guardrails.canonical_memory_key(clean_project_id, final_category, clean_title, content)

        # Tombstone check (canonical + content hash)
        if _has_content_tombstone(conn, clean_project_id, final_category, clean_title, content):
            raise HTTPException(status_code=410, detail="Merged content was previously deleted and tombstoned")

        merged_pinned = any(
            bool(s.get("pinned")) or (isinstance(s.get("tags"), list) and "pinned" in s["tags"])
            for s in sources_to_link
        )
        merged_tags = ["pinned"] if merged_pinned else []

        new_cid = new_id("cnt")
        conn.execute(
            """
            INSERT INTO contents (
                id, project_id, kind, category, title, content, tags, agent,
                status, version, related_ids, content_hash,
                applicability, origin_authority, canonical_key,
                created_at, updated_at
            ) VALUES (?, ?, 'memory', ?, ?, ?, ?, 'user', 'active', 1, ?, ?, '{}', 'manual', ?, ?, ?)
            """,
            (new_cid, clean_project_id, final_category, clean_title, content, encode(merged_tags), encode(ids), c_hash, can_key, now, now),
        )

        for s in sources_to_link:
            conn.execute(
                "UPDATE contents SET status = 'historical', superseded_by_id = ?, version = COALESCE(version, 1) + 1, updated_at = ? WHERE id = ?",
                (new_cid, now, s["id"]),
            )
            _record_current_version_snapshot(conn, s["id"], "user", now)
            invalidate_handoffs(conn, s["id"])
            log_audit(actor="user", action="content.merged_source", target=s["id"], conn=conn)

        conn.execute(
            """
            INSERT INTO content_versions (
                id, content_id, version, title, content, actor, created_at
            ) VALUES (?, ?, 1, ?, ?, 'merge', ?)
            """,
            (new_id("ver"), new_cid, clean_title, content, now),
        )

        # Keep original conversation/document provenance; merged memories are
        # related content, not documents invented to satisfy a source schema.
        origins = {}
        for source in sources_to_link:
            for origin in _get_content_sources(conn, source["id"]):
                origins[(origin["kind"], origin["id"])] = origin
            if source["kind"] == "document":
                origins[("document", source["id"])] = {
                    "kind": "document", "id": source["id"], "title": source["title"],
                    "agent": source.get("agent"), "deleted": False, "quote": None,
                }
        for origin in origins.values():
            conn.execute(
                """INSERT INTO content_sources
                   (id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id("src"), new_cid, origin["id"], origin["kind"],
                 origin["title"], origin.get("agent"), int(origin["deleted"]), origin.get("quote"), now),
            )

        log_audit(actor="user", action="content.merge", target=new_cid, conn=conn)
    return get_content(new_cid)


def pin_content(content_id: str, pinned: bool = True, expected_version: int | None = None) -> dict[str, Any]:
    """Pin or unpin a memory item as an always-on core rule (Letta Core Memory tier)."""
    with db() as conn:
        row = conn.execute("SELECT version FROM contents WHERE id = ?", (content_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Content not found")
        eff_version = expected_version if expected_version is not None else row["version"]
        return update_content(
            content_id,
            pinned=pinned,
            expected_version=eff_version,
            _connection=conn,
        )


_TITLE_PREFIX_RE = re.compile(
    r"^(?:用户偏好与规范|用户偏好|使用习惯与偏好|使用习惯|技术与架构决策|决定与约束|踩坑经验与教训|踩坑经验|项目背景与上下文|项目背景|任务进展与阶段背景|任务推进节点|会话进展提炼)[:：\s]+",
    re.IGNORECASE,
)


def _normalize_memory_key(title: str) -> str:
    cleaned = _TITLE_PREFIX_RE.sub("", (title or "").strip()).lower()
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", cleaned)
    return cleaned


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    clean = re.sub(r"\s+", "", (text or "").lower())
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i : i + n] for i in range(len(clean) - n + 1)}


def _memory_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    key_a = _normalize_memory_key(a.get("title", ""))
    key_b = _normalize_memory_key(b.get("title", ""))
    if key_a and key_b:
        if key_a == key_b:
            return 1.0
        if min(len(key_a), len(key_b)) >= 6 and (key_a in key_b or key_b in key_a):
            return 0.85
    text_a = f"{_TITLE_PREFIX_RE.sub('', a.get('title', ''))} {a.get('content', '')}"
    text_b = f"{_TITLE_PREFIX_RE.sub('', b.get('title', ''))} {b.get('content', '')}"
    grams_a = _char_ngrams(text_a, 2)
    grams_b = _char_ngrams(text_b, 2)
    if not grams_a or not grams_b:
        return 0.0
    overlap = len(grams_a & grams_b)
    union = len(grams_a | grams_b)
    return overlap / union if union else 0.0


def consolidate_memories(project_id: str | None = None, scope: str = "project") -> dict[str, Any]:
    """Mem0-style memory consolidation: deduplicate & merge similar active memories and archive noisy session fillers."""
    # Exclude category='task' since short-term tasks are managed by `working_tasks`.
    clauses = ["kind = 'memory'", "status = 'active'", "category != 'task'"]
    params: list[Any] = []
    if scope == "global":
        clauses.append("project_id IS NULL")
    elif scope == "project" and project_id:
        clauses.append("project_id = ?")
        params.append(project_id)
    elif project_id:
        clauses.append("(project_id = ? OR project_id IS NULL)")
        params.append(project_id)

    where = " AND ".join(clauses)
    merged_groups = 0
    archived_count = 0
    updated_ids: list[str] = []

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"SELECT * FROM contents WHERE {where} ORDER BY updated_at DESC, id DESC",
            tuple(params),
        ).fetchall()
        items = [_enrich_content_item(row_to_dict(r)) for r in rows]
        now = now_iso()

        # Step 1: Archive low-signal '会话进展提炼：' items if structured memories already exist for the project
        structured_count = sum(1 for it in items if not str(it.get("title") or "").startswith("会话进展提炼："))
        remaining_items: list[dict[str, Any]] = []
        for it in items:
            if (
                structured_count > 0
                and str(it.get("title") or "").startswith("会话进展提炼：")
                and not it.get("pinned")
                and it.get("category") not in ("decision", "task")
            ):
                conn.execute(
                    "UPDATE contents SET status = 'historical', version = COALESCE(version, 1) + 1, updated_at = ? WHERE id = ?",
                    (now, it["id"]),
                )
                _record_current_version_snapshot(conn, it["id"], "consolidate", now)
                invalidate_handoffs(conn, it["id"])
                log_audit(actor="user", action="content.consolidate_archive_filler", target=it["id"], conn=conn)
                archived_count += 1
            else:
                remaining_items.append(it)

        # Step 2: Bucket by (project_id, category) and cluster near-duplicates
        buckets: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
        for it in remaining_items:
            buckets.setdefault((it.get("project_id"), it.get("category") or "knowledge"), []).append(it)

        from app.services import project_service

        for (_pid, _cat), bucket in buckets.items():
            if _cat in ("decision", "task"):
                continue
            consumed: set[str] = set()
            # Sort primary candidates: pinned first, then version DESC, then updated_at DESC
            bucket.sort(
                key=lambda x: (
                    1 if x.get("pinned") else 0,
                    x.get("version", 1),
                    project_service._ts_epoch_ms(x.get("updated_at")),
                    str(x.get("updated_at") or ""),
                ),
                reverse=True,
            )
            for idx, primary in enumerate(bucket):
                if primary["id"] in consumed:
                    continue
                duplicates: list[dict[str, Any]] = []
                for candidate in bucket[idx + 1 :]:
                    if candidate["id"] in consumed:
                        continue
                    # Never merge contradictory claims!
                    is_conflict, _ = governance_guardrails.detect_memory_contradiction(
                        primary,
                        candidate.get("title") or "",
                        candidate.get("content") or "",
                        candidate.get("category") or "knowledge",
                    )
                    if is_conflict:
                        continue
                    if _memory_similarity(primary, candidate) >= 0.68:
                        duplicates.append(candidate)
                        consumed.add(candidate["id"])
                if not duplicates:
                    continue

                lines: list[str] = []
                seen_lines: set[str] = set()
                for member in [primary, *duplicates]:
                    for raw_line in (member.get("content") or "").splitlines():
                        clean_line = raw_line.strip()
                        norm_line = re.sub(r"\s+", " ", clean_line).lower()
                        if clean_line and norm_line not in seen_lines:
                            seen_lines.add(norm_line)
                            lines.append(clean_line)
                merged_body = "\n".join(lines) or primary.get("content", "")
                merged_tags = list(dict.fromkeys([
                    *(primary.get("tags") or []),
                    *(t for d in duplicates for t in (d.get("tags") or [])),
                ]))
                merged_related = list(dict.fromkeys([
                    *(primary.get("related_ids") or []),
                    *(d["id"] for d in duplicates),
                    *(r for d in duplicates for r in (d.get("related_ids") or [])),
                ]))
                new_ver = primary["version"] + 1
                c_hash = compute_content_hash(primary.get("project_id"), primary["category"], primary["title"], merged_body)
                can_key = governance_guardrails.canonical_memory_key(
                    primary.get("project_id"), primary["category"], primary["title"], merged_body
                )
                if _has_content_tombstone(conn, primary.get("project_id"), primary["category"], primary["title"], merged_body):
                    consumed.add(primary["id"])
                    continue
                consumed.add(primary["id"])
                merged_groups += 1
                conn.execute(
                    """UPDATE contents
                       SET content = ?, tags = ?, related_ids = ?, version = ?, content_hash = ?, canonical_key = ?, updated_at = ?
                       WHERE id = ?""",
                    (merged_body, encode(merged_tags), encode(merged_related), new_ver, c_hash, can_key, now, primary["id"]),
                )
                conn.execute(
                    """INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
                       VALUES (?, ?, ?, ?, ?, 'consolidate', ?)""",
                    (new_id("ver"), primary["id"], new_ver, primary["title"], merged_body, now),
                )
                invalidate_handoffs(conn, primary["id"])
                existing_source_keys = {
                    (str(sr["source_kind"] or ""), str(sr["source_id"] or ""))
                    for sr in conn.execute(
                        "SELECT source_kind, source_id FROM content_sources WHERE content_id = ?",
                        (primary["id"],),
                    ).fetchall()
                }
                for dup in duplicates:
                    dup_sources = conn.execute(
                        """
                        SELECT source_id, source_kind, source_title, source_agent, deleted, quote, created_at
                        FROM content_sources
                        WHERE content_id = ?
                        ORDER BY created_at ASC, id ASC
                        """,
                        (dup["id"],),
                    ).fetchall()
                    for ds in dup_sources:
                        skey = (str(ds["source_kind"] or ""), str(ds["source_id"] or ""))
                        if skey in existing_source_keys:
                            if ds["quote"]:
                                conn.execute(
                                    """
                                    UPDATE content_sources
                                    SET quote = COALESCE(NULLIF(quote, ''), ?)
                                    WHERE content_id = ? AND source_kind = ? AND source_id = ?
                                    """,
                                    (ds["quote"], primary["id"], ds["source_kind"], ds["source_id"]),
                                )
                            continue
                        existing_source_keys.add(skey)
                        conn.execute(
                            """
                            INSERT INTO content_sources
                                (id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                new_id("src"),
                                primary["id"],
                                ds["source_id"],
                                ds["source_kind"],
                                ds["source_title"],
                                ds["source_agent"],
                                ds["deleted"],
                                ds["quote"],
                                ds["created_at"] or now,
                            ),
                        )
                    conn.execute(
                        "UPDATE contents SET status = 'historical', superseded_by_id = ?, version = COALESCE(version, 1) + 1, updated_at = ? WHERE id = ?",
                        (primary["id"], now, dup["id"]),
                    )
                    _record_current_version_snapshot(conn, dup["id"], "consolidate", now)
                    invalidate_handoffs(conn, dup["id"])
                    log_audit(actor="user", action="content.consolidate_merge", target=f"{dup['id']}->{primary['id']}", conn=conn)
                    archived_count += 1
                updated_ids.append(primary["id"])

    return {
        "ok": True,
        "merged_groups": merged_groups,
        "archived_count": archived_count,
        "updated_ids": updated_ids,
    }


def export_memory_handbook(project_id: str | None = None, include_global: bool = True) -> dict[str, Any]:
    """Render an AGENTS.md / CLAUDE.md compatible Markdown rulebook from active project & global memories."""
    with db() as conn:
        project_name = "全局通用记忆库"
        if project_id:
            row = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()
            if row:
                project_name = row["name"]

        clauses = ["kind = 'memory'", "status = 'active'", "category != 'task'"]
        params: list[Any] = []
        if project_id and include_global:
            clauses.append("(project_id = ? OR project_id IS NULL)")
            params.append(project_id)
        elif project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        else:
            clauses.append("project_id IS NULL")

        rows = conn.execute(
            f"SELECT * FROM contents WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id DESC",
            tuple(params),
        ).fetchall()
        items = [_enrich_content_item(row_to_dict(r)) for r in rows]

    pinned_items = [it for it in items if it.get("pinned")]
    unpinned = [it for it in items if not it.get("pinned")]

    sections_spec = [
        ("preference", "## 🌐 使用习惯与编码偏好 (Preferences & Coding Style)"),
        ("decision", "## ⚖️ 架构决定与技术约束 (Architecture Decisions & Constraints)"),
        ("lesson", "## 🛠️ 踩坑经验与避坑纪律 (Lessons & Pitfalls)"),
        ("background", "## 🧭 项目背景与上下文 (Project Background)"),
        ("knowledge", "## 📚 领域知识与约定 (Domain Knowledge)"),
    ]

    md_lines = [
        f"# AGENTS.md / CLAUDE.md — {project_name}",
        "",
        "> 由 **Senrelay 续境 (ContextCore)** 自动生成的结构化认知记忆手册。可直接保存至项目根目录 `CLAUDE.md` 或 `AGENTS.md`。",
        "",
    ]

    if pinned_items:
        md_lines.append("## 📌 常驻核心规则 (Pinned Core Rules — 每次必读)")
        md_lines.append("")
        for it in pinned_items:
            scope_tag = " `[全局]`" if it.get("scope") == "global" else ""
            md_lines.append(f"- **{it['title']}**{scope_tag}：{it['content']}")
        md_lines.append("")

    for cat_key, heading in sections_spec:
        cat_items = [it for it in unpinned if it.get("category") == cat_key]
        if not cat_items:
            continue
        md_lines.append(heading)
        md_lines.append("")
        for it in cat_items:
            scope_tag = " `[全局]`" if it.get("scope") == "global" else ""
            md_lines.append(f"- **{it['title']}**{scope_tag}：{it['content']}")
        md_lines.append("")

    if len(items) == 0:
        md_lines.append("_当前暂无有效记忆条目。可通过对话提炼或手动新增常驻规则。_")
        md_lines.append("")

    return {
        "project_id": project_id,
        "project_name": project_name,
        "markdown": "\n".join(md_lines).strip() + "\n",
        "counts": {
            "total": len(items),
            "pinned": len(pinned_items),
            "global": sum(1 for it in items if it.get("scope") == "global"),
            "project": sum(1 for it in items if it.get("scope") == "project"),
        },
    }
