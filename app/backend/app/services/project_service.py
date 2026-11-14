from datetime import datetime, timezone
import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode, decode


_DARWIN_VAR_TEMP_PREFIX_RE = re.compile(r"^/(?:private/)?var/folders/[^/]+/[^/]+/T(?:/(.*))?$")
_POSIX_TMP_PREFIX_RE = re.compile(r"^/(?:private/)?(?:var/)?tmp(?:/(.*))?$")
_BROAD_SYSTEM_ROOTS = frozenset({
    "/",
    "/Users",
    "/home",
    "/Volumes",
    "/System",
    "/Applications",
    "/Library",
    "/private",
    "/var",
    "/opt",
})
_HOME_CONTAINER_DIRS = frozenset({
    "Desktop",
    "Documents",
    "Downloads",
    "Library",
    "Movies",
    "Music",
    "Pictures",
    "Public",
})
_EPHEMERAL_SEGMENTS = (
    "/.aionui/",
    "/Application Support/AionUi/",
    "/.gemini/antigravity/brain/",
    "/.gemini/antigravity-cli/brain/",
    "/.gemini/antigravity-ide/brain/",
    "/.codex/sessions/",
    "/.codex/archived_sessions/",
    "/.codex/memories",
    "/.omp/agent/sessions/",
    "/.claude/projects/",
    "/.contextcore/",
    "/Application Support/ContextCore/",
)


def _normalize_iso_utc(ts: Any) -> str | None:
    if not ts or not isinstance(ts, str):
        return None
    raw = ts.strip()
    if not raw:
        return None
    try:
        clean = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def _ts_epoch_ms(ts: Any) -> int:
    """Convert ISO timestamp to integer epoch milliseconds (Rule #15: never order timestamps lexicographically)."""
    norm = _normalize_iso_utc(ts)
    if not norm:
        return 0
    try:
        dt = datetime.fromisoformat(norm)
        return int(round(dt.timestamp() * 1000))
    except Exception:
        return 0


def _latest_timestamp(*candidates: Any) -> str | None:
    best_ts: str | None = None
    best_ms = -1
    for cand in candidates:
        if not cand or not isinstance(cand, str):
            continue
        norm = _normalize_iso_utc(cand) or cand.strip()
        if not norm:
            continue
        ms = _ts_epoch_ms(norm)
        if ms > best_ms:
            best_ms = ms
            best_ts = norm
    return best_ts


def is_broad_or_ephemeral_project_path(path: str | None) -> bool:
    """Return True if path is an OS/user container root, temp directory, or internal agent scratch directory."""
    if not path or not str(path).strip():
        return True
    raw_expanded = os.path.abspath(os.path.expanduser(str(path).strip()))
    try:
        real_norm = os.path.realpath(raw_expanded)
    except OSError:
        real_norm = raw_expanded

    for candidate in dict.fromkeys((raw_expanded.rstrip(os.sep) or "/", real_norm.rstrip(os.sep) or "/")):
        if candidate in _BROAD_SYSTEM_ROOTS:
            return True
        if os.path.basename(candidate).startswith("omp-temp-"):
            return True
        slashed = candidate + "/"
        if any(seg in slashed for seg in _EPHEMERAL_SEGMENTS):
            return True
        # Bare temp roots (depth 0, e.g. /tmp, /var/folders/.../T) and direct top-level
        # temp scratch folders (depth 1, e.g. /tmp/cc-probe, /var/folders/.../T/tmpXXXXXX)
        # are ephemeral; nested workspaces inside isolated sandboxes (depth >= 2) remain valid.
        for temp_re in (_POSIX_TMP_PREFIX_RE, _DARWIN_VAR_TEMP_PREFIX_RE):
            m_temp = temp_re.match(candidate)
            if m_temp:
                rel_tail = [p for p in (m_temp.group(1) or "").split("/") if p]
                if len(rel_tail) <= 1:
                    return True
        parts = [p for p in candidate.split("/") if p]
        if len(parts) == 2 and parts[0] in ("Users", "home", "Volumes"):
            return True
        if len(parts) == 3 and parts[0] in ("Users", "home") and parts[2] in _HOME_CONTAINER_DIRS:
            return True

    try:
        home_raw = os.path.abspath(os.path.expanduser("~")).rstrip(os.sep) or "/"
        home_real = os.path.realpath(home_raw).rstrip(os.sep) or "/"
        for candidate in (raw_expanded.rstrip(os.sep) or "/", real_norm.rstrip(os.sep) or "/"):
            if candidate in (home_raw, home_real):
                return True
            for h in (home_raw, home_real):
                if h != "/" and candidate.startswith(h + "/"):
                    rel_parts = [p for p in candidate[len(h) + 1 :].split("/") if p]
                    if len(rel_parts) == 1 and rel_parts[0] in _HOME_CONTAINER_DIRS:
                        return True
    except OSError:
        pass
    return False


def touch_project_activity(conn, project_id: str | None, activity_at: str | None = None) -> None:
    """Monotonically advance projects.updated_at using epoch-ms comparison."""
    if not project_id:
        return
    row = conn.execute("SELECT updated_at FROM projects WHERE id = ?", (project_id,)).fetchone()
    if not row:
        return
    candidate = _normalize_iso_utc(activity_at) or (activity_at.strip() if isinstance(activity_at, str) and activity_at.strip() else now_iso())
    current = row["updated_at"]
    if not current or _ts_epoch_ms(candidate) > _ts_epoch_ms(current):
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (candidate, project_id))


def _compute_project_effective_updated_at(conn, project_row: dict[str, Any], *, has_wt: bool | None = None) -> str:
    pid = project_row["id"]
    candidates: list[Any] = []
    for r in conn.execute(
        "SELECT updated_at FROM conversations WHERE project_id = ? AND updated_at IS NOT NULL ORDER BY updated_at DESC LIMIT 8",
        (pid,),
    ).fetchall():
        candidates.append(r["updated_at"])
    for r in conn.execute(
        "SELECT updated_at FROM contents WHERE project_id = ? AND status = 'active' AND updated_at IS NOT NULL ORDER BY updated_at DESC LIMIT 8",
        (pid,),
    ).fetchall():
        candidates.append(r["updated_at"])
    if has_wt is None:
        has_wt = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_tasks' LIMIT 1").fetchone())
    if has_wt:
        for r in conn.execute(
            "SELECT updated_at FROM working_tasks WHERE project_id = ? AND updated_at IS NOT NULL ORDER BY updated_at DESC LIMIT 8",
            (pid,),
        ).fetchall():
            candidates.append(r["updated_at"])
    is_auto_reconstructed = str(project_row.get("description") or "").startswith("自动从工作目录还原:")
    proj_updated = project_row.get("updated_at")
    proj_created = project_row.get("created_at")
    if candidates and is_auto_reconstructed and proj_updated == proj_created:
        best = _latest_timestamp(*candidates)
        if best:
            return best
    best = _latest_timestamp(proj_updated, *candidates)
    return best or str(proj_updated or proj_created or now_iso())


def _get_project_counts(conn, project_id: str) -> dict[str, int]:
    conv_count = conn.execute(
        "SELECT COUNT(*) as c FROM conversations WHERE project_id = ?", (project_id,)
    ).fetchone()["c"]
    mem_count = conn.execute(
        "SELECT COUNT(*) as c FROM contents WHERE project_id = ? AND kind = 'memory' AND category != 'task' AND status = 'active'",
        (project_id,),
    ).fetchone()["c"]
    doc_count = conn.execute(
        "SELECT COUNT(*) as c FROM contents WHERE project_id = ? AND kind = 'document' AND status = 'active'",
        (project_id,),
    ).fetchone()["c"]
    has_working_tasks = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_tasks' LIMIT 1"
    ).fetchone()
    if has_working_tasks:
        wt_count = conn.execute(
            "SELECT COUNT(*) as c FROM working_tasks WHERE project_id = ? AND status IN ('active', 'in_progress', 'blocked')",
            (project_id,),
        ).fetchone()["c"]
        legacy_unmirrored = conn.execute(
            """
            SELECT COUNT(*) as c FROM contents c
            WHERE c.project_id = ? AND c.category = 'task' AND c.status = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM working_tasks wt
                  WHERE wt.project_id = c.project_id AND wt.legacy_content_id = c.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM tombstones tb
                  WHERE tb.entity_type = 'memory' AND tb.dedupe_key = ('legacy_task:' || c.id)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM json_each(
                      CASE WHEN json_valid(c.tags) AND json_type(c.tags) = 'array' THEN c.tags ELSE '[]' END
                  ) AS tv WHERE tv.value IN ('status:completed', 'status:cancelled', 'status:paused')
              )
            """,
            (project_id,),
        ).fetchone()["c"]
        task_count = wt_count + legacy_unmirrored
    else:
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
                "SELECT * FROM projects WHERE status = ?", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects"
            ).fetchall()
        has_wt = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_tasks' LIMIT 1").fetchone())
        result = []
        for r in rows:
            p = row_to_dict(r)
            p["updated_at"] = _compute_project_effective_updated_at(conn, p, has_wt=has_wt)
            p["directories"] = _get_project_directories(conn, p["id"])
            p["counts"] = _get_project_counts(conn, p["id"])
            result.append(p)
        result.sort(key=lambda item: (_ts_epoch_ms(item.get("updated_at")), str(item.get("updated_at") or ""), item.get("id") or ""), reverse=True)
        return result


def get_project(project_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        p = row_to_dict(row)
        p["updated_at"] = _compute_project_effective_updated_at(conn, p)
        p["directories"] = _get_project_directories(conn, p["id"])
        p["counts"] = _get_project_counts(conn, p["id"])
        return p


def create_project(
    name: str,
    description: str = "",
    directories: list[str] | None = None,
    *,
    activity_at: str | None = None,
    manual: bool = False,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="Project name cannot be empty")
    pid = new_id("proj")
    now = now_iso()
    effective_ts = _normalize_iso_utc(activity_at) or now
    created_ts = effective_ts if _ts_epoch_ms(effective_ts) < _ts_epoch_ms(now) else now
    with db() as conn:
        conn.execute(
            """
            INSERT INTO projects (id, name, description, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (pid, clean_name, description.strip(), created_ts, effective_ts),
        )
        if directories:
            for d in directories:
                norm_d = os.path.abspath(d.strip())
                conn.execute(
                    "INSERT OR IGNORE INTO project_directories (id, project_id, path, created_at) VALUES (?, ?, ?, ?)",
                    (new_id("pdir"), pid, norm_d, now),
                )
        log_audit(actor="user", action="project.create", target=pid, conn=conn)
        if manual:
            log_audit(actor="user", action="project.create_manual", target=pid, conn=conn)
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
    from app.services import governance_guardrails

    item = conn.execute(
        "SELECT kind,category,title,content FROM contents WHERE id=?", (content_id,),
    ).fetchone()
    digest = compute_content_hash(project_id, item["category"], item["title"], item["content"])
    if conn.execute(
        "SELECT 1 FROM tombstones WHERE entity_type='content' AND dedupe_key=?", (digest,),
    ).fetchone():
        raise HTTPException(410, "Project move would restore permanently deleted content")
    if governance_guardrails.is_tombstoned_memory(
        conn,
        project_id,
        item["category"],
        item["title"] or "",
        item["content"] or "",
        include_cross_category=(item["kind"] == "memory"),
    ):
        raise HTTPException(410, "Project move would restore permanently deleted content")
    can_key = governance_guardrails.canonical_memory_key(
        project_id,
        item["category"],
        item["title"] or "",
        item["content"] or "",
    )
    conn.execute(
        "UPDATE contents SET project_id=?,content_hash=?,canonical_key=?,version=version+1,updated_at=? WHERE id=?",
        (project_id, digest, can_key, now, content_id),
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

            # Move conversations, conflicts, handoffs, working tasks, and telemetry
            from app.services import telemetry_service, working_memory_service
            working_memory_service.ensure_schema(conn)
            telemetry_service.ensure_schema(conn)
            conn.execute("UPDATE conversations SET project_id = ?, updated_at = ? WHERE project_id = ?", (target_id, now, sid))
            conn.execute("UPDATE conflicts SET project_id = ? WHERE project_id = ?", (target_id, sid))
            conn.execute("UPDATE handoffs SET project_id = ? WHERE project_id = ?", (target_id, sid))
            conn.execute("UPDATE working_tasks SET project_id = ?, updated_at = ? WHERE project_id = ?", (target_id, now, sid))
            conn.execute("UPDATE memory_usage_events SET project_id = ? WHERE project_id = ?", (target_id, sid))
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

        # Strictly verify all requested directories belong to source project
        validated_dirs: list[tuple[str, str]] = []
        if directories:
            src_dir_rows = conn.execute(
                "SELECT path FROM project_directories WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            src_dir_lookup: dict[str, str] = {}
            for r in src_dir_rows:
                stored_path = str(r["path"])
                abs_src = os.path.abspath(os.path.expanduser(stored_path))
                real_src = os.path.realpath(os.path.expanduser(stored_path))
                src_dir_lookup.setdefault(abs_src, stored_path)
                src_dir_lookup.setdefault(real_src, stored_path)
            seen_dirs: set[str] = set()
            for d in directories:
                raw_d = (d or "").strip()
                if not raw_d:
                    raise HTTPException(status_code=422, detail="Directory path must not be empty")
                abs_d = os.path.abspath(os.path.expanduser(raw_d))
                real_d = os.path.realpath(os.path.expanduser(raw_d))
                matched_stored = src_dir_lookup.get(abs_d) or src_dir_lookup.get(real_d)
                if not matched_stored:
                    raise HTTPException(status_code=404, detail=f"Directory '{raw_d}' not found on source project")
                if matched_stored not in seen_dirs:
                    seen_dirs.add(matched_stored)
                    validated_dirs.append((matched_stored, os.path.abspath(os.path.expanduser(matched_stored))))

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
        if validated_dirs:
            for stored_path, norm_d in validated_dirs:
                conn.execute("DELETE FROM project_directories WHERE project_id = ? AND path = ?", (project_id, stored_path))
                conn.execute(
                    "INSERT OR IGNORE INTO project_directories (id, project_id, path, created_at) VALUES (?, ?, ?, ?)",
                    (new_id("pdir"), new_pid, norm_d, now),
                )

        from app.services import working_memory_service
        working_memory_service.ensure_schema(conn)
        if conversation_ids:
            for cid in conversation_ids:
                conn.execute(
                    "UPDATE conversations SET project_id = ?, updated_at = ? WHERE id = ?",
                    (new_pid, now, cid),
                )
                conn.execute(
                    "UPDATE working_tasks SET project_id = ?, updated_at = ? WHERE project_id = ? AND source_conversation_id = ?",
                    (new_pid, now, project_id, cid),
                )

        if content_ids:
            for cnt_id in dict.fromkeys(content_ids):
                _move_content(conn, cnt_id, new_pid, now)
                conn.execute(
                    "UPDATE working_tasks SET project_id = ?, updated_at = ? WHERE project_id = ? AND legacy_content_id = ?",
                    (new_pid, now, project_id, cnt_id),
                )
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


def cwd_identity(path: str | None) -> str:
    """Stable directory identity for dedupe and code-state comparison."""
    text = str(path or "").strip()
    if not text:
        return ""
    try:
        return os.path.realpath(os.path.abspath(text)).rstrip(os.sep)
    except OSError:
        return text.rstrip(os.sep)


def _sha_text(value: str) -> bool:
    return len(value) in (40, 64) and all(char in "0123456789abcdef" for char in value)


def _common_git_dir(git_dir: Path) -> Path:
    marker = git_dir / "commondir"
    if not marker.is_file():
        return git_dir
    try:
        raw = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return git_dir
    common = Path(raw)
    if not common.is_absolute():
        common = git_dir / common
    try:
        return common.resolve()
    except OSError:
        return common


def _resolve_git_ref(git_dir: Path, ref: str) -> str | None:
    dirs = [git_dir]
    common = _common_git_dir(git_dir)
    if common != git_dir:
        dirs.append(common)
    for base in dirs:
        loose = base / ref
        try:
            if loose.is_file():
                text = loose.read_text(encoding="utf-8", errors="replace").strip()
                if _sha_text(text):
                    return text
        except OSError:
            continue
    for base in dirs:
        packed = base / "packed-refs"
        try:
            if not packed.is_file():
                continue
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line or line.startswith("#") or line.startswith("^"):
                    continue
                sha, _, name = line.partition(" ")
                if name.strip() == ref and _sha_text(sha.strip()):
                    return sha.strip()
        except OSError:
            continue
    return None


def read_git_checkout(path: str | None) -> dict[str, Any] | None:
    """Read the branch and commit of a checkout from Git files. Does not run Git."""
    if not path or not os.path.isdir(str(path)):
        return None
    identity = repository_identity(str(path))
    if not identity:
        return None
    root = Path(identity["root"])
    marker = root / ".git"
    try:
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            line = marker.read_text(encoding="utf-8", errors="replace")[:4096].strip()
            if not line.startswith("gitdir:"):
                return None
            git_dir = Path(line.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = root / git_dir
        else:
            return None
        head_text = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, UnicodeError):
        return None
    if head_text.startswith("ref:"):
        ref = head_text.split(":", 1)[1].strip()
        branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None
        return {
            "root": str(root),
            "branch": branch,
            "head": _resolve_git_ref(git_dir, ref),
            "detached": False,
        }
    if _sha_text(head_text):
        return {"root": str(root), "branch": None, "head": head_text, "detached": True}
    return None


def _git_output(cwd: str, args: list[str], timeout: float = 15) -> bytes | None:
    """Read-only Git invocation. Hooks are pointed at an empty directory."""
    command = ["git", "--no-pager", "-C", cwd, "--no-optional-locks"]
    if os.path.isdir("/var/empty"):
        command.extend(["-c", "core.hooksPath=/var/empty"])
    command.extend(args)
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _hash_worktree_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    try:
        if path.is_symlink():
            digest.update(b"symlink:")
            digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
        elif (path / ".git").exists():
            sub_checkout = read_git_checkout(str(path))
            sub_head = str((sub_checkout or {}).get("head") or "")
            sub_dirty = _dirty_fingerprint(str(path)) if sub_head else None
            digest.update(f"gitlink:{sub_head}:{sub_dirty or 'incomplete'}".encode("utf-8"))
        elif path.is_file():
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > 48 * 1024 * 1024:
                    digest.update(str(size).encode())
                    digest.update(str(path.stat().st_mtime_ns).encode())
                    digest.update(handle.read(1024 * 1024))
                    if size > 1024 * 1024:
                        handle.seek(max(0, size - 1024 * 1024))
                        digest.update(handle.read(1024 * 1024))
                else:
                    while True:
                        chunk = handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
        else:
            digest.update(b"missing")
    except OSError:
        digest.update(b"unreadable")
    return digest.digest()


def _parse_raw_diff_z(raw: bytes) -> list[tuple[bytes, bytes]]:
    parts = raw.split(b"\0")
    entries: list[tuple[bytes, bytes]] = []
    index = 0
    while index + 1 < len(parts):
        meta, name = parts[index], parts[index + 1]
        index += 2
        if meta and name:
            entries.append((meta, name))
    return entries


def _dirty_fingerprint(worktree_root: str) -> str | None:
    """Fingerprint of staged index changes, unstaged worktree changes, and untracked files. None if Git cannot be read."""
    cached = _git_output(worktree_root, ["diff", "--cached", "--raw", "--no-abbrev", "--no-renames", "-z", "--no-ext-diff", "HEAD"])
    unstaged = _git_output(worktree_root, ["diff", "--raw", "--no-abbrev", "--no-renames", "-z", "--no-ext-diff"])
    others = _git_output(worktree_root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if cached is None or unstaged is None or others is None:
        return None
    cached_entries = _parse_raw_diff_z(cached)
    unstaged_entries = _parse_raw_diff_z(unstaged)
    untracked = [item for item in others.split(b"\0") if item]
    if not cached_entries and not unstaged_entries and not untracked:
        return "clean"
    digest = hashlib.sha256()
    root = Path(worktree_root)
    for meta, name in cached_entries:
        digest.update(b"C\0")
        digest.update(meta)
        digest.update(b"\0")
        digest.update(name)
        digest.update(b"\0")
    for meta, name in unstaged_entries:
        status = meta.rsplit(b" ", 1)[-1]
        digest.update(b"W\0")
        digest.update(meta)
        digest.update(b"\0")
        digest.update(name)
        digest.update(b"\0")
        if status.startswith(b"D"):
            digest.update(b"deleted")
        else:
            digest.update(_hash_worktree_file(root / name.decode("utf-8", "surrogateescape")))
        digest.update(b"\0")
    for name in untracked:
        digest.update(b"??\0")
        digest.update(name)
        digest.update(b"\0")
        digest.update(_hash_worktree_file(root / name.decode("utf-8", "surrogateescape")))
        digest.update(b"\0")
    return digest.hexdigest()


def worktree_code_state(cwd: str | None, cache: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Snapshot the commit and uncommitted changes for a conversation directory.

    The fingerprint is a hash, never file contents. `complete` is false when the
    commit can be seen but uncommitted changes could not be checked.
    """
    key = cwd_identity(cwd)
    if cache is not None and key in cache:
        return cache[key]
    empty = {
        "kind": "not_git",
        "cwd": key,
        "branch": None,
        "head": None,
        "detached": False,
        "dirty": False,
        "complete": True,
        "fingerprint": "not_git",
    }
    if not key:
        state = empty
    elif not os.path.isdir(key):
        state = {**empty, "kind": "unavailable", "dirty": None, "complete": False, "fingerprint": None}
    else:
        checkout = read_git_checkout(key)
        if not checkout or not checkout.get("head"):
            state = empty if not checkout else {
                **empty,
                "kind": "git",
                "branch": checkout.get("branch"),
                "detached": bool(checkout.get("detached")),
                "dirty": None,
                "complete": False,
                "fingerprint": None,
            }
        else:
            dirty = _dirty_fingerprint(str(checkout["root"]))
            if dirty is None:
                state = {
                    "kind": "git",
                    "cwd": key,
                    "branch": checkout.get("branch"),
                    "head": checkout.get("head"),
                    "detached": bool(checkout.get("detached")),
                    "dirty": None,
                    "complete": False,
                    "fingerprint": None,
                }
            else:
                fingerprint = hashlib.sha256(f"{checkout['head']}\n{dirty}".encode()).hexdigest()
                state = {
                    "kind": "git",
                    "cwd": key,
                    "branch": checkout.get("branch"),
                    "head": checkout.get("head"),
                    "detached": bool(checkout.get("detached")),
                    "dirty": dirty != "clean",
                    "complete": True,
                    "fingerprint": fingerprint,
                }
    if cache is not None:
        cache[key] = state
    return state


def code_states_compatible(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    if not isinstance(expected, dict) or not isinstance(current, dict):
        return False
    if expected.get("kind") != current.get("kind"):
        return False
    if cwd_identity(str(expected.get("cwd") or "")) != cwd_identity(str(current.get("cwd") or "")):
        return False
    if expected.get("kind") != "git":
        return True
    if not expected.get("complete") or not current.get("complete"):
        return False
    if expected.get("head") != current.get("head"):
        return False
    if expected.get("branch") != current.get("branch"):
        return False
    if bool(expected.get("detached")) != bool(current.get("detached")):
        return False
    return expected.get("fingerprint") == current.get("fingerprint")


def _resolve_project_id_by_path_conn(conn, path: str) -> tuple[str | None, bool]:
    if not path or is_broad_or_ephemeral_project_path(path):
        return None, False
    norm_target = canonical_project_path(path)
    rows = conn.execute(
        """
        SELECT p.id, pd.path as dir_path
        FROM project_directories pd
        JOIN projects p ON pd.project_id = p.id
        WHERE p.status = 'active'
        """
    ).fetchall()
    matching = []
    for r in rows:
        if is_broad_or_ephemeral_project_path(r["dir_path"]):
            continue
        dir_path = canonical_project_path(r["dir_path"])
        if norm_target == dir_path or norm_target.startswith(dir_path.rstrip(os.sep) + os.sep):
            matching.append((len(dir_path), r["id"]))
    if not matching:
        return None, False
    matching.sort(key=lambda x: x[0], reverse=True)
    top_len, top_id = matching[0]
    unique_tied = {mid for mlen, mid in matching if mlen == top_len}
    if len(unique_tied) > 1:
        return None, True
    return top_id, False


def resolve_project_by_path(path: str) -> tuple[dict[str, Any] | None, bool]:
    with db() as conn:
        top_id, ambiguous = _resolve_project_id_by_path_conn(conn, path)
    if ambiguous:
        return None, True
    if not top_id:
        return None, False
    return get_project(top_id), False


def reconstruct_or_resolve_project(
    cwd: str | None,
    metadata: dict[str, Any] | None = None,
    *,
    activity_at: str | None = None,
) -> str | None:
    """Resolve an existing project by cwd or reconstruct a new project without hidden defaults."""
    if not cwd and not metadata:
        return None

    if cwd and str(cwd).strip():
        norm_cwd = os.path.abspath(str(cwd).strip())
        if is_broad_or_ephemeral_project_path(norm_cwd):
            return None
        matched, is_ambiguous = resolve_project_by_path(norm_cwd)
        if matched and not is_ambiguous:
            return matched["id"]
        if is_ambiguous:
            return None

        repo = repository_identity(norm_cwd)
        if repo:
            common = repo["common_root"]
            if not is_broad_or_ephemeral_project_path(common):
                matched, ambiguous = resolve_project_by_path(common)
                if ambiguous:
                    return None
                if matched:
                    with db() as conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO project_directories(id,project_id,path,created_at) VALUES(?,?,?,?)",
                            (new_id("dir"), matched["id"], repo["root"], now_iso()),
                        )
                        touch_project_activity(conn, matched["id"], activity_at)
                    return matched["id"]
                norm_cwd = common

        # Reconstruct new project based on actual cwd/project metadata
        proj_name = (metadata.get("name") or metadata.get("title") or "").strip() if metadata else ""
        if not proj_name:
            basename = os.path.basename(norm_cwd)
            proj_name = basename if basename else norm_cwd

        desc = (metadata.get("description") or f"自动从工作目录还原: {norm_cwd}") if metadata else f"自动从工作目录还原: {norm_cwd}"
        new_proj = create_project(
            name=proj_name,
            description=desc,
            directories=[norm_cwd],
            activity_at=activity_at,
        )
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
        return create_project(
            name=proj_name,
            description=metadata.get("description", ""),
            activity_at=activity_at,
        )["id"]

    return None


def repair_misclassified_projects() -> dict[str, Any]:
    """
    Idempotently repair legacy broad/ephemeral blackhole projects (e.g. $HOME, /tmp, omp-temp-*)
    and backfill missing workspace cwd/project bindings for Antigravity and unassigned sessions.
    """
    from app.adapters.antigravity import infer_transcript_cwd
    from app.adapters.base import AgentAdapterError, validate_path

    backfilled_cwd_count = 0
    repaired_conversations = 0
    repaired_contents = 0
    repaired_tasks = 0
    archived_blackholes = 0

    # 1. Backfill missing cwd on Antigravity conversations whose transcript exists on disk
    ag_roots: list[str] = []
    ag_rows = []
    with db() as conn:
        ag_conn = conn.execute(
            "SELECT enabled, paused, paths FROM connections WHERE id = 'antigravity'"
        ).fetchone()
        if ag_conn and ag_conn["enabled"] and not ag_conn["paused"]:
            ag_roots = [str(p) for p in decode(ag_conn["paths"], []) if p]
            if ag_roots:
                ag_rows = conn.execute(
                    """
                    SELECT id, source_path FROM conversations
                    WHERE agent = 'antigravity' AND (cwd IS NULL OR cwd = '') AND source_path IS NOT NULL AND source_path != ''
                    """
                ).fetchall()
    for ag in ag_rows:
        try:
            validated_source = validate_path(ag["source_path"], ag_roots)
        except AgentAdapterError:
            continue
        inferred = infer_transcript_cwd(validated_source, roots=ag_roots)
        if inferred and not is_broad_or_ephemeral_project_path(inferred):
            checkout = read_git_checkout(inferred)
            branch = None if not checkout or checkout.get("detached") else checkout.get("branch")
            head = checkout.get("head") if checkout else None
            with db() as conn:
                conn.execute(
                    """
                    UPDATE conversations
                    SET cwd = ?,
                        git_branch = COALESCE(git_branch, ?),
                        git_head = COALESCE(git_head, ?)
                    WHERE id = ?
                    """,
                    (inferred, branch, head, ag["id"]),
                )
            backfilled_cwd_count += 1

    # 2. Identify active projects whose directories (or auto-reconstructed origin path) are exclusively broad/ephemeral blackholes
    blackhole_ids: list[str] = []
    auto_prefix = "自动从工作目录还原:"
    with db() as conn:
        curated_ids = {
            row["target"]
            for row in conn.execute(
                "SELECT DISTINCT target FROM audit_logs WHERE action IN ('project.create_manual', 'project.update')"
            ).fetchall()
            if row["target"]
        }
        active_projs = conn.execute("SELECT id, name, description FROM projects WHERE status = 'active'").fetchall()
        for proj in active_projs:
            if proj["id"] in curated_ids:
                continue
            dirs = _get_project_directories(conn, proj["id"])
            desc = str(proj["description"] or "").strip()
            if desc and not desc.startswith(auto_prefix):
                continue
            proj_name = str(proj["name"] or "").strip()
            if dirs and not desc.startswith(auto_prefix):
                dir_basenames = {os.path.basename(os.path.abspath(d)) or os.path.abspath(d) for d in dirs}
                if proj_name and proj_name not in dir_basenames:
                    continue
            origin_path = desc[len(auto_prefix) :].strip() if desc.startswith(auto_prefix) else ""
            if dirs and all(is_broad_or_ephemeral_project_path(d) for d in dirs):
                blackhole_ids.append(proj["id"])
            elif not dirs and origin_path and is_broad_or_ephemeral_project_path(origin_path):
                blackhole_ids.append(proj["id"])
        # Detach broad/ephemeral directory bindings immediately so reconstruction resolves cleanly
        for bh_id in blackhole_ids:
            conn.execute("DELETE FROM project_directories WHERE project_id = ?", (bh_id,))
    # 3. Collect conversations trapped in blackhole projects OR unassigned with a real cwd
    with db() as conn:
        candidates: list[dict[str, Any]] = []
        if blackhole_ids:
            placeholders = ",".join("?" for _ in blackhole_ids)
            for r in conn.execute(
                f"SELECT id, project_id, cwd, started_at, updated_at FROM conversations WHERE project_id IN ({placeholders})",
                tuple(blackhole_ids),
            ).fetchall():
                candidates.append(dict(r))
        for r in conn.execute(
            "SELECT id, project_id, cwd, started_at, updated_at FROM conversations WHERE project_id IS NULL AND cwd IS NOT NULL AND cwd != ''"
        ).fetchall():
            candidates.append(dict(r))

    # Sort shallowest cwd first so parent workspace roots (e.g. Desktop/改造) create their project
    # before nested subfolders (e.g. Desktop/改造/ContextCore) resolve into them.
    def _sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
        raw_c = str(row.get("cwd") or "").strip()
        if not raw_c:
            return (9999, str(row.get("started_at") or ""), str(row["id"]))
        parts = [p for p in os.path.abspath(raw_c).split(os.sep) if p]
        return (len(parts), str(row.get("started_at") or ""), str(row["id"]))

    candidates.sort(key=_sort_key)

    conv_moves: dict[str, tuple[str | None, str | None]] = {}
    for row in candidates:
        cid = row["id"]
        old_pid = row["project_id"]
        raw_cwd = str(row.get("cwd") or "").strip()
        activity_ts = row.get("updated_at") or row.get("started_at")
        if not raw_cwd or is_broad_or_ephemeral_project_path(raw_cwd):
            new_pid = None
        else:
            new_pid = reconstruct_or_resolve_project(raw_cwd, activity_at=activity_ts)
        if new_pid != old_pid:
            with db() as conn:
                conn.execute("UPDATE conversations SET project_id = ? WHERE id = ?", (new_pid, cid))
                if new_pid:
                    touch_project_activity(conn, new_pid, activity_ts)
            conv_moves[cid] = (old_pid, new_pid)
            repaired_conversations += 1

    # 4. Move linked contents, working_tasks, handoffs, and conflicts out of blackhole projects
    if blackhole_ids:
        now = now_iso()
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            has_wt = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_tasks' LIMIT 1").fetchone())
            for bh_id in blackhole_ids:
                # Contents linked via content_sources to a moved conversation
                content_rows = conn.execute(
                    "SELECT id, updated_at FROM contents WHERE project_id = ?", (bh_id,)
                ).fetchall()
                for ct in content_rows:
                    src_targets = conn.execute(
                        """
                        SELECT DISTINCT cv.project_id
                        FROM content_sources cs
                        JOIN conversations cv ON cs.source_kind = 'conversation' AND cs.source_id = cv.id
                        WHERE cs.content_id = ? AND cv.project_id IS NOT NULL AND cv.project_id != ?
                        """,
                        (ct["id"], bh_id),
                    ).fetchall()
                    if len(src_targets) == 1:
                        dest_pid = src_targets[0]["project_id"]
                        try:
                            _move_content(conn, ct["id"], dest_pid, ct["updated_at"] or now)
                            touch_project_activity(conn, dest_pid, ct["updated_at"])
                            repaired_contents += 1
                        except HTTPException:
                            # Tombstone collision in destination project; leave untouched
                            pass

                if has_wt:
                    wt_rows = conn.execute(
                        "SELECT id, cwd, source_conversation_id, updated_at FROM working_tasks WHERE project_id = ?",
                        (bh_id,),
                    ).fetchall()
                    for wt in wt_rows:
                        dest_pid = None
                        if wt["source_conversation_id"]:
                            cv = conn.execute(
                                "SELECT project_id FROM conversations WHERE id = ?",
                                (wt["source_conversation_id"],),
                            ).fetchone()
                            if cv and cv["project_id"] and cv["project_id"] != bh_id:
                                dest_pid = cv["project_id"]
                        if not dest_pid and wt["cwd"] and not is_broad_or_ephemeral_project_path(wt["cwd"]):
                            matched_id, amb = _resolve_project_id_by_path_conn(conn, wt["cwd"])
                            if matched_id and not amb and matched_id != bh_id:
                                dest_pid = matched_id
                        if dest_pid:
                            conn.execute("UPDATE working_tasks SET project_id = ? WHERE id = ?", (dest_pid, wt["id"]))
                            touch_project_activity(conn, dest_pid, wt["updated_at"])
                            repaired_tasks += 1

                # Move conflicts whose existing_id moved to a repaired project
                conn.execute(
                    """
                    UPDATE conflicts
                    SET project_id = (
                        SELECT ct.project_id FROM contents ct WHERE ct.id = conflicts.existing_id
                    )
                    WHERE project_id = ?
                      AND EXISTS (
                          SELECT 1 FROM contents ct
                          WHERE ct.id = conflicts.existing_id
                            AND ct.project_id IS NOT NULL
                            AND ct.project_id != ?
                      )
                    """,
                    (bh_id, bh_id),
                )

                # Move handoffs whose referenced sources or card working_directory map to a single repaired project
                for hf in conn.execute(
                    "SELECT id, source_ids, card FROM handoffs WHERE project_id = ?",
                    (bh_id,),
                ).fetchall():
                    target_pids: set[str] = set()
                    for sid in decode(hf["source_ids"], []):
                        if not isinstance(sid, str) or not sid:
                            continue
                        ct_row = conn.execute("SELECT project_id FROM contents WHERE id = ?", (sid,)).fetchone()
                        if ct_row and ct_row["project_id"] and ct_row["project_id"] != bh_id:
                            target_pids.add(ct_row["project_id"])
                        cv_row = conn.execute("SELECT project_id FROM conversations WHERE id = ?", (sid,)).fetchone()
                        if cv_row and cv_row["project_id"] and cv_row["project_id"] != bh_id:
                            target_pids.add(cv_row["project_id"])
                    if not target_pids:
                        card_obj = decode(hf["card"], {})
                        card_wd = card_obj.get("working_directory") if isinstance(card_obj, dict) else None
                        if isinstance(card_wd, str) and card_wd and not is_broad_or_ephemeral_project_path(card_wd):
                            matched_id, amb = _resolve_project_id_by_path_conn(conn, card_wd)
                            if matched_id and not amb and matched_id != bh_id:
                                target_pids.add(matched_id)
                    if len(target_pids) == 1:
                        conn.execute(
                            "UPDATE handoffs SET project_id = ? WHERE id = ?",
                            (next(iter(target_pids)), hf["id"]),
                        )

                # Archive blackhole/ephemeral project so it never clutters active project list,
                # while keeping any leftover unlinked memories searchable.
                conn.execute(
                    "UPDATE projects SET status = 'archived', archived_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, bh_id),
                )
                log_audit(actor="daemon", action="project.repair_archive_blackhole", target=bh_id, conn=conn)
                archived_blackholes += 1

    # 5. Synchronize stored updated_at on auto-reconstructed active projects with their real item activity
    with db() as conn:
        has_wt = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'working_tasks' LIMIT 1").fetchone())
        for r in conn.execute("SELECT * FROM projects WHERE status = 'active'").fetchall():
            p = row_to_dict(r)
            eff = _compute_project_effective_updated_at(conn, p, has_wt=has_wt)
            if eff and eff != p["updated_at"]:
                conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (eff, p["id"]))

    return {
        "backfilled_cwd_count": backfilled_cwd_count,
        "repaired_conversations": repaired_conversations,
        "repaired_contents": repaired_contents,
        "repaired_tasks": repaired_tasks,
        "archived_blackholes": archived_blackholes,
    }
