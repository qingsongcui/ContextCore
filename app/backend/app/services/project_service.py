import os
from typing import Any
from fastapi import HTTPException
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode


def _get_project_counts(conn, project_id: str) -> dict[str, int]:
    conv_count = conn.execute(
        "SELECT COUNT(*) as c FROM conversations WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    mem_count = conn.execute(
        "SELECT COUNT(*) as c FROM contents WHERE project_id = ? AND kind = 'memory' AND status = 'active'",
        (project_id,),
    ).fetchone()["c"]
    doc_count = conn.execute(
        "SELECT COUNT(*) as c FROM contents WHERE project_id = ? AND kind = 'document' AND status = 'active'",
        (project_id,),
    ).fetchone()["c"]
    task_count = conn.execute(
        "SELECT COUNT(*) as c FROM contents WHERE project_id = ? AND category = 'task' AND status = 'active'",
        (project_id,),
    ).fetchone()["c"]
    return {
        "conversations": conv_count,
        "memories": mem_count,
        "documents": doc_count,
        "tasks": task_count,
    }


def _get_project_directories(conn, project_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT path FROM project_directories WHERE project_id = ? ORDER BY created_at ASC",
        (project_id,),
    ).fetchall()
    return [r["path"] for r in rows]


def list_projects(status: str | None = None) -> list[dict[str, Any]]:
    with db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        result = []
        for r in rows:
            p = row_to_dict(r)
            p["directories"] = _get_project_directories(conn, p["id"])
            p["counts"] = _get_project_counts(conn, p["id"])
            result.append(p)
        return result


def get_project(project_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        p = row_to_dict(row)
        p["directories"] = _get_project_directories(conn, p["id"])
        p["counts"] = _get_project_counts(conn, p["id"])
        return p


def create_project(name: str, description: str = "", directories: list[str] | None = None) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="Project name cannot be empty")
    pid = new_id("proj")
    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO projects (id, name, description, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (pid, clean_name, description.strip(), now, now),
        )
        if directories:
            for d in directories:
                norm_d = os.path.abspath(d.strip())
                conn.execute(
                    "INSERT OR IGNORE INTO project_directories (id, project_id, path, created_at) VALUES (?, ?, ?, ?)",
                    (new_id("pdir"), pid, norm_d, now),
                )
        log_audit(actor="user", action="project.create", target=pid, conn=conn)
    return get_project(pid)


def update_project(
    project_id: str,
    name: str | None = None,
    description: str | None = None,
    directories: list[str] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")

        updates = []
        params = []
        now = now_iso()

        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise HTTPException(status_code=422, detail="Project name cannot be empty")
            updates.append("name = ?")
            params.append(clean_name)

        if description is not None:
            updates.append("description = ?")
            params.append(description.strip())

        if status is not None:
            if status not in ("active", "archived"):
                raise HTTPException(status_code=422, detail="Invalid status")
            updates.append("status = ?")
            params.append(status)
            if status == "archived":
                updates.append("archived_at = ?")
                params.append(now)
            else:
                updates.append("archived_at = NULL")

        updates.append("updated_at = ?")
        params.append(now)
        params.append(project_id)

        conn.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id = ?", tuple(params))

        if directories is not None:
            conn.execute("DELETE FROM project_directories WHERE project_id = ?", (project_id,))
            for d in directories:
                norm_d = os.path.abspath(d.strip())
                conn.execute(
                    "INSERT OR IGNORE INTO project_directories (id, project_id, path, created_at) VALUES (?, ?, ?, ?)",
                    (new_id("pdir"), project_id, norm_d, now),
                )

        log_audit(actor="user", action="project.update", target=project_id, conn=conn)
    return get_project(project_id)


def _move_content(conn, content_id: str, project_id: str, now: str) -> None:
    """Move one body at a time and invalidate editors holding its old revision."""
    from app.services.content_service import compute_content_hash

    item = conn.execute(
        "SELECT category,title,content FROM contents WHERE id=?", (content_id,),
    ).fetchone()
    digest = compute_content_hash(project_id, item["category"], item["title"], item["content"])
    if conn.execute(
        "SELECT 1 FROM tombstones WHERE entity_type='content' AND dedupe_key=?", (digest,),
    ).fetchone():
        raise HTTPException(410, "Project move would restore permanently deleted content")
    conn.execute(
        "UPDATE contents SET project_id=?,content_hash=?,version=version+1,updated_at=? WHERE id=?",
        (project_id, digest, now, content_id),
    )
    conn.execute(
        """INSERT INTO content_versions(id,content_id,version,title,content,actor,created_at)
           SELECT ?,id,version,title,content,'user',? FROM contents WHERE id=?""",
        (new_id("ver"), now, content_id),
    )
    log_audit(actor="user", action="content.reassign", target=f"{content_id}->{project_id}", conn=conn)


def merge_projects(source_ids: list[str], target_id: str) -> dict[str, Any]:
    if not source_ids:
        raise HTTPException(status_code=422, detail="source_ids cannot be empty")

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute("SELECT * FROM projects WHERE id = ?", (target_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="Target project not found")

        # Strictly verify all source projects exist
        for sid in source_ids:
            src = conn.execute("SELECT * FROM projects WHERE id = ?", (sid,)).fetchone()
            if not src:
                raise HTTPException(status_code=404, detail=f"Source project '{sid}' not found")

        now = now_iso()
        for sid in source_ids:
            if sid == target_id:
                continue

            # Keep resident bodies bounded while moving a large project. Keyset
            # pages remain stable as rows leave the source project's index.
            after = ""
            while True:
                ids = [row["id"] for row in conn.execute(
                    "SELECT id FROM contents WHERE project_id=? AND id>? ORDER BY id LIMIT 64",
                    (sid, after),
                )]
                if not ids:
                    break
                for content_id in ids:
                    _move_content(conn, content_id, target_id, now)
                after = ids[-1]

            # Move conversations, conflicts, handoffs
            conn.execute("UPDATE conversations SET project_id = ?, updated_at = ? WHERE project_id = ?", (target_id, now, sid))
            conn.execute("UPDATE conflicts SET project_id = ? WHERE project_id = ?", (target_id, sid))
            conn.execute("UPDATE handoffs SET project_id = ? WHERE project_id = ?", (target_id, sid))

            # Move directories and clean up from source
            src_dirs = conn.execute("SELECT path FROM project_directories WHERE project_id = ?", (sid,)).fetchall()
            for sd in src_dirs:
                conn.execute(
                    "INSERT OR IGNORE INTO project_directories (id, project_id, path, created_at) VALUES (?, ?, ?, ?)",
                    (new_id("pdir"), target_id, sd["path"], now),
                )
            conn.execute("DELETE FROM project_directories WHERE project_id = ?", (sid,))

            # Archive source project
            conn.execute(
                "UPDATE projects SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
                (now, now, sid),
            )
            log_audit(actor="user", action="project.merge_source", target=sid, conn=conn)

        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, target_id))
        log_audit(actor="user", action="project.merge", target=target_id, conn=conn)
    return get_project(target_id)


def split_project(
    project_id: str,
    name: str,
    description: str = "",
    conversation_ids: list[str] | None = None,
    content_ids: list[str] | None = None,
    directories: list[str] | None = None,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="New project name cannot be empty")

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        src = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not src:
            raise HTTPException(status_code=404, detail="Source project not found")

        # Strictly verify all requested conversation_ids belong to source project
        if conversation_ids:
            for cid in conversation_ids:
                conv_row = conn.execute("SELECT id FROM conversations WHERE id = ? AND project_id = ?", (cid, project_id)).fetchone()
                if not conv_row:
                    raise HTTPException(status_code=404, detail=f"Conversation '{cid}' not found on source project")

        # Strictly verify all requested content_ids belong to source project
        if content_ids:
            for cnt_id in content_ids:
                cnt_row = conn.execute("SELECT id FROM contents WHERE id = ? AND project_id = ?", (cnt_id, project_id)).fetchone()
                if not cnt_row:
                    raise HTTPException(status_code=404, detail=f"Content '{cnt_id}' not found on source project")

        new_pid = new_id("proj")
        now = now_iso()
        conn.execute(
            """
            INSERT INTO projects (id, name, description, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (new_pid, clean_name, description.strip(), now, now),
        )

        # Move directories: remove from source project and assign to new project
        if directories:
            for d in directories:
                norm_d = os.path.abspath(d.strip())
                conn.execute("DELETE FROM project_directories WHERE project_id = ? AND path = ?", (project_id, norm_d))
                conn.execute(
                    "INSERT OR IGNORE INTO project_directories (id, project_id, path, created_at) VALUES (?, ?, ?, ?)",
                    (new_id("pdir"), new_pid, norm_d, now),
                )

        if conversation_ids:
            for cid in conversation_ids:
                conn.execute(
                    "UPDATE conversations SET project_id = ?, updated_at = ? WHERE id = ?",
                    (new_pid, now, cid),
                )

        if content_ids:
            for cnt_id in dict.fromkeys(content_ids):
                _move_content(conn, cnt_id, new_pid, now)

        conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
        log_audit(actor="user", action="project.split", target=new_pid, conn=conn)
    return get_project(new_pid)


def canonical_project_path(path: str) -> str:
    """Compare filesystem identity without rewriting stored paths or symlinks."""
    return os.path.realpath(os.path.abspath(path.strip()))


def repository_identity(path: str) -> dict[str, str] | None:
    """Inspect Git metadata only; do not invoke hooks, shells or change worktrees."""
    from pathlib import Path
    target = Path(path).expanduser()
    if not target.is_dir():
        return None
    for root in (target, *target.parents):
        marker = root / ".git"
        if marker.is_dir():
            return {"root": str(root), "common_root": str(root), "surface": "repository"}
        if marker.is_file():
            try:
                line = marker.read_text(encoding="utf-8")[:4096].strip()
                if not line.startswith("gitdir:"):
                    return None
                gitdir = Path(line.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = root / gitdir
                common_file = gitdir / "commondir"
                common = (gitdir / common_file.read_text().strip()).resolve() if common_file.is_file() else gitdir.resolve()
                common_root = common.parent if common.name == ".git" else root
                return {"root": str(root), "common_root": str(common_root), "surface": "worktree"}
            except (OSError, UnicodeError):
                return None
    return None


def resolve_project_by_path(path: str) -> tuple[dict[str, Any] | None, bool]:
    if not path:
        return None, False
    norm_target = canonical_project_path(path)
    with db() as conn:
        rows = conn.execute(
            """
            SELECT p.*, pd.path as dir_path
            FROM project_directories pd
            JOIN projects p ON pd.project_id = p.id
            WHERE p.status = 'active'
            """
        ).fetchall()

        matching = []
        for r in rows:
            dir_path = canonical_project_path(r["dir_path"])
            if norm_target == dir_path or norm_target.startswith(dir_path.rstrip(os.sep) + os.sep):
                matching.append((len(dir_path), r["id"]))

        if not matching:
            return None, False

        # Sort descending by match length
        matching.sort(key=lambda x: x[0], reverse=True)
        top_len, top_id = matching[0]

        # Check for ambiguity: multiple projects matching with the same longest prefix
        tied = [mid for mlen, mid in matching if mlen == top_len]
        unique_tied = set(tied)
        if len(unique_tied) > 1:
            return None, True  # Ambiguity conflict!

        return get_project(top_id), False


def reconstruct_or_resolve_project(cwd: str | None, metadata: dict[str, Any] | None = None) -> str | None:
    """Resolve an existing project by cwd or reconstruct a new project without hidden defaults."""
    if not cwd and not metadata:
        return None

    if cwd:
        norm_cwd = os.path.abspath(cwd.strip())
        matched, is_ambiguous = resolve_project_by_path(norm_cwd)
        if matched and not is_ambiguous:
            return matched["id"]
        if is_ambiguous:
            return None

        repo = repository_identity(norm_cwd)
        if repo:
            common = repo["common_root"]
            matched, ambiguous = resolve_project_by_path(common)
            if ambiguous:
                return None
            if matched:
                with db() as conn:
                    conn.execute("INSERT OR IGNORE INTO project_directories(id,project_id,path,created_at) VALUES(?,?,?,?)",
                                 (new_id("dir"), matched["id"], repo["root"], now_iso()))
                return matched["id"]
            norm_cwd = common

        # Reconstruct new project based on actual cwd/project metadata
        proj_name = (metadata.get("name") or metadata.get("title") or "").strip() if metadata else ""
        if not proj_name:
            basename = os.path.basename(norm_cwd)
            proj_name = basename if basename else norm_cwd

        desc = (metadata.get("description") or f"自动从工作目录还原: {norm_cwd}") if metadata else f"自动从工作目录还原: {norm_cwd}"
        new_proj = create_project(name=proj_name, description=desc, directories=[norm_cwd])
        return new_proj["id"]

    if metadata and metadata.get("name"):
        proj_name = metadata["name"].strip()
        if not proj_name:
            return None
        with db() as conn:
            matches = conn.execute(
                "SELECT id FROM projects WHERE name = ? AND status = 'active'", (proj_name,),
            ).fetchall()
        if len(matches) == 1:
            return matches[0]["id"]
        if len(matches) > 1:
            return None
        return create_project(name=proj_name, description=metadata.get("description", ""))["id"]

    return None
