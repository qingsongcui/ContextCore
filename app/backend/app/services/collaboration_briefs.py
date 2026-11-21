"""Local collaboration drafts refer to the saved card; never cache a withdrawn offer."""
from fastapi import HTTPException

from app.database import db, log_audit, now_iso
from app.services.workbench_service import read_profile


def _row(conn, brief_id):
    row = conn.execute("SELECT * FROM collaboration_briefs WHERE id=?", (brief_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Brief not found")
    return row


def _selection(profile, asset_index, profile_revision):
    if asset_index is None:
        return None
    assets = [asset for asset in profile["assets"] if asset["selected"]]
    if profile["revision"] != profile_revision or asset_index >= len(assets):
        raise HTTPException(409, "Card selection changed")
    return assets[asset_index]


def _draft(row, profile):
    selection = None
    if row["profile_revision"] == profile["revision"]:
        selected = (asset for asset in profile["assets"] if asset["selected"])
        for index, asset in enumerate(selected):
            if asset["id"] == row["asset_id"]:
                selection = {"revision": profile["revision"], "index": index,
                             "title": asset["title"], "excerpt": asset["excerpt"]}
                break
    return {"id": row["id"], "revision": row["revision"], "selection": selection,
            "selection_stale": bool(row["asset_id"] and not selection),
            "updated_at": row["updated_at"], "deliverable": row["deliverable"], "terms": row["terms"]}


def index(offset, limit):
    with db() as conn:
        conn.execute("BEGIN")
        total = conn.execute("SELECT COUNT(*) FROM collaboration_briefs").fetchone()[0]
        # Index previews include author's text only. A card edit cannot leave a stale offer title here.
        rows = conn.execute(
            """SELECT id,revision,substr(deliverable,1,120) AS deliverable,updated_at
               FROM collaboration_briefs ORDER BY updated_at DESC,id LIMIT ? OFFSET ?""",
            (limit, offset))
        return {"items": [dict(row) for row in rows], "total": total, "offset": offset}


def read(brief_id):
    with db() as conn:
        conn.execute("BEGIN")
        return _draft(_row(conn, brief_id), read_profile(conn))


def save(brief_id, form):
    if not (form.deliverable.strip() or form.terms.strip() or form.asset_index is not None):
        raise HTTPException(422, "Empty collaboration brief")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT revision FROM collaboration_briefs WHERE id=?", (brief_id,)).fetchone()
        if not prior and form.revision:
            raise HTTPException(404, "Brief not found")
        if prior and form.revision != prior["revision"]:
            raise HTTPException(409, "Brief version changed")
        card = read_profile(conn)
        asset = _selection(card, form.asset_index, form.profile_revision)
        conn.execute(
            """INSERT INTO collaboration_briefs(id,revision,asset_id,profile_revision,deliverable,terms,updated_at)
               VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,
               asset_id=excluded.asset_id, profile_revision=excluded.profile_revision, deliverable=excluded.deliverable,
               terms=excluded.terms, updated_at=excluded.updated_at""",
            (brief_id, form.revision + 1, asset["id"] if asset else None,
             card["revision"] if asset else None, form.deliverable, form.terms, now_iso()))
        log_audit(actor="user", action="brief.save", target=brief_id, conn=conn)
        return _draft(_row(conn, brief_id), card)


def delete(brief_id, revision):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if _row(conn, brief_id)["revision"] != revision:
            raise HTTPException(409, "Brief version changed")
        conn.execute("DELETE FROM collaboration_briefs WHERE id=?", (brief_id,))
        log_audit(actor="user", action="brief.delete", target=brief_id, conn=conn)


def export(form):
    """Export exactly the selection the author previewed, against a single current snapshot."""
    if not form.deliverable.strip() or form.asset_index is None:
        raise HTTPException(422, "Select an offer and enter a deliverable before exporting")
    with db() as conn:
        conn.execute("BEGIN")
        if form.id and _row(conn, form.id)["revision"] != form.revision:
            raise HTTPException(409, "Brief version changed")
        card = read_profile(conn)
        asset = _selection(card, form.asset_index, form.profile_revision)
        return {"profile_revision": card["revision"], "provider": card["name"], "title": asset["title"],
                "excerpt": asset["excerpt"], "deliverable": form.deliverable.strip(), "terms": form.terms.strip()}
