import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from app.config import settings
from app.event_bus import discard_pending_events, publish_pending_events, queue_event
from app.private_storage import prepare_database


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str = "") -> str:
    token = uuid4().hex[:12]
    return f"{prefix}_{token}" if prefix else token


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def decode(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


JSON_COLUMNS = {
    "tags",
    "related_ids",
    "directories",
    "messages",
    "paths",
    "capabilities",
    "proposed",
    "card",
    "source_ids",
    "source_revisions",
    "sources",
    "versions",
    "counts",
}


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in JSON_COLUMNS:
        if key in item:
            item[key] = decode(item[key], [] if key.endswith("s") or key in {"tags", "related_ids", "paths", "source_ids", "directories"} else {})
    return item


@contextmanager
def db(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or settings.db_path
    data_dir = os.environ.get('CONTEXTCORE_DATA_DIR')
    prepare_database(db_path, data_directory=Path(data_dir) if data_dir else None)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
        publish_pending_events(conn)
    except Exception:
        conn.rollback()
        discard_pending_events(conn)
        raise
    finally:
        discard_pending_events(conn)
        conn.close()


def log_audit(actor: str, action: str, target: str, result: str = "ok", conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    record = {
        "id": new_id("aud"),
        "actor": actor,
        "action": action,
        "target": target,
        "result": result,
        "created_at": now_iso(),
    }
    sql = "INSERT INTO audit_logs (id, actor, action, target, result, created_at) VALUES (?, ?, ?, ?, ?, ?)"
    params = (record["id"], record["actor"], record["action"], record["target"], record["result"], record["created_at"])
    if conn:
        conn.execute(sql, params)
        queue_event(conn, "audit.created", record)
    else:
        with db() as local_conn:
            local_conn.execute(sql, params)
            queue_event(local_conn, "audit.created", record)
    return record




def get_app_setting(key: str, default: Any = None, conn: sqlite3.Connection | None = None) -> Any:
    sql = "SELECT value FROM app_settings WHERE key = ?"
    if conn:
        row = conn.execute(sql, (key,)).fetchone()
    else:
        with db() as local_conn:
            row = local_conn.execute(sql, (key,)).fetchone()
    if not row:
        return default
    return decode(row["value"], default)


def set_app_setting(key: str, value: Any, conn: sqlite3.Connection | None = None) -> None:
    encoded = encode(value)
    sql = "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)"
    params = (key, encoded, now_iso())
    if conn:
        conn.execute(sql, params)
    else:
        with db() as local_conn:
            local_conn.execute(sql, params)


def _check_and_add_column(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    cols = {row["name"] for row in cursor.fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def create_schema(db_path: Path | None = None) -> None:
    target_path = db_path or settings.db_path
    with db(target_path) as conn:
        # 1. Core new tables
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
                archived_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_directories (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, path)
            );

            CREATE TABLE IF NOT EXISTS contents (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id),
                kind TEXT NOT NULL CHECK(kind IN ('memory', 'document')),
                category TEXT NOT NULL CHECK(category IN ('preference', 'background', 'decision', 'task', 'lesson', 'knowledge')),
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                agent TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'historical')),
                version INTEGER NOT NULL DEFAULT 1,
                related_ids TEXT NOT NULL DEFAULT '[]',
                content_hash TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS content_versions (
                id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL,
                UNIQUE(content_id, version)
            );

            CREATE TABLE IF NOT EXISTS content_identity_history (
                content_id TEXT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
                content_hash TEXT NOT NULL,
                project_id TEXT,
                PRIMARY KEY(content_id, content_hash)
            );
            INSERT OR IGNORE INTO content_identity_history(content_id,content_hash,project_id)
                SELECT id,content_hash,project_id FROM contents WHERE content_hash IS NOT NULL;
            CREATE TRIGGER IF NOT EXISTS content_identity_insert AFTER INSERT ON contents
            WHEN NEW.content_hash IS NOT NULL BEGIN
                INSERT OR IGNORE INTO content_identity_history VALUES(NEW.id,NEW.content_hash,NEW.project_id);
            END;
            CREATE TRIGGER IF NOT EXISTS content_identity_update BEFORE UPDATE OF content_hash ON contents
            WHEN OLD.content_hash IS NOT NEW.content_hash BEGIN
                INSERT OR IGNORE INTO content_identity_history
                    SELECT OLD.id,OLD.content_hash,OLD.project_id WHERE OLD.content_hash IS NOT NULL;
                INSERT OR IGNORE INTO content_identity_history
                    SELECT NEW.id,NEW.content_hash,NEW.project_id WHERE NEW.content_hash IS NOT NULL;
            END;

            CREATE TABLE IF NOT EXISTS content_sources (
                id TEXT PRIMARY KEY,
                content_id TEXT NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
                source_id TEXT,
                source_kind TEXT NOT NULL CHECK(source_kind IN ('conversation', 'document')),
                source_title TEXT NOT NULL DEFAULT '',
                source_agent TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                quote TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                external_id TEXT,
                project_id TEXT REFERENCES projects(id),
                agent TEXT NOT NULL,
                title TEXT NOT NULL,
                cwd TEXT,
                source_path TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                messages TEXT NOT NULL DEFAULT '[]',
                process_summary TEXT NOT NULL DEFAULT '[]',
                processing_status TEXT NOT NULL DEFAULT 'pending' CHECK(processing_status IN ('pending', 'processed', 'error')),
                processing_error TEXT,
                content_hash TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_agent_ext ON conversations(agent, external_id) WHERE external_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS idx_conversations_updated_id ON conversations(updated_at DESC,id DESC);
            CREATE INDEX IF NOT EXISTS idx_conversations_project_updated ON conversations(project_id,updated_at DESC,id DESC);
            CREATE INDEX IF NOT EXISTS idx_conversations_agent_updated ON conversations(agent,updated_at DESC,id DESC);

            CREATE TABLE IF NOT EXISTS conversation_message_offsets (
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                start_byte INTEGER NOT NULL CHECK(start_byte >= 0),
                byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
                PRIMARY KEY(conversation_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS conversation_message_index_state (
                conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                message_count INTEGER NOT NULL CHECK(message_count >= 0)
            );
            CREATE TRIGGER IF NOT EXISTS invalidate_conversation_message_offsets
            AFTER UPDATE OF messages ON conversations
            WHEN OLD.messages IS NOT NEW.messages
            BEGIN
                DELETE FROM conversation_message_offsets WHERE conversation_id = NEW.id;
                DELETE FROM conversation_message_index_state WHERE conversation_id = NEW.id;
            END;


            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                discovered INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                paths TEXT NOT NULL DEFAULT '[]',
                capabilities TEXT NOT NULL DEFAULT '{}',
                token_hash TEXT,
                token_preview TEXT,
                last_sync TEXT,
                error TEXT,
                config_preview TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conflicts (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id),
                existing_id TEXT REFERENCES contents(id) ON DELETE CASCADE,
                proposed TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'resolved')),
                resolution TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS handoffs (
                id TEXT PRIMARY KEY,
                project_id TEXT REFERENCES projects(id),
                source_agent TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'awaiting_confirmation' CHECK(status IN ('awaiting_confirmation', 'confirmed', 'cancelled')),
                card TEXT NOT NULL,
                source_ids TEXT NOT NULL DEFAULT '[]',
                source_revisions TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                confirmed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tombstones (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL CHECK(entity_type IN ('conversation', 'content', 'document', 'memory')),
                dedupe_key TEXT NOT NULL,
                scope_project_id TEXT,
                deleted_at TEXT NOT NULL,
                deleted_by TEXT NOT NULL DEFAULT 'user',
                reason TEXT,
                UNIQUE(entity_type, dedupe_key)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS distillation_queue (
                id TEXT PRIMARY KEY,
                conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'completed', 'error')),
                payload TEXT,
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_meters (
                id TEXT PRIMARY KEY,
                host_id TEXT,
                tokens INTEGER,
                requests INTEGER,
                note TEXT,
                created_at TEXT NOT NULL
            );
            """
        )

        # 2. Preserve legacy tables if absent (for compatibility without drops)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                type TEXT NOT NULL DEFAULT 'source',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'manual',
                source_file TEXT,
                agent TEXT,
                confidence TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'active',
                tags TEXT NOT NULL DEFAULT '[]',
                related_items TEXT NOT NULL DEFAULT '[]',
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                type TEXT NOT NULL DEFAULT 'memory',
                title TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'conversation',
                agent TEXT,
                confidence TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'pending',
                captured_at TEXT NOT NULL DEFAULT '',
                reviewed_at TEXT,
                merge_target TEXT,
                reject_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS knowledge_versions (
                id TEXT PRIMARY KEY,
                knowledge_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS citations (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL DEFAULT 'cites',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        # 3. Alter projects table if missing status / archived_at
        _check_and_add_column(conn, "projects", "status", "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived'))")
        _check_and_add_column(conn, "projects", "archived_at", "TEXT")
        _check_and_add_column(conn, "conversations", "external_id", "TEXT")
        _check_and_add_column(conn, "conversations", "cwd", "TEXT")
        _check_and_add_column(conn, "handoffs", "source_revisions", "TEXT NOT NULL DEFAULT '{}'")

        # 4. Migrate legacy knowledge_items to contents once (never resurrect deleted items)
        migration_done = get_app_setting("legacy_migration_done", default=False, conn=conn)
        if not migration_done:
            old_items = conn.execute("SELECT * FROM knowledge_items WHERE id NOT IN (SELECT id FROM contents)").fetchall()
            for item in old_items:
                old_type = item["type"] if "type" in item.keys() else "source"
                if old_type == "emergence":
                    continue  # Out of scope per PRD D036

                type_map = {
                    "source": ("document", "knowledge"),
                    "memory": ("memory", "background"),
                    "rule": ("memory", "decision"),
                    "task": ("memory", "task"),
                    "feedback": ("memory", "lesson"),
                }
                kind, category = type_map.get(old_type, ("memory", "knowledge"))

                old_status = item["status"] if "status" in item.keys() else "active"
                if old_status in ("archived", "outdated"):
                    continue  # Do not auto-revive archived or outdated

                content_val = item["content"] if "content" in item.keys() else ""
                title_val = item["title"] if "title" in item.keys() else ""
                proj_id = item["project_id"] if "project_id" in item.keys() else None
                c_hash = hashlib.sha256(f"content:{proj_id or ''}:{category}:{title_val.strip()}:{content_val.strip()}".encode("utf-8")).hexdigest()

                # Check tombstones: do not re-insert user-deleted legacy items
                tb = conn.execute("SELECT id FROM tombstones WHERE entity_type = 'content' AND dedupe_key = ?", (c_hash,)).fetchone()
                if tb:
                    continue

                conn.execute(
                    """
                    INSERT OR IGNORE INTO contents (
                        id, project_id, kind, category, title, content, tags, agent,
                        status, version, related_ids, content_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        proj_id,
                        kind,
                        category,
                        title_val,
                        content_val,
                        item["tags"] if "tags" in item.keys() else "[]",
                        item["agent"] if "agent" in item.keys() else None,
                        item["version"] if "version" in item.keys() else 1,
                        item["related_items"] if "related_items" in item.keys() else "[]",
                        c_hash,
                        (item["created_at"] if "created_at" in item.keys() else None) or now_iso(),
                        (item["updated_at"] if "updated_at" in item.keys() else None) or now_iso(),
                    ),
                )

                conn.execute(
                    """
                    INSERT OR IGNORE INTO content_versions (
                        id, content_id, version, title, content, actor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_id("ver"),
                        item["id"],
                        item["version"] if "version" in item.keys() else 1,
                        title_val,
                        content_val,
                        (item["agent"] if "agent" in item.keys() else None) or "legacy_migration",
                        (item["created_at"] if "created_at" in item.keys() else None) or now_iso(),
                    ),
                )
            set_app_setting("legacy_migration_done", True, conn=conn)

        # 5. Migrate legacy overbroad shared-database conversation tombstones
        shared_tombstones_migrated = get_app_setting("shared_db_tombstones_migrated", default=False, conn=conn)
        if not shared_tombstones_migrated:
            from app.services.conversation_service import is_shared_database_path, compute_source_identity_key
            rows = conn.execute(
                "SELECT DISTINCT agent, source_path FROM conversations WHERE source_path IS NOT NULL"
            ).fetchall()
            for r in rows:
                if is_shared_database_path(r["source_path"]):
                    shared_key = compute_source_identity_key(r["agent"], r["source_path"], "", None)
                    conn.execute(
                        "DELETE FROM tombstones WHERE entity_type = 'conversation' AND dedupe_key = ?",
                        (shared_key,),
                    )
            set_app_setting("shared_db_tombstones_migrated", True, conn=conn)
        # 5. Initialize default connections from the single host registry
        from app.integrations.constants import HOST_LABELS, SUPPORTED_HOSTS
        default_connections = [(host_id, HOST_LABELS[host_id]) for host_id in SUPPORTED_HOSTS]
        default_caps = {
            "history": {"status": "not_configured", "detail": "History adapter not connected"},
            "capture": {"status": "not_configured", "detail": "Capture adapter not connected"},
            "memory_read": {"status": "available", "detail": "Available via MCP search_memory"},
            "memory_write": {"status": "available", "detail": "Available via MCP save_memory"},
            "handoff": {"status": "available", "detail": "Available via MCP start_handoff"},
            "compute": {"status": "unsupported", "detail": "No local model execution engine"},
        }
        for conn_id, label in default_connections:
            exists = conn.execute("SELECT id FROM connections WHERE id = ?", (conn_id,)).fetchone()
            if not exists:
                conn.execute(
                    """
                    INSERT INTO connections (
                        id, label, discovered, enabled, paused, paths, capabilities,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conn_id,
                        label,
                        0,
                        0,
                        0,
                        encode([]),
                        encode(default_caps),
                        now_iso(),
                        now_iso(),
                    ),
                )

        # Additive service migrations preserve legacy identities and data.
        from app.services.processing_jobs import ensure_schema as ensure_jobs
        from app.services.derivation_service import ensure_schema as ensure_derivations
        from app.services.host_registry import ensure_observation_schema
        from app.services.connection_service import ensure_source_cursor_schema
        ensure_jobs(conn)
        ensure_derivations(conn)
        ensure_observation_schema(conn=conn)
        ensure_source_cursor_schema(conn=conn)
        from app.services.retrieval_service import ensure_schema as ensure_retrieval
        from app.services.usage_service import ensure_schema as ensure_usage
        ensure_retrieval(conn)
        ensure_usage(conn)
        from app.services.management_service import ensure_schema as ensure_management
        ensure_management(conn)
        from app.services.context_epoch_service import ensure_schema as ensure_context_epochs
        ensure_context_epochs(conn)
        from app.services.migration_service import ensure_schema as ensure_migrations
        ensure_migrations(conn)

        # 6. Seal schema version
        set_app_setting("schema_version", settings.schema_version, conn=conn)


def seed_data(db_path: Path | None = None) -> None:
    # Fixed connection registry only; strictly no demo data seeding
    from app.integrations.constants import HOST_LABELS, SUPPORTED_HOSTS
    default_connections = [(host_id, HOST_LABELS[host_id]) for host_id in SUPPORTED_HOSTS]
    try:
        from app.adapters import CAPABILITY_MATRIX
    except Exception:
        CAPABILITY_MATRIX = {}

    with db(db_path) as conn:
        for conn_id, label in default_connections:
            exists = conn.execute("SELECT id FROM connections WHERE id = ?", (conn_id,)).fetchone()
            caps = CAPABILITY_MATRIX.get(conn_id, {})
            if not exists:
                conn.execute(
                    """
                    INSERT INTO connections (
                        id, label, discovered, enabled, paused, paths, capabilities,
                        created_at, updated_at
                    ) VALUES (?, ?, 0, 0, 0, '[]', ?, ?, ?)
                    """,
                    (conn_id, label, encode(caps), now_iso(), now_iso()),
                )
            else:
                # Update default capabilities from canonical matrix if empty
                row = conn.execute("SELECT capabilities FROM connections WHERE id = ?", (conn_id,)).fetchone()
                if not row["capabilities"] or row["capabilities"] == "{}":
                    conn.execute(
                        "UPDATE connections SET capabilities = ?, updated_at = ? WHERE id = ?",
                        (encode(caps), now_iso(), conn_id),
                    )


def backup_database(destination: Path | str | None = None, db_path: Path | None = None) -> dict[str, Any]:
    """Create a consistent, integrity-verified whole-database SQLite backup."""
    from app.private_storage import check_directory_path, private_directory, private_file

    source_path = Path(db_path or settings.db_path).expanduser()
    data_dir = os.environ.get("CONTEXTCORE_DATA_DIR")
    prepare_database(source_path, data_directory=Path(data_dir) if data_dir else None)

    created_at = now_iso()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    default_name = f"contextcore-backup-{stamp}-{uuid4().hex[:8]}.db"

    if destination is None:
        backup_dir = source_path.parent / "backups"
        private_directory(backup_dir)
        dest_path = backup_dir / default_name
    else:
        candidate = Path(destination).expanduser()
        if candidate.exists() and candidate.is_dir():
            private_directory(candidate)
            dest_path = candidate / default_name
        else:
            check_directory_path(candidate.parent)
            candidate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            dest_path = candidate

    if dest_path.is_symlink():
        raise OSError("Backup destination cannot be a symbolic link")

    if dest_path.resolve(strict=False) == source_path.resolve(strict=False):
        raise OSError("Backup destination cannot overwrite the live database")
    if dest_path.exists() and source_path.exists():
        try:
            if os.path.samefile(dest_path, source_path):
                raise OSError("Backup destination cannot overwrite the live database")
        except OSError as exc:
            if "overwrite the live database" in str(exc):
                raise

    for sidecar_suffix in ("-wal", "-shm"):
        dest_sidecar = Path(f"{dest_path}{sidecar_suffix}")
        if dest_sidecar.exists() or dest_sidecar.is_symlink():
            raise OSError(f"Refusing to overwrite destination with existing SQLite sidecar: {dest_sidecar.name}")

    temp_path = dest_path.parent / f".{dest_path.name}.tmp_{uuid4().hex}"
    try:
        private_file(temp_path, create=True)
        src_conn = sqlite3.connect(source_path, timeout=10)
        dst_conn = sqlite3.connect(temp_path, timeout=10)
        try:
            src_conn.backup(dst_conn)
            dst_conn.execute("PRAGMA journal_mode = DELETE")
            check_row = dst_conn.execute("PRAGMA quick_check").fetchone()
            if not check_row or check_row[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
            dst_conn.commit()
        finally:
            dst_conn.close()
            src_conn.close()

        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(temp_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        private_file(temp_path)
        hasher = hashlib.sha256()
        with open(temp_path, "r+b") as fh:
            while chunk := fh.read(65536):
                hasher.update(chunk)
            os.fsync(fh.fileno())
        size_bytes = temp_path.stat().st_size
        os.replace(temp_path, dest_path)
        private_file(dest_path)
        try:
            dir_fd = os.open(dest_path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        for candidate_cleanup in (temp_path, Path(f"{temp_path}-wal"), Path(f"{temp_path}-shm"), Path(f"{temp_path}-journal")):
            try:
                if candidate_cleanup.exists():
                    candidate_cleanup.unlink()
            except OSError:
                pass
        raise

    try:
        log_audit(actor="user", action="database.backup", target=str(dest_path))
    except Exception:
        pass

    return {
        "ok": True,
        "source_path": str(source_path),
        "backup_path": str(dest_path),
        "size_bytes": size_bytes,
        "sha256": hasher.hexdigest(),
        "integrity": "ok",
        "created_at": created_at,
    }

