from fastapi import HTTPException

from app.database import db, decode, encode, log_audit, now_iso, row_to_dict
from app.services.content_service import _get_content_sources_batch
from app.services.memory_rules import is_asserted, reading_title, rule_topic


def reading_page(project_id, offset, limit):
    with db() as conn:
        conn.execute("BEGIN")
        conn.create_function("is_asserted", 1, lambda tags: int(is_asserted(decode(tags, []))), deterministic=True)
        args = ()
        where = "project_id IS NULL AND (category='preference' OR EXISTS (SELECT 1 FROM json_each(tags) WHERE value='personal'))"
        if project_id:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise HTTPException(404, "Project not found")
            where = "(project_id=? OR project_id IS NULL)"
            args = (project_id,)
        if project_id:
            # Project conventions can shadow defaults on a different page.
            topics = {rule_topic(r[0]) for r in conn.execute(
                """SELECT content FROM contents WHERE project_id=? AND kind='memory' AND status='active'
                   AND is_asserted(tags) AND category IN ('decision','preference') AND coalesce(applicability,'{}') IN ('{}','')""", args)}
            topics.discard(None)
            if topics:
                conn.create_function("is_shadowed_default", 1, lambda text: int(rule_topic(text) in topics), deterministic=True)
                where += " AND (project_id IS NOT NULL OR category NOT IN ('decision','preference') OR NOT is_shadowed_default(content))"
        predicate = "is_asserted(tags) AND kind='memory' AND status='active' AND category!='task' AND " + where
        total = conn.execute("SELECT count(*) FROM contents WHERE " + predicate, args).fetchone()[0]
        rows = conn.execute(
            f"""SELECT id,project_id,category,title,substr(content,1,500) AS content,tags,version,updated_at
                FROM contents WHERE {predicate}
                ORDER BY project_id IS NULL, updated_at DESC,id DESC LIMIT ? OFFSET ?""", (*args, limit, offset),
        )
        page = [row_to_dict(r) for r in rows]
        ids = [r["id"] for r in page]
        sources = _get_content_sources_batch(conn, ids)
        history = {}
        if ids:
            for row in conn.execute(
                f"""SELECT id,superseded_by_id,title,substr(content,1,500) AS content,tags FROM contents
                    WHERE superseded_by_id IN ({','.join('?' for _ in ids)}) ORDER BY updated_at DESC,id""", ids,
            ):
                history.setdefault(row["superseded_by_id"], []).append({
                    "id": row["id"], "title": reading_title(row["title"], row["content"], decode(row["tags"], [])),
                })
        for row in page:
            tags = row.pop("tags") or []
            row["heading"] = reading_title(row["title"], row["content"], tags)
            row["section"] = row["category"]
            if project_id and not row["project_id"]:
                row["section"] = "personal" if row["category"] == "preference" or "personal" in tags else "shared"
            row["sources"] = sources.get(row["id"], [])
            row["previous"] = history.get(row["id"], [])
        return {"items": page, "total": total, "offset": offset}


def read_profile(conn):
    row = conn.execute("SELECT revision,payload FROM personal_workspace WHERE id=1").fetchone()
    if not row:
        return {"revision": 0, "name": "", "bio": "", "assets": []}
    return {"revision": row["revision"], **decode(row["payload"], {})}


def get_profile():
    with db() as conn:
        result = read_profile(conn)
        for asset in result["assets"]:
            for ref in asset.get("evidence", []):
                row = conn.execute("SELECT title,version FROM contents WHERE id=?", (ref["id"],)).fetchone()
                ref["title"] = row["title"] if row else None
                ref["current_version"] = row["version"] if row else None
            if asset["source_id"]:
                row = conn.execute("SELECT title,version FROM contents WHERE id=?", (asset["source_id"],)).fetchone()
                asset["source_title"] = row["title"] if row else None
                asset["source_current_version"] = row["version"] if row else None
        return result


def save_profile(req):
    data = req.model_dump(exclude={"revision"})
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = read_profile(conn)
        if prior["revision"] != req.revision:
            raise HTTPException(409, "Profile version changed; reload before saving")
        ids = {a["id"] for a in data["assets"]}
        if len(ids) != len(data["assets"]):
            raise HTTPException(422, "Duplicate asset ID")
        old = {a["id"]: a for a in prior["assets"]}
        for asset in data["assets"]:
            old_versions = {(r["id"], r["version"]) for r in old.get(asset["id"], {}).get("evidence", [])}
            for ref in asset["evidence"]:
                if (ref["id"], ref["version"]) in old_versions:
                    continue
                row = conn.execute("SELECT version FROM contents WHERE id=?", (ref["id"],)).fetchone()
                if not row or row["version"] != ref["version"]:
                    raise HTTPException(409, "Selected evidence changed; select its current version")
            if asset["source_id"] and (asset["source_id"], asset["source_version"]) != (
                old.get(asset["id"], {}).get("source_id"), old.get(asset["id"], {}).get("source_version")
            ):
                row = conn.execute("SELECT version FROM contents WHERE id=?", (asset["source_id"],)).fetchone()
                if not row or row["version"] != asset["source_version"]:
                    raise HTTPException(409, "Selected evidence changed; select its current version")
        revision = req.revision + 1
        conn.execute("""INSERT INTO personal_workspace(id,revision,payload,updated_at) VALUES(1,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,
                        payload=excluded.payload,updated_at=excluded.updated_at""", (revision, encode(data), now_iso()))
        log_audit(actor="user", action="profile.save", target="local-profile", conn=conn)
        return {"revision": revision, **data}


def profile_snapshot():
    with db() as conn:
        saved = read_profile(conn)
        # Positive selection: private notes, sources, project IDs and recipes are never serialized.
        return {"revision": saved["revision"], "name": saved["name"], "bio": saved["bio"],
                "assets": [{"title": a["title"], "excerpt": a["excerpt"]} for a in saved["assets"] if a["selected"]]}


def capability_candidates(project_ids, offset):
    """Group comparable work from user-selected projects without attributing it to a person."""
    project_ids = sorted(set(project_ids))
    if not project_ids:
        return {"items": [], "more": False}
    with db() as conn:
        marks = ",".join("?" for _ in project_ids)
        conn.create_function("work_topic", 3,
            lambda title, body, tags: reading_title(title, body, decode(tags, [])).strip().casefold()[:160],
            deterministic=True)
        rows = conn.execute(f"""
            WITH evidence AS (
                SELECT id, project_id, title, version, updated_at, work_topic(title, substr(content,1,240), tags) AS topic
                FROM contents WHERE project_id IN ({marks}) AND status='active' AND (kind='document' OR category='lesson')
            ), topics AS (
                SELECT topic, count(DISTINCT project_id) AS projects, count(*) AS samples, max(updated_at) AS last_seen
                FROM evidence WHERE topic<>'' GROUP BY topic ORDER BY projects DESC,samples DESC,last_seen DESC,topic
                LIMIT 13 OFFSET ?
            ), ranked AS (
                SELECT e.*, t.projects,t.samples,t.last_seen,
                       row_number() OVER (PARTITION BY e.topic ORDER BY e.updated_at DESC,e.id) AS seq
                FROM evidence e JOIN topics t ON t.topic=e.topic
            ) SELECT * FROM ranked WHERE seq<=8 ORDER BY projects DESC,samples DESC,last_seen DESC,topic,seq
        """, (*project_ids, offset))
        groups = {}
        for row in rows:
            group = groups.setdefault(row["topic"], {"topic": row["topic"], "projects": row["projects"],
                "samples": row["samples"], "evidence": []})
            group["evidence"].append({k: row[k] for k in ("id", "project_id", "title", "version")})
        items = list(groups.values())
        return {"items": items[:12], "more": len(items) > 12}
