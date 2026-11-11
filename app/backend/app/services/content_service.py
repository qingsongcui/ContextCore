import hashlib
import re
from contextlib import nullcontext
from typing import Any
from fastapi import HTTPException
from app.services.handoff_service import invalidate_handoffs
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode
from app.date_filters import upper_date_bound


def _enrich_content_item(item: dict[str, Any]) -> dict[str, Any]:
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    item["pinned"] = "pinned" in tags
    item["scope"] = "global" if not item.get("project_id") else "project"
    if "source_count" not in item or item["source_count"] is None:
        sources = item.get("sources")
        if isinstance(sources, list):
            item["source_count"] = sum(1 for s in sources if isinstance(s, dict) and not s.get("deleted"))
        else:
            item["source_count"] = 0
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


def _has_content_tombstone(conn, project_id: str | None, category: str, title: str, content: str) -> bool:
    raw_hash = compute_content_hash(project_id, category, title, content)
    norm_hash = compute_normalized_content_hash(project_id, category, title, content)
    row = conn.execute(
        "SELECT 1 FROM tombstones WHERE entity_type IN ('content', 'memory', 'document') AND dedupe_key IN (?, ?)",
        (raw_hash, norm_hash),
    ).fetchone()
    return row is not None


def escape_like(value: str) -> str:
    """Escape %, _, and backslash for SQLite LIKE queries using backslash as escape char."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")




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
        })
    return result


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
    clauses = []
    params = []

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
    if agent:
        clauses.append("c.agent = ?")
        params.append(agent)
    if status:
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
        "c.id,c.project_id,c.kind,c.category,c.title,c.tags,c.agent,c.status,c.version,c.related_ids,c.created_at,c.updated_at"
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
        result = []
        for r in rows:
            item = row_to_dict(r)
            if summary:
                item["content"] = ""
                item["sources"] = []
            else:
                item["sources"] = _get_content_sources(conn, item["id"])
            result.append(_enrich_content_item(item))
        return result


def content_index(
    project_id: str | None = None,
    kind: str | None = None,
    category: str | None = None,
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
    if agent:
        clauses.append("c.agent = ?")
        params.append(agent)
    if status:
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
                      c.version,c.related_ids,c.created_at,c.updated_at,
                      substr(c.content,1,240) AS content_preview
               FROM contents c """ + where + f" {order_clause} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        items = [_enrich_content_item(row_to_dict(row)) for row in cursor]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _read_content(conn, content_id: str, *, include_versions: bool = True) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM contents WHERE id = ?", (content_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Content not found")
    item = row_to_dict(row)
    item["sources"] = _get_content_sources(conn, item["id"])
    item["versions"] = _get_content_versions(conn, item["id"]) if include_versions else None
    return _enrich_content_item(item)


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

    c_hash = compute_content_hash(project_id, category, clean_title, content)
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
                return get_content(existing['id'])

        # Handle superseding
        if supersedes_id:
            old = conn.execute("SELECT * FROM contents WHERE id = ?", (supersedes_id,)).fetchone()
            if not old:
                raise HTTPException(status_code=404, detail=f"Target content for supersession '{supersedes_id}' not found")
            if old["status"] != "active":
                raise HTTPException(status_code=409, detail=f"Target content for supersession '{supersedes_id}' is not active (status: {old['status']})")
            if old["project_id"] != project_id:
                raise HTTPException(status_code=400, detail="Cannot supersede content from another project domain")
            conn.execute(
                "UPDATE contents SET status = 'historical', updated_at = ? WHERE id = ?",
                (now, supersedes_id),
            )
            log_audit(actor=agent or "user", action="content.supersede", target=supersedes_id, conn=conn)
        conn.execute(
            """
            INSERT INTO contents (
                id, project_id, kind, category, title, content, tags, agent,
                status, version, related_ids, content_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?)
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
                encode(related_ids or []),
                c_hash,
                now,
                now,
            ),
        )

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
            # Look up source title
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
                (new_id("src"), cid, source_id, source_kind, source_title, agent, quote, now),
            )

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
    expected_version: int,
    _connection=None,
) -> dict[str, Any]:
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
        if scope == "global":
            new_project_id = None
        elif project_id is not None:
            proj_row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj_row:
                raise HTTPException(status_code=404, detail=f"Target project '{project_id}' not found")
            new_project_id = project_id
        else:
            new_project_id = current.get("project_id")

        c_hash = compute_content_hash(new_project_id, new_category, new_title, new_content)

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

        if project_id is not None and project_id != current.get("project_id"):
            log_audit(actor=agent or "user", action="content.reassign", target=f"{content_id}->{project_id}", conn=conn)
        else:
            log_audit(actor=agent or "user", action="content.update", target=content_id, conn=conn)
        if _connection is not None:
            return _read_content(conn, content_id)
    return get_content(content_id)


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

        c_hash = compute_content_hash(current.get("project_id"), current.get("category", "knowledge"), restored_title, restored_content)
        if _has_content_tombstone(conn, current.get("project_id"), current.get("category", "knowledge"), restored_title, restored_content):
            raise HTTPException(status_code=410, detail="Restored content snapshot was previously deleted and tombstoned")

        cursor = conn.execute(
            """
            UPDATE contents SET
                title = ?,
                content = ?,
                version = ?,
                content_hash = ?,
                updated_at = ?
            WHERE id = ? AND version = ?
            """,
            (restored_title, restored_content, new_version, c_hash, now, content_id, expected_version),
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
                if _has_content_tombstone(conn, row["project_id"], category, row["title"], row["content"]):
                    raise HTTPException(status_code=410, detail="Category change would restore permanently deleted content")
                if expected_versions is not None:
                    updated = conn.execute(
                        "UPDATE contents SET category = ?, version = ?, content_hash = ?, updated_at = ? WHERE id = ? AND version = ?",
                        (category, new_ver, c_hash, now, cid, row["version"]),
                    )
                    if updated.rowcount != 1:
                        raise HTTPException(status_code=409, detail=f"Version conflict for {cid}")
                else:
                    conn.execute(
                        "UPDATE contents SET category = ?, version = ?, content_hash = ?, updated_at = ? WHERE id = ?",
                        (category, new_ver, c_hash, now, cid),
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
    ids: list[str], title: str, content: str, project_id: str,
    expected_versions: dict[str, int] | None = None,
) -> dict[str, Any]:
    if len(ids) < 2:
        raise HTTPException(status_code=422, detail="At least 2 content items are required to merge")
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=422, detail="Merged title cannot be empty")

    c_hash = compute_content_hash(project_id, "knowledge", clean_title, content)

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if project_id:
            proj_row = conn.execute("SELECT id FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not proj_row:
                raise HTTPException(status_code=404, detail=f"Target project '{project_id}' not found")
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=422, detail="ids must not contain duplicates")
        if expected_versions is not None and set(expected_versions) != set(ids):
            raise HTTPException(status_code=422, detail="expected_versions must match every source id")
        # Tombstone check
        tb = conn.execute("SELECT id FROM tombstones WHERE entity_type = 'content' AND dedupe_key = ?", (c_hash,)).fetchone()
        if tb:
            raise HTTPException(status_code=410, detail="Merged content was previously deleted and tombstoned")

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

        for s in sources_to_link:
            conn.execute(
                "UPDATE contents SET status = 'historical', updated_at = ? WHERE id = ?",
                (now, s["id"]),
            )
            log_audit(actor="user", action="content.merged_source", target=s["id"], conn=conn)

        new_cid = new_id("cnt")
        conn.execute(
            """
            INSERT INTO contents (
                id, project_id, kind, category, title, content, tags, agent,
                status, version, related_ids, content_hash, created_at, updated_at
            ) VALUES (?, ?, 'memory', 'knowledge', ?, ?, '[]', 'user', 'active', 1, ?, ?, ?, ?)
            """,
            (new_cid, project_id, clean_title, content, encode(ids), c_hash, now, now),
        )

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
                    "agent": source.get("agent"), "deleted": False,
                }
        for origin in origins.values():
            conn.execute(
                """INSERT INTO content_sources
                   (id, content_id, source_id, source_kind, source_title, source_agent, deleted, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (new_id("src"), new_cid, origin["id"], origin["kind"],
                 origin["title"], origin.get("agent"), int(origin["deleted"]), now),
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
    clauses = ["kind = 'memory'", "status = 'active'"]
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
                    "UPDATE contents SET status = 'historical', updated_at = ? WHERE id = ?",
                    (now, it["id"]),
                )
                invalidate_handoffs(conn, it["id"])
                log_audit(actor="user", action="content.consolidate_archive_filler", target=it["id"], conn=conn)
                archived_count += 1
            else:
                remaining_items.append(it)

        # Step 2: Bucket by (project_id, category) and cluster near-duplicates
        buckets: dict[tuple[str | None, str], list[dict[str, Any]]] = {}
        for it in remaining_items:
            buckets.setdefault((it.get("project_id"), it.get("category") or "knowledge"), []).append(it)

        for (_pid, _cat), bucket in buckets.items():
            if _cat in ("decision", "task"):
                continue
            consumed: set[str] = set()
            # Sort primary candidates: pinned first, then version DESC, then updated_at DESC
            bucket.sort(key=lambda x: (1 if x.get("pinned") else 0, x.get("version", 1), x.get("updated_at", "")), reverse=True)
            for idx, primary in enumerate(bucket):
                if primary["id"] in consumed:
                    continue
                duplicates: list[dict[str, Any]] = []
                for candidate in bucket[idx + 1 :]:
                    if candidate["id"] in consumed:
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
                if _has_content_tombstone(conn, primary.get("project_id"), primary["category"], primary["title"], merged_body):
                    consumed.add(primary["id"])
                    continue
                consumed.add(primary["id"])
                merged_groups += 1
                conn.execute(
                    """UPDATE contents
                       SET content = ?, tags = ?, related_ids = ?, version = ?, content_hash = ?, updated_at = ?
                       WHERE id = ?""",
                    (merged_body, encode(merged_tags), encode(merged_related), new_ver, c_hash, now, primary["id"]),
                )
                conn.execute(
                    """INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
                       VALUES (?, ?, ?, ?, ?, 'consolidate', ?)""",
                    (new_id("ver"), primary["id"], new_ver, primary["title"], merged_body, now),
                )
                invalidate_handoffs(conn, primary["id"])
                for dup in duplicates:
                    conn.execute(
                        "UPDATE contents SET status = 'historical', updated_at = ? WHERE id = ?",
                        (now, dup["id"]),
                    )
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

        clauses = ["kind = 'memory'", "status = 'active'"]
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
        ("task", "## 📋 当前任务与推进节点 (Active Tasks & Checkpoints)"),
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
