"""Task-bound short-term working memory service (`working_tasks` + `working_task_events`).

Separates mutable in-flight task state, temporary constraints, and discussion-only plans
from governed long-term `contents` memories.
"""
from __future__ import annotations

from contextlib import nullcontext
import re
import sqlite3
from typing import Any

from fastapi import HTTPException

from app.database import db, decode, encode, log_audit, new_id, now_iso
from app.services import governance_guardrails, telemetry_service

TASK_STATUSES = ("active", "in_progress", "blocked", "paused", "completed", "cancelled")
ACTIVE_TASK_STATUSES = ("active", "in_progress", "blocked")
INACTIVE_TASK_STATUSES = ("paused", "completed", "cancelled")
EXECUTION_MODES = ("discussion_only", "approved")

_TASK_PAGE_ORDER = """CASE status WHEN 'in_progress' THEN 0 WHEN 'active' THEN 1
    WHEN 'blocked' THEN 2 WHEN 'paused' THEN 3 WHEN 'completed' THEN 4 ELSE 5 END,
    updated_at DESC, id DESC"""

MAX_TITLE_CHARS = 200
MAX_GOAL_CHARS = 600
MAX_CURRENT_STATE_CHARS = 600
MAX_ITEM_CHARS = 400
MAX_STEP_ITEMS = 30
MAX_CONSTRAINT_ITEMS = 20
MAX_FAILED_ITEMS = 20
MAX_BLOCKER_ITEMS = 20
MAX_NEXT_STEP_ITEMS = 20

_TASK_TITLE_NORM_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_TASK_PREFIX_RE = re.compile(
    r"^(?:task|todo|wip|当前任务|工作任务|推进任务|待办)[:：\s\-]+",
    re.IGNORECASE,
)

_ENGINEERING_ACTION_RE = re.compile(
    r"(?:实现|修复|重构|改造|新增|添加|接入|迁移|优化|排查|定位|调试|编写|开发|升级|设计|方案|评审|联调|补齐|完善|拆分|合并|删除|清理|测试|"
    r"\b(?:implement|fix|refactor|build|create|add|update|migrate|optimize|debug|investigate|design|architect|integrate|remove|write|wire|patch|resolve)\b)",
    re.IGNORECASE,
)

_TRIVIAL_QA_RE = re.compile(
    r"(?:"
    r"^(?:现在|当前|目前)?(?:有几个|有多少个?|在哪个|是什么|有哪些)(?:分支|目录|文件|提交|版本|端口|环境变量)[？?\s]*$"
    r"|"
    r"^(?:查看|看看|告诉我)(?:当前|现在)?(?:分支|目录|状态|时间|版本)[？?\s]*$"
    r"|"
    r"^(?:what|which)\s+(?:branch|directory|folder|commit|version|node\s+version|python\s+version)\b.*[?？]?$"
    r"|"
    r"^how\s+many\s+(?:branches|commits|files)\b.*[?？]?$"
    r"|"
    r"^(?:what\s+is|what\s+does|explain|define)\s+[a-z0-9_\-\s]{1,40}[?？]?$"
    r"|"
    r"^[\w\s\-]{1,25}(?:是什么意思|是什么|怎么读|怎么拼)[？?\s]*$"
    r")",
    re.IGNORECASE,
)

_DISCUSSION_ONLY_CUE_RE = re.compile(
    r"(?:仅讨论|只讨论|先讨论|讨论方案|尚未授权改代码|不要改代码|先别改代码|"
    r"\b(?:discussion\s+only|brainstorm\s+only|do\s+not\s+edit\s+code\s+yet|plan\s+only)\b)",
    re.IGNORECASE,
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS working_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active', 'in_progress', 'blocked', 'paused', 'completed', 'cancelled')),
            execution_mode TEXT NOT NULL DEFAULT 'approved'
                CHECK(execution_mode IN ('discussion_only', 'approved')),
            goal TEXT NOT NULL DEFAULT '',
            completed_steps TEXT NOT NULL DEFAULT '[]',
            current_state TEXT NOT NULL DEFAULT '',
            temp_constraints TEXT NOT NULL DEFAULT '[]',
            failed_attempts TEXT NOT NULL DEFAULT '[]',
            blockers TEXT NOT NULL DEFAULT '[]',
            next_steps TEXT NOT NULL DEFAULT '[]',
            cwd TEXT,
            git_branch TEXT,
            last_agent TEXT,
            source_conversation_id TEXT,
            legacy_content_id TEXT UNIQUE,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_working_tasks_project_status
        ON working_tasks(project_id, status, updated_at DESC, id DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_working_tasks_project_norm_title
        ON working_tasks(project_id, normalized_title)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_working_tasks_project_branch
        ON working_tasks(project_id, git_branch, status)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS working_task_events (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES working_tasks(id) ON DELETE CASCADE,
            version INTEGER NOT NULL,
            actor TEXT NOT NULL DEFAULT 'user',
            event_type TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_working_task_events_task
        ON working_task_events(task_id, version ASC, created_at ASC)
        """
    )


def normalize_task_title(title: str) -> str:
    stripped = _TASK_PREFIX_RE.sub("", (title or "").strip()).lower()
    return _TASK_TITLE_NORM_RE.sub("", stripped)


def is_trivial_qa(text: str | None) -> bool:
    """Return True when input is a one-off read/status/concept question rather than a task."""
    clean = (text or "").strip()
    if not clean:
        return True
    if len(clean) > 120:
        return False
    if _ENGINEERING_ACTION_RE.search(clean):
        return False
    if _TRIVIAL_QA_RE.search(clean):
        return True
    return False


def _bound_str(value: str | None, max_chars: int) -> str:
    return (value or "").strip()[:max_chars]


def _normalize_str_list(items: list[Any] | None, max_items: int) -> list[str]:
    if not items:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in items:
        text = _bound_str(str(raw if raw is not None else ""), MAX_ITEM_CHARS)
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result[-max_items:]


def _normalize_completed_step(item: dict[str, Any] | str, default_agent: str | None, now: str) -> dict[str, Any] | None:
    if isinstance(item, str):
        step = _bound_str(item, MAX_ITEM_CHARS)
        if not step:
            return None
        return {"step": step, "evidence": "", "agent": default_agent, "at": now}
    if not isinstance(item, dict):
        return None
    step = _bound_str(str(item.get("step") or item.get("title") or item.get("summary") or ""), MAX_ITEM_CHARS)
    if not step:
        return None
    evidence = _bound_str(str(item.get("evidence") or item.get("evidence_ref") or ""), MAX_ITEM_CHARS)
    agent = str(item.get("agent") or default_agent or "").strip() or None
    at = str(item.get("at") or now).strip() or now
    return {"step": step, "evidence": evidence, "agent": agent, "at": at}


def _normalize_completed_steps(
    items: list[dict[str, Any] | str] | None,
    default_agent: str | None,
    now: str,
) -> list[dict[str, Any]]:
    if not items:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        norm = _normalize_completed_step(raw, default_agent, now)
        if not norm:
            continue
        key = norm["step"].lower()
        if key in seen:
            # Replace earlier entry with newer evidence if provided
            result = [
                (norm if existing["step"].lower() == key and norm.get("evidence") else existing)
                for existing in result
            ]
        else:
            seen.add(key)
            result.append(norm)
    return result[-MAX_STEP_ITEMS:]


def _normalize_failed_attempt(item: dict[str, Any] | str, default_agent: str | None, now: str) -> dict[str, Any] | None:
    if isinstance(item, str):
        attempt = _bound_str(item, MAX_ITEM_CHARS)
        if not attempt:
            return None
        return {"attempt": attempt, "reason": "", "agent": default_agent, "at": now}
    if not isinstance(item, dict):
        return None
    attempt = _bound_str(str(item.get("attempt") or item.get("hypothesis") or ""), MAX_ITEM_CHARS)
    if not attempt:
        return None
    reason = _bound_str(str(item.get("reason") or item.get("failure_reason") or ""), MAX_ITEM_CHARS)
    agent = str(item.get("agent") or default_agent or "").strip() or None
    at = str(item.get("at") or now).strip() or now
    return {"attempt": attempt, "reason": reason, "agent": agent, "at": at}


def _normalize_failed_attempts(
    items: list[dict[str, Any] | str] | None,
    default_agent: str | None,
    now: str,
) -> list[dict[str, Any]]:
    if not items:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in items:
        norm = _normalize_failed_attempt(raw, default_agent, now)
        if not norm:
            continue
        key = norm["attempt"].lower()
        if key not in seen:
            seen.add(key)
            result.append(norm)
    return result[-MAX_FAILED_ITEMS:]


def _row_to_task(row: sqlite3.Row, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data = dict(row)
    execution_mode = data.get("execution_mode") if data.get("execution_mode") in EXECUTION_MODES else "approved"
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "title": data["title"],
        "status": data["status"],
        "execution_mode": execution_mode,
        "must_not_execute": execution_mode == "discussion_only",
        "goal": data.get("goal") or "",
        "completed_steps": decode(data.get("completed_steps"), []),
        "current_state": data.get("current_state") or "",
        "temp_constraints": decode(data.get("temp_constraints"), []),
        "failed_attempts": decode(data.get("failed_attempts"), []),
        "blockers": decode(data.get("blockers"), []),
        "next_steps": decode(data.get("next_steps"), []),
        "cwd": data.get("cwd"),
        "git_branch": data.get("git_branch"),
        "last_agent": data.get("last_agent"),
        "source_conversation_id": data.get("source_conversation_id"),
        "legacy_content_id": data.get("legacy_content_id"),
        "version": int(data.get("version") or 1),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "completed_at": data.get("completed_at"),
        "events": events if events is not None else [],
    }


def _load_events_map(
    conn: sqlite3.Connection,
    task_ids: list[str],
    *,
    max_per_task: int = 30,
) -> dict[str, list[dict[str, Any]]]:
    if not task_ids:
        return {}
    events_by_task: dict[str, list[dict[str, Any]]] = {tid: [] for tid in task_ids}
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT id, task_id, version, actor, event_type, summary, created_at
        FROM working_task_events
        WHERE task_id IN ({placeholders})
        ORDER BY version ASC, created_at ASC, id ASC
        """,
        tuple(task_ids),
    ).fetchall()
    for r in rows:
        tid = r["task_id"]
        if tid in events_by_task:
            events_by_task[tid].append(dict(r))
    if max_per_task > 0:
        for tid, evs in events_by_task.items():
            if len(evs) > max_per_task:
                events_by_task[tid] = evs[-max_per_task:]
    return events_by_task


def _record_task_event(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    version: int,
    actor: str,
    event_type: str,
    summary: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO working_task_events (id, task_id, version, actor, event_type, summary, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id("wte"), task_id, version, actor or "user", event_type, _bound_str(summary, 300), created_at),
    )


def _legacy_tag_status(row: sqlite3.Row) -> str | None:
    tags = decode(row["tags"], [])
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("status:"):
            raw = tag.split(":", 1)[1].strip().lower()
            if raw == "pending":
                return "active"
            if raw in TASK_STATUSES:
                return raw
    return None


def _map_legacy_task_status(row: sqlite3.Row) -> str:
    """Map a legacy `contents(category='task')` row onto a working task status.

    Retiring a legacy note (`status='outdated'`) pauses its mirrored task; only a
    `historical` row or an explicit `status:completed` tag completes it, so an
    outdated note can never complete an active/discussion-only task.
    """
    tag_status = _legacy_tag_status(row)
    if tag_status == "completed":
        return "completed"
    if row["status"] == "historical":
        return "completed"
    if row["status"] == "outdated":
        return "paused"
    if row["status"] != "active":
        return "completed"
    return tag_status or "active"


def _sync_status_to_legacy_content(
    conn: sqlite3.Connection,
    legacy_content_id: str | None,
    new_status: str,
    now: str,
) -> None:
    if not legacy_content_id:
        return
    row = conn.execute("SELECT id, tags, status FROM contents WHERE id = ?", (legacy_content_id,)).fetchone()
    if not row:
        return
    tag_status_map = {
        "active": "pending",
        "in_progress": "in_progress",
        "blocked": "blocked",
        "paused": "paused",
        "completed": "completed",
        "cancelled": "cancelled",
    }
    mapped_tag = f"status:{tag_status_map.get(new_status, 'pending')}"
    existing_tags = decode(row["tags"], [])
    base_tags = [t for t in (existing_tags if isinstance(existing_tags, list) else []) if not str(t).startswith("status:")]
    base_tags.append(mapped_tag)
    conn.execute(
        "UPDATE contents SET tags = ?, updated_at = ? WHERE id = ?",
        (encode(base_tags), now, legacy_content_id),
    )


def ensure_legacy_tasks_synced(
    project_id: str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> int:
    """Idempotently mirror legacy `contents(category='task')` rows into `working_tasks`."""
    from app.services import project_service

    synced = 0
    with nullcontext(conn) if conn is not None else db() as active_conn:
        ensure_schema(active_conn)
        if project_id:
            rows = active_conn.execute(
                "SELECT * FROM contents WHERE project_id = ? AND category = 'task' ORDER BY created_at ASC",
                (project_id,),
            ).fetchall()
        else:
            rows = active_conn.execute(
                "SELECT * FROM contents WHERE project_id IS NOT NULL AND category = 'task' ORDER BY created_at ASC"
            ).fetchall()

        for r in rows:
            legacy_id = r["id"]
            tomb = active_conn.execute(
                "SELECT 1 FROM tombstones WHERE entity_type = 'memory' AND dedupe_key = ? LIMIT 1",
                (f"legacy_task:{legacy_id}",),
            ).fetchone()
            if tomb:
                continue

            mapped_status = _map_legacy_task_status(r)
            existing = active_conn.execute(
                "SELECT * FROM working_tasks WHERE legacy_content_id = ?",
                (legacy_id,),
            ).fetchone()

            if not existing:
                tid = new_id("tsk")
                title = _bound_str(r["title"] or "Untitled Task", MAX_TITLE_CHARS)
                norm_title = normalize_task_title(title) or legacy_id
                body = _bound_str(r["content"] or "", MAX_GOAL_CHARS)
                created_at = r["created_at"] or now_iso()
                updated_at = r["updated_at"] or created_at
                completed_at = updated_at if mapped_status == "completed" else None
                actor = r["agent"] or "legacy_sync"
                active_conn.execute(
                    """
                    INSERT INTO working_tasks (
                        id, project_id, title, normalized_title, status, execution_mode,
                        goal, completed_steps, current_state, temp_constraints,
                        failed_attempts, blockers, next_steps, cwd, git_branch,
                        last_agent, source_conversation_id, legacy_content_id,
                        version, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, 'approved', ?, '[]', ?, '[]', '[]', '[]', '[]', NULL, NULL, ?, NULL, ?, 1, ?, ?, ?)
                    """,
                    (
                        tid,
                        r["project_id"],
                        title,
                        norm_title,
                        mapped_status,
                        body or title,
                        body,
                        r["agent"],
                        legacy_id,
                        created_at,
                        updated_at,
                        completed_at,
                    ),
                )
                _record_task_event(
                    active_conn,
                    task_id=tid,
                    version=1,
                    actor=actor,
                    event_type="legacy_synced",
                    summary=f"Mirrored legacy task '{title}' ({mapped_status})",
                    created_at=created_at,
                )
                synced += 1
            elif (
                project_service._ts_epoch_ms(r["updated_at"])
                > project_service._ts_epoch_ms(existing["updated_at"])
            ):
                if (
                    r["status"] == "outdated"
                    and existing["execution_mode"] == "discussion_only"
                    and existing["status"] in ACTIVE_TASK_STATUSES
                ):
                    # Retiring the legacy note must never drop an active
                    # discussion-only execution gate.
                    mapped_status = existing["status"]
                new_ver = int(existing["version"] or 1) + 1
                title = _bound_str(r["title"] or existing["title"], MAX_TITLE_CHARS)
                norm_title = normalize_task_title(title) or legacy_id
                body = _bound_str(r["content"] or "", MAX_CURRENT_STATE_CHARS)
                updated_at = r["updated_at"]
                completed_at = updated_at if mapped_status == "completed" else (
                    existing["completed_at"] if mapped_status in INACTIVE_TASK_STATUSES else None
                )
                active_conn.execute(
                    """
                    UPDATE working_tasks
                    SET title = ?, normalized_title = ?, status = ?, current_state = ?,
                        last_agent = COALESCE(?, last_agent), version = ?, updated_at = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        norm_title,
                        mapped_status,
                        body or existing["current_state"],
                        r["agent"],
                        new_ver,
                        updated_at,
                        completed_at,
                        existing["id"],
                    ),
                )
                _record_task_event(
                    active_conn,
                    task_id=existing["id"],
                    version=new_ver,
                    actor=r["agent"] or "legacy_sync",
                    event_type="legacy_updated",
                    summary=f"Synced legacy task update ({mapped_status})",
                    created_at=updated_at,
                )
                synced += 1
    return synced


def _resolve_project_and_branch(
    project_id: str | None,
    cwd: str | None,
    git_branch: str | None,
    *,
    auto_create_project: bool = True,
) -> tuple[str | None, str | None]:
    from app.services import project_service

    resolved_pid = (project_id or "").strip() or None
    resolved_branch = (git_branch or "").strip() or None

    if cwd:
        checkout = project_service.read_git_checkout(cwd)
        if resolved_branch is None and isinstance(checkout, dict) and checkout.get("branch"):
            resolved_branch = str(checkout["branch"])
        if resolved_pid is None:
            if auto_create_project:
                resolved_pid = project_service.reconstruct_or_resolve_project(cwd)
            else:
                matched, ambiguous = project_service.resolve_project_by_path(cwd)
                if matched and not ambiguous:
                    resolved_pid = matched["id"]
    return resolved_pid, resolved_branch


def list_working_tasks(
    project_id: str | None = None,
    *,
    active_only: bool = False,
    status: str | None = None,
    git_branch: str | None = None,
    cwd: str | None = None,
    include_events: bool = True,
    limit: int = 100,
    offset: int = 0,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), 200))
    bounded_offset = max(0, int(offset))
    resolved_pid, _ = _resolve_project_and_branch(project_id, cwd, git_branch, auto_create_project=False)

    if status and status not in ("all", "history") and status not in TASK_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid working task status '{status}'")

    with nullcontext(conn) if conn is not None else db() as active_conn:
        ensure_schema(active_conn)
        ensure_legacy_tasks_synced(resolved_pid, conn=active_conn)

        clauses: list[str] = []
        params: list[Any] = []
        if resolved_pid:
            clauses.append("project_id = ?")
            params.append(resolved_pid)
        if active_only:
            clauses.append("status IN ('active', 'in_progress', 'blocked')")
        elif status == "history":
            clauses.append("status IN ('paused', 'completed', 'cancelled')")
        elif status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if git_branch:
            clauses.append("git_branch = ?")
            params.append(git_branch)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = active_conn.execute(
            f"""
            SELECT * FROM working_tasks
            {where}
            ORDER BY {_TASK_PAGE_ORDER}
            LIMIT ? OFFSET ?
            """,
            (*params, bounded_limit, bounded_offset),
        ).fetchall()

        task_ids = [r["id"] for r in rows]
        events_map = _load_events_map(active_conn, task_ids) if include_events else {}
        return [_row_to_task(r, events_map.get(r["id"], [])) for r in rows]

def locate_working_task_page(
    task_id: str, *, project_id: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """Locate a deep-linked task in the same ordering used by paged lists."""
    bounded_limit = max(1, min(int(limit), 200))
    with db() as conn:
        ensure_schema(conn)
        ensure_legacy_tasks_synced(project_id, conn=conn)
        task = conn.execute("SELECT project_id,status FROM working_tasks WHERE id=?", (task_id,)).fetchone()
        if task is None or (project_id and task["project_id"] != project_id):
            raise HTTPException(404, "Working task not found in this project")
        history = task["status"] in INACTIVE_TASK_STATUSES
        states = INACTIVE_TASK_STATUSES if history else ACTIVE_TASK_STATUSES
        placeholders = ",".join("?" for _ in states)
        clause = " AND project_id=?" if project_id else ""
        params = (*states, project_id) if project_id else states
        row = conn.execute(
            f"""WITH ranked AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY {_TASK_PAGE_ORDER}) - 1 AS position
                FROM working_tasks WHERE status IN ({placeholders}){clause}
            ) SELECT position FROM ranked WHERE id=?""",
            (*params, task_id),
        ).fetchone()
        if row is None:
            raise HTTPException(409, "Working task changed while locating its page")
        return {"task_id": task_id, "group": "history" if history else "active",
                "offset": (row["position"] // bounded_limit) * bounded_limit, "limit": bounded_limit}



def summarize_working_tasks(
    project_id: str | None = None,
    *,
    cwd: str | None = None,
    git_branch: str | None = None,
) -> dict[str, int]:
    """Server-side task counts so clients never infer totals from a truncated page."""
    resolved_pid, _ = _resolve_project_and_branch(project_id, cwd, git_branch, auto_create_project=False)
    with db() as active_conn:
        ensure_schema(active_conn)
        ensure_legacy_tasks_synced(resolved_pid, conn=active_conn)

        clauses: list[str] = []
        params: list[Any] = []
        if resolved_pid:
            clauses.append("project_id = ?")
            params.append(resolved_pid)
        if git_branch:
            clauses.append("git_branch = ?")
            params.append(git_branch)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        row = active_conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('active', 'in_progress', 'blocked') THEN 1 ELSE 0 END) AS active_count,
                   SUM(CASE WHEN status IN ('paused', 'completed', 'cancelled') THEN 1 ELSE 0 END) AS history_count
            FROM working_tasks
            {where}
            """,
            tuple(params),
        ).fetchone()
        total = int(row["total"] or 0) if row is not None else 0
        active_count = int(row["active_count"] or 0) if row is not None else 0
        history_count = int(row["history_count"] or 0) if row is not None else 0
        return {"active": active_count, "history": history_count, "all": total}


def get_working_task(
    task_id: str,
    *,
    include_events: bool = True,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    with nullcontext(conn) if conn is not None else db() as active_conn:
        ensure_schema(active_conn)
        row = active_conn.execute("SELECT * FROM working_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Working task '{task_id}' not found")
        events_map = _load_events_map(active_conn, [task_id], max_per_task=100) if include_events else {}
        return _row_to_task(row, events_map.get(task_id, []))


def upsert_working_task(
    *,
    title: str,
    project_id: str | None = None,
    task_id: str | None = None,
    cwd: str | None = None,
    git_branch: str | None = None,
    status: str | None = None,
    execution_mode: str | None = None,
    goal: str | None = None,
    completed_steps: list[dict[str, Any] | str] | None = None,
    add_completed_step: dict[str, Any] | str | None = None,
    current_state: str | None = None,
    temp_constraints: list[str] | None = None,
    add_temp_constraint: str | None = None,
    failed_attempts: list[dict[str, Any] | str] | None = None,
    add_failed_attempt: dict[str, Any] | str | None = None,
    blockers: list[str] | None = None,
    add_blocker: str | None = None,
    next_steps: list[str] | None = None,
    agent: str | None = None,
    last_agent: str | None = None,
    source_conversation_id: str | None = None,
    cited_memory_ids: list[str] | None = None,
    citation_note: str | None = None,
    continue_branch_task: bool = False,
    expected_version: int | None = None,
    allow_skip_trivial: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Create or converge onto an existing active working task with 6 bounded slots & CAS."""
    clean_title = _bound_str(title, MAX_TITLE_CHARS)
    if not clean_title:
        raise HTTPException(status_code=422, detail="Task title cannot be empty")

    if status is not None and status not in TASK_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid task status '{status}'")
    if execution_mode is not None and execution_mode not in EXECUTION_MODES:
        raise HTTPException(status_code=422, detail=f"Invalid execution_mode '{execution_mode}'")

    actor = (agent or last_agent or "").strip() or None
    resolved_pid, resolved_branch = _resolve_project_and_branch(project_id, cwd, git_branch)
    now = now_iso()

    with nullcontext(conn) if conn is not None else db() as active_conn:
        if not active_conn.in_transaction:
            active_conn.execute("BEGIN IMMEDIATE")
        ensure_schema(active_conn)

        existing: sqlite3.Row | None = None
        explicit_target = False

        if task_id:
            explicit_target = True
            existing = active_conn.execute(
                "SELECT * FROM working_tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail=f"Working task '{task_id}' not found")
            resolved_pid = existing["project_id"]
        else:
            if not resolved_pid:
                raise HTTPException(status_code=422, detail="project_id or resolvable cwd is required")
            proj_row = active_conn.execute(
                "SELECT id FROM projects WHERE id = ?", (resolved_pid,)
            ).fetchone()
            if not proj_row:
                raise HTTPException(status_code=404, detail=f"Project '{resolved_pid}' not found")

            ensure_legacy_tasks_synced(resolved_pid, conn=active_conn)
            norm_title = normalize_task_title(clean_title)
            if norm_title:
                existing = active_conn.execute(
                    """
                    SELECT * FROM working_tasks
                    WHERE project_id = ? AND normalized_title = ?
                      AND status IN ('active', 'in_progress', 'blocked')
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (resolved_pid, norm_title),
                ).fetchone()
            if existing is None and continue_branch_task and resolved_branch:
                branch_tasks = active_conn.execute(
                    """
                    SELECT * FROM working_tasks
                    WHERE project_id = ? AND git_branch = ?
                      AND status IN ('active', 'in_progress', 'blocked')
                    ORDER BY updated_at DESC
                    """,
                    (resolved_pid, resolved_branch),
                ).fetchall()
                if len(branch_tasks) == 1:
                    existing = branch_tasks[0]

        if existing is None:
            has_substantive_slots = any(
                bool(x)
                for x in (
                    completed_steps,
                    add_completed_step,
                    temp_constraints,
                    add_temp_constraint,
                    failed_attempts,
                    add_failed_attempt,
                    blockers,
                    add_blocker,
                    next_steps,
                )
            )
            if not has_substantive_slots and is_trivial_qa(clean_title) and (not goal or is_trivial_qa(goal)):
                if allow_skip_trivial:
                    return {
                        "ok": True,
                        "skipped": True,
                        "action": "skipped_trivial_qa",
                        "reason": "trivial_qa",
                    }
                raise HTTPException(
                    status_code=422,
                    detail="Trivial one-off Q&A does not create a persistent working task",
                )

            tid = new_id("tsk")
            norm_title = normalize_task_title(clean_title) or tid
            eff_status = status or "active"
            if execution_mode is not None:
                eff_mode = execution_mode
            elif _DISCUSSION_ONLY_CUE_RE.search(f"{clean_title} {goal or ''} {current_state or ''}"):
                eff_mode = "discussion_only"
            else:
                eff_mode = "approved"

            eff_goal = _bound_str(goal if goal is not None else clean_title, MAX_GOAL_CHARS)
            eff_state = _bound_str(
                current_state
                if current_state is not None
                else ("正在讨论方案，尚未授权改代码" if eff_mode == "discussion_only" else ""),
                MAX_CURRENT_STATE_CHARS,
            )

            steps_list = _normalize_completed_steps(completed_steps, actor, now)
            if add_completed_step is not None:
                steps_list = _normalize_completed_steps([*steps_list, add_completed_step], actor, now)

            constraints_list = _normalize_str_list(
                [*(temp_constraints or []), *([add_temp_constraint] if add_temp_constraint else [])],
                MAX_CONSTRAINT_ITEMS,
            )
            failed_list = _normalize_failed_attempts(failed_attempts, actor, now)
            if add_failed_attempt is not None:
                failed_list = _normalize_failed_attempts([*failed_list, add_failed_attempt], actor, now)

            blockers_list = _normalize_str_list(
                [*(blockers or []), *([add_blocker] if add_blocker else [])],
                MAX_BLOCKER_ITEMS,
            )
            next_list = _normalize_str_list(next_steps, MAX_NEXT_STEP_ITEMS)
            completed_at = now if eff_status == "completed" else None

            active_conn.execute(
                """
                INSERT INTO working_tasks (
                    id, project_id, title, normalized_title, status, execution_mode,
                    goal, completed_steps, current_state, temp_constraints,
                    failed_attempts, blockers, next_steps, cwd, git_branch,
                    last_agent, source_conversation_id, legacy_content_id,
                    version, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?, ?)
                """,
                (
                    tid,
                    resolved_pid,
                    clean_title,
                    norm_title,
                    eff_status,
                    eff_mode,
                    eff_goal,
                    encode(steps_list),
                    eff_state,
                    encode(constraints_list),
                    encode(failed_list),
                    encode(blockers_list),
                    encode(next_list),
                    cwd,
                    resolved_branch,
                    actor,
                    source_conversation_id,
                    now,
                    now,
                    completed_at,
                ),
            )
            _record_task_event(
                active_conn,
                task_id=tid,
                version=1,
                actor=actor or "user",
                event_type="created",
                summary=f"Created task '{clean_title}' ({eff_status}, {eff_mode})",
                created_at=now,
            )
            if cited_memory_ids:
                telemetry_service.record_cited(
                    cited_memory_ids,
                    agent=actor or "user",
                    project_id=resolved_pid,
                    task_id=tid,
                    surface="working_task",
                    note=citation_note or eff_state or clean_title,
                    conn=active_conn,
                )
            from app.services import project_service
            project_service.touch_project_activity(active_conn, resolved_pid, now)
            log_audit(actor=actor or "user", action="working_task.create", target=tid, conn=active_conn)
            return get_working_task(tid, conn=active_conn)

        # Converging / updating an existing task
        cur_version = int(existing["version"] or 1)
        if expected_version is not None and cur_version != expected_version:
            raise HTTPException(
                status_code=409,
                detail=f"Version conflict: current version is {cur_version}, expected {expected_version}",
            )

        new_version = cur_version + 1
        eff_title = clean_title if explicit_target else existing["title"]
        norm_title = normalize_task_title(eff_title) or existing["normalized_title"]
        eff_status = status if status is not None else existing["status"]

        # Invariant 6: preserve discussion_only across cross-agent convergence unless explicitly changed.
        if execution_mode is not None:
            eff_mode = execution_mode
        else:
            eff_mode = existing["execution_mode"]

        eff_goal = _bound_str(goal, MAX_GOAL_CHARS) if goal is not None else (existing["goal"] or "")
        eff_state = (
            _bound_str(current_state, MAX_CURRENT_STATE_CHARS)
            if current_state is not None
            else (existing["current_state"] or "")
        )

        cur_steps = decode(existing["completed_steps"], [])
        if explicit_target and completed_steps is not None:
            merged_steps = _normalize_completed_steps(completed_steps, actor, now)
        elif completed_steps is not None:
            merged_steps = _normalize_completed_steps([*cur_steps, *completed_steps], actor, now)
        else:
            merged_steps = cur_steps
        if add_completed_step is not None:
            merged_steps = _normalize_completed_steps([*merged_steps, add_completed_step], actor, now)

        cur_constraints = decode(existing["temp_constraints"], [])
        if explicit_target and temp_constraints is not None:
            merged_constraints = _normalize_str_list(temp_constraints, MAX_CONSTRAINT_ITEMS)
        elif temp_constraints is not None:
            merged_constraints = _normalize_str_list([*cur_constraints, *temp_constraints], MAX_CONSTRAINT_ITEMS)
        else:
            merged_constraints = cur_constraints
        if add_temp_constraint:
            merged_constraints = _normalize_str_list([*merged_constraints, add_temp_constraint], MAX_CONSTRAINT_ITEMS)

        cur_failed = decode(existing["failed_attempts"], [])
        if explicit_target and failed_attempts is not None:
            merged_failed = _normalize_failed_attempts(failed_attempts, actor, now)
        elif failed_attempts is not None:
            merged_failed = _normalize_failed_attempts([*cur_failed, *failed_attempts], actor, now)
        else:
            merged_failed = cur_failed
        if add_failed_attempt is not None:
            merged_failed = _normalize_failed_attempts([*merged_failed, add_failed_attempt], actor, now)

        cur_blockers = decode(existing["blockers"], [])
        merged_blockers = (
            _normalize_str_list(blockers, MAX_BLOCKER_ITEMS) if blockers is not None else list(cur_blockers)
        )
        if add_blocker:
            merged_blockers = _normalize_str_list([*merged_blockers, add_blocker], MAX_BLOCKER_ITEMS)

        cur_next = decode(existing["next_steps"], [])
        merged_next = _normalize_str_list(next_steps, MAX_NEXT_STEP_ITEMS) if next_steps is not None else list(cur_next)

        eff_cwd = cwd if cwd is not None else existing["cwd"]
        eff_branch = resolved_branch if resolved_branch is not None else existing["git_branch"]
        eff_agent = actor if actor is not None else existing["last_agent"]
        eff_conv = source_conversation_id if source_conversation_id is not None else existing["source_conversation_id"]
        if eff_status == "completed":
            completed_at = existing["completed_at"] or now
        elif eff_status in ACTIVE_TASK_STATUSES:
            completed_at = None
        else:
            completed_at = existing["completed_at"]

        updated = active_conn.execute(
            """
            UPDATE working_tasks SET
                title = ?, normalized_title = ?, status = ?, execution_mode = ?,
                goal = ?, completed_steps = ?, current_state = ?, temp_constraints = ?,
                failed_attempts = ?, blockers = ?, next_steps = ?, cwd = ?, git_branch = ?,
                last_agent = ?, source_conversation_id = ?, version = ?,
                updated_at = ?, completed_at = ?
            WHERE id = ? AND version = ?
            """,
            (
                eff_title,
                norm_title,
                eff_status,
                eff_mode,
                eff_goal,
                encode(merged_steps),
                eff_state,
                encode(merged_constraints),
                encode(merged_failed),
                encode(merged_blockers),
                encode(merged_next),
                eff_cwd,
                eff_branch,
                eff_agent,
                eff_conv,
                new_version,
                now,
                completed_at,
                existing["id"],
                cur_version,
            ),
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=409, detail="Concurrent update conflict on working task")

        if status is not None and status != existing["status"]:
            _sync_status_to_legacy_content(active_conn, existing["legacy_content_id"], eff_status, now)

        ev_type = "converged" if not explicit_target else "updated"
        ev_summary = eff_state or f"Updated task ({eff_status}, {eff_mode})"
        _record_task_event(
            active_conn,
            task_id=existing["id"],
            version=new_version,
            actor=actor or "user",
            event_type=ev_type,
            summary=ev_summary,
            created_at=now,
        )
        if cited_memory_ids:
            telemetry_service.record_cited(
                cited_memory_ids,
                agent=actor or "user",
                project_id=existing["project_id"],
                task_id=existing["id"],
                surface="working_task",
                note=citation_note or eff_state or eff_title,
                conn=active_conn,
            )
        from app.services import project_service
        project_service.touch_project_activity(active_conn, existing["project_id"], now)
        log_audit(actor=actor or "user", action=f"working_task.{ev_type}", target=existing["id"], conn=active_conn)
        return get_working_task(existing["id"], conn=active_conn)


def patch_working_task(
    task_id: str,
    *,
    title: str | None = None,
    status: str | None = None,
    execution_mode: str | None = None,
    goal: str | None = None,
    completed_steps: list[dict[str, Any] | str] | None = None,
    add_completed_step: dict[str, Any] | str | None = None,
    current_state: str | None = None,
    temp_constraints: list[str] | None = None,
    add_temp_constraint: str | None = None,
    failed_attempts: list[dict[str, Any] | str] | None = None,
    add_failed_attempt: dict[str, Any] | str | None = None,
    blockers: list[str] | None = None,
    add_blocker: str | None = None,
    next_steps: list[str] | None = None,
    cwd: str | None = None,
    git_branch: str | None = None,
    agent: str | None = None,
    last_agent: str | None = None,
    source_conversation_id: str | None = None,
    cited_memory_ids: list[str] | None = None,
    citation_note: str | None = None,
    summary: str | None = None,
    expected_version: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Explicit slot/status/mode patch on a known working task ID."""
    with nullcontext(conn) if conn is not None else db() as active_conn:
        ensure_schema(active_conn)
        current = active_conn.execute("SELECT title FROM working_tasks WHERE id = ?", (task_id,)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail=f"Working task '{task_id}' not found")
        eff_title = title.strip() if title is not None else current["title"]
        result = upsert_working_task(
            task_id=task_id,
            title=eff_title,
            status=status,
            execution_mode=execution_mode,
            goal=goal,
            completed_steps=completed_steps,
            add_completed_step=add_completed_step,
            current_state=current_state if current_state is not None else summary,
            temp_constraints=temp_constraints,
            add_temp_constraint=add_temp_constraint,
            failed_attempts=failed_attempts,
            add_failed_attempt=add_failed_attempt,
            blockers=blockers,
            add_blocker=add_blocker,
            next_steps=next_steps,
            cwd=cwd,
            git_branch=git_branch,
            agent=agent,
            last_agent=last_agent,
            source_conversation_id=source_conversation_id,
            cited_memory_ids=cited_memory_ids,
            citation_note=citation_note,
            expected_version=expected_version,
            conn=active_conn,
        )
        return result


def transition_working_task(
    task_id: str,
    status: str | None = None,
    *,
    execution_mode: str | None = None,
    current_state: str | None = None,
    summary: str | None = None,
    actor: str = "user",
    cited_memory_ids: list[str] | None = None,
    citation_note: str | None = None,
    expected_version: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Transition a working task's lifecycle status and/or execution_mode."""
    if status is None and execution_mode is None:
        raise HTTPException(status_code=422, detail="status or execution_mode is required for transition")
    return patch_working_task(
        task_id,
        status=status,
        execution_mode=execution_mode,
        current_state=current_state,
        summary=summary,
        agent=actor,
        cited_memory_ids=cited_memory_ids,
        citation_note=citation_note,
        expected_version=expected_version,
        conn=conn,
    )


def add_temp_constraint_to_active_task(
    project_id: str,
    constraint: str,
    *,
    cwd: str | None = None,
    title_hint: str | None = None,
    agent: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Attach a temporary constraint (`这次先别...`) to the current active task without touching long-term memory."""
    clean_constraint = _bound_str(constraint, MAX_ITEM_CHARS)
    if not clean_constraint:
        raise HTTPException(status_code=422, detail="Constraint cannot be empty")
    _, resolved_branch = _resolve_project_and_branch(project_id, cwd, None, auto_create_project=False)

    with nullcontext(conn) if conn is not None else db() as active_conn:
        ensure_schema(active_conn)
        ensure_legacy_tasks_synced(project_id, conn=active_conn)
        target_row = None
        if resolved_branch:
            target_row = active_conn.execute(
                """
                SELECT * FROM working_tasks
                WHERE project_id = ? AND git_branch = ?
                  AND status IN ('active', 'in_progress', 'blocked')
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (project_id, resolved_branch),
            ).fetchone()
        if target_row is None:
            target_row = active_conn.execute(
                """
                SELECT * FROM working_tasks
                WHERE project_id = ? AND status IN ('active', 'in_progress', 'blocked')
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        if target_row is not None:
            return patch_working_task(
                target_row["id"],
                add_temp_constraint=clean_constraint,
                agent=agent,
                conn=active_conn,
            )
        fallback_title = _bound_str(title_hint or "当前会话任务", MAX_TITLE_CHARS)
        if is_trivial_qa(fallback_title):
            fallback_title = "当前会话任务"
        return upsert_working_task(
            project_id=project_id,
            cwd=cwd,
            git_branch=resolved_branch,
            title=fallback_title,
            goal=fallback_title,
            temp_constraints=[clean_constraint],
            agent=agent,
            conn=active_conn,
        )


def delete_working_task(
    task_id: str,
    *,
    actor: str = "user",
    expected_version: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, bool]:
    with nullcontext(conn) if conn is not None else db() as active_conn:
        if not active_conn.in_transaction:
            active_conn.execute("BEGIN IMMEDIATE")
        ensure_schema(active_conn)
        row = active_conn.execute("SELECT * FROM working_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Working task '{task_id}' not found")
        if expected_version is not None and int(row["version"] or 1) != expected_version:
            raise HTTPException(
                status_code=409,
                detail=f"Version conflict: current version is {row['version']}, expected {expected_version}",
            )
        now = now_iso()
        if row["legacy_content_id"]:
            active_conn.execute(
                """
                INSERT OR IGNORE INTO tombstones
                    (id, entity_type, dedupe_key, scope_project_id, deleted_at, deleted_by, reason)
                VALUES (?, 'memory', ?, ?, ?, ?, 'working_task_deleted')
                """,
                (new_id("tomb"), f"legacy_task:{row['legacy_content_id']}", row["project_id"], now, actor),
            )
        active_conn.execute("DELETE FROM working_task_events WHERE task_id = ?", (task_id,))
        active_conn.execute("DELETE FROM working_tasks WHERE id = ?", (task_id,))
        log_audit(actor=actor or "user", action="working_task.delete", target=task_id, conn=active_conn)
        return {"ok": True}


_EVIDENCE_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
        "been", "have", "has", "had", "not", "you", "your", "but", "can", "will",
        "would", "should", "could", "into", "then", "than", "they", "them", "there",
    }
)


def _evidence_message_contents(conv_messages: Any) -> list[str]:
    """Yield only decoded message bodies; never ids, roles, timestamps, or raw JSON."""
    decoded_messages = decode(conv_messages, []) if isinstance(conv_messages, str) else conv_messages
    contents: list[str] = []
    if isinstance(decoded_messages, list):
        for message in decoded_messages:
            if isinstance(message, dict):
                for key in ("content", "text"):
                    value = message.get(key)
                    if isinstance(value, str) and value.strip():
                        contents.append(value)
            elif isinstance(message, str) and message.strip():
                contents.append(message)
    elif isinstance(decoded_messages, str) and decoded_messages.strip():
        contents.append(decoded_messages)
    return contents


def _evidence_quote_found(quote: str, conv_messages: Any) -> bool:
    """True only when the quote appears in a decoded message body from the source conversation."""
    return any(quote in content for content in _evidence_message_contents(conv_messages))


def _evidence_tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens = {
        tok
        for tok in re.findall(r"[a-z0-9][a-z0-9_.-]{2,}|[0-9]{2,}", lowered)
        if tok not in _EVIDENCE_STOPWORDS
    }
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def _evidence_quote_supports_lesson(quote: str, title: str, content: str) -> bool:
    """Reject a genuine but unrelated quote: the lesson must be about the quoted content.

    Reuses the distillation admission rule for direct claim support, then requires shared
    evidence tokens (latin words / CJK bigrams) for paraphrased or derived lessons.
    """
    from app.services.distillation_service import _claim_supported_by_quote

    if _claim_supported_by_quote(title, quote) or _claim_supported_by_quote(content, quote):
        return True

    quote_tokens = _evidence_tokens(quote)
    lesson_tokens = _evidence_tokens(f"{title}\n{content}")
    shared = quote_tokens & lesson_tokens
    if not shared:
        return False
    if any(len(tok) >= 5 for tok in shared):
        return True
    return len(shared) >= 2


def _require_task_source_conversation(
    conn: sqlite3.Connection, task_row: sqlite3.Row
) -> tuple[str, Any]:
    """Require an existing same-project source conversation linked to the task."""
    conv_id = (task_row["source_conversation_id"] or "").strip()
    if not conv_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "Promoting a task conclusion requires an existing source conversation "
                "(set working task source_conversation_id first)"
            ),
        )
    conv_row = conn.execute(
        "SELECT id, project_id, messages FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if not conv_row:
        raise HTTPException(
            status_code=422,
            detail="Linked source conversation no longer exists; cannot promote without source evidence",
        )
    if conv_row["project_id"] != task_row["project_id"]:
        raise HTTPException(
            status_code=422,
            detail="Linked source conversation belongs to a different project; promotion refused",
        )
    return conv_id, conv_row["messages"]


def _validate_task_lesson_evidence(
    conv_messages: Any,
    *,
    quote: str,
    title: str,
    content: str,
) -> None:
    if not quote:
        raise HTTPException(
            status_code=422,
            detail="evidence_quote is required; promotion needs a quote found in the source conversation",
        )
    if not _evidence_quote_found(quote, conv_messages):
        raise HTTPException(
            status_code=422,
            detail="evidence_quote was not found in the linked source conversation messages",
        )
    if not _evidence_quote_supports_lesson(quote, title, content):
        raise HTTPException(
            status_code=422,
            detail="evidence_quote is unrelated to the promoted lesson and cannot certify it",
        )


def promote_task_conclusion(
    task_id: str,
    *,
    title: str,
    content: str,
    category: str = "lesson",
    applicability: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    pinned: bool = False,
    evidence_quote: str | None = None,
    actor: str = "user",
    complete_task: bool = False,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Promote a verified task conclusion into conditional long-term memory.

    Requires an existing same-project source conversation and a non-empty
    `evidence_quote` found in its decoded message bodies and semantically related
    to the promoted lesson. User promotions keep `verified_outcome`; Agent
    promotions are stamped `source_backed` because conversation evidence is not
    independent validation. The task is reconciled and updated in one transaction
    with CAS revalidation before any task/count change.
    """
    from app.services import content_service

    clean_title = (title or "").strip()
    clean_content = (content or "").strip()
    if not clean_title or not clean_content:
        raise HTTPException(status_code=422, detail="Promoted memory title and content cannot be empty")
    if category == "task":
        raise HTTPException(status_code=422, detail="Cannot promote a task conclusion into category='task'")

    norm_applicability = governance_guardrails.normalize_applicability(applicability)
    if not governance_guardrails.has_non_empty_applicability(norm_applicability):
        raise HTTPException(
            status_code=422,
            detail="Promoting a task conclusion requires non-empty applicability conditions (e.g. when, env, version_range, or paths)",
        )

    actor_key = (actor or "user").strip().lower() or "user"
    is_agent_write = actor_key not in ("user", "manual", "web")
    origin_authority = "source_backed" if is_agent_write else "verified_outcome"
    quote = (evidence_quote or "").strip()

    with nullcontext(conn) if conn is not None else db() as active_conn:
        if not active_conn.in_transaction:
            active_conn.execute("BEGIN IMMEDIATE")
        ensure_schema(active_conn)
        task_row = active_conn.execute("SELECT * FROM working_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task_row:
            raise HTTPException(status_code=404, detail=f"Working task '{task_id}' not found")
        project_id = task_row["project_id"]
        base_version = int(task_row["version"] or 1)
        base_conv_id = (task_row["source_conversation_id"] or "").strip()

        if is_agent_write and task_row["execution_mode"] == "discussion_only":
            raise HTTPException(
                status_code=422,
                detail="Discussion-only tasks cannot promote conclusions until the user authorizes execution",
            )

        # Agent promotions still pass the ordinary admission gate; `source_backed`
        # is a valid truthful origin but never a self-asserted authoritative one.
        verdict = governance_guardrails.classify_write_candidate(
            title=clean_title,
            content=clean_content,
            category=category,
            origin_authority=origin_authority,
            is_agent_write=is_agent_write,
            actor=actor,
            kind="memory",
        )
        if not verdict["allowed"]:
            raise HTTPException(
                status_code=422,
                detail=f"Task conclusion rejected by governance guardrails: {verdict['reason']}",
            )

        valid_conv_id, conv_messages = _require_task_source_conversation(active_conn, task_row)
        _validate_task_lesson_evidence(
            conv_messages,
            quote=quote,
            title=clean_title,
            content=clean_content,
        )

        reconcile_res = content_service.reconcile_long_term_memory(
            title=clean_title,
            content=clean_content,
            project_id=project_id,
            category=category,
            tags=tags,
            pinned=pinned,
            applicability=norm_applicability,
            origin_authority=origin_authority,
            agent=actor,
            actor=actor,
            is_agent_write=is_agent_write,
            source_id=valid_conv_id,
            source_kind="conversation",
            quote=quote,
            promoted_from_task_id=task_id,
            auto_route_temp_constraint=False,
            _connection=active_conn,
        )

        action = reconcile_res.get("action")
        if action == "DROP_TOMBSTONED":
            raise HTTPException(
                status_code=410, detail="Promoted memory matches a previously deleted/tombstoned memory"
            )
        if action in ("REJECTED", "ROUTE_TO_WORKING_TASK"):
            raise HTTPException(
                status_code=422,
                detail=f"Task conclusion rejected by governance guardrails: {reconcile_res.get('reason')}",
            )

        # Revalidate inside the same write transaction: the task must not have been
        # deleted, moved to another project, relinked, or version-bumped by anyone
        # else while the memory was reconciled.
        cur_row = active_conn.execute(
            "SELECT * FROM working_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not cur_row:
            raise HTTPException(status_code=409, detail="Working task was deleted during promotion; no writes committed")
        if (
            cur_row["project_id"] != project_id
            or (cur_row["source_conversation_id"] or "").strip() != base_conv_id
            or int(cur_row["version"] or 1) != base_version
        ):
            raise HTTPException(
                status_code=409,
                detail="Working task changed during promotion (project, source, or version); no writes committed",
            )
        stable_conv = active_conn.execute(
            "SELECT id, project_id, messages FROM conversations WHERE id = ?", (valid_conv_id,)
        ).fetchone()
        if (
            not stable_conv
            or stable_conv["project_id"] != project_id
            or not _evidence_quote_found(quote, stable_conv["messages"])
        ):
            raise HTTPException(
                status_code=409,
                detail="Source conversation changed during promotion; no writes committed",
            )

        if action == "OPEN_CONFLICT":
            # A conflict is not a successful promotion: keep the task untouched and
            # surface the pending conflict instead of completing it.
            return {
                "task": get_working_task(task_id, conn=active_conn),
                "memory": None,
                "action": action,
                "conflict": reconcile_res.get("conflict"),
                "promoted": False,
            }

        now = now_iso()
        new_ver = base_version + 1
        eff_status = "completed" if complete_task else cur_row["status"]
        eff_completed_at = (
            (cur_row["completed_at"] or now) if eff_status == "completed" else cur_row["completed_at"]
        )
        updated = active_conn.execute(
            """
            UPDATE working_tasks
            SET status = ?, version = ?, updated_at = ?, completed_at = ?
            WHERE id = ? AND version = ?
            """,
            (eff_status, new_ver, now, eff_completed_at, task_id, base_version),
        )
        if updated.rowcount != 1:
            raise HTTPException(
                status_code=409, detail="Concurrent update conflict on working task during promotion"
            )
        if complete_task and cur_row["status"] != "completed":
            _sync_status_to_legacy_content(active_conn, cur_row["legacy_content_id"], "completed", now)
        _record_task_event(
            active_conn,
            task_id=task_id,
            version=new_ver,
            actor=actor or "user",
            event_type="promoted",
            summary=f"Promoted conclusion '{clean_title}' ({action})",
            created_at=now,
        )
        log_audit(actor=actor or "user", action="working_task.promote", target=task_id, conn=active_conn)
        updated_task = get_working_task(task_id, conn=active_conn)

    return {
        "task": updated_task,
        "memory": reconcile_res.get("memory"),
        "action": action,
        "conflict": reconcile_res.get("conflict"),
        "promoted": True,
    }
