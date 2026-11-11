"""Durable, body-free invalidation ledger for independently owned entities.

Triggers share the source transaction, so an index outage never re-enables a
deleted record. Cleanup receipts are separate from the immediate read barrier.
"""
from __future__ import annotations

from app.database import db, now_iso


def ensure_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS derivation_cleanup (
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_kind TEXT NOT NULL,
        entity_id TEXT NOT NULL, operation TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        error TEXT, created_at TEXT NOT NULL, completed_at TEXT)""")
    for table, kind in (("contents", "content"), ("conversations", "conversation")):
        for event, op in (("DELETE", "delete"), ("UPDATE", "update")):
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS derivation_{table}_{op}
                AFTER {event} ON {table} BEGIN
                INSERT INTO derivation_cleanup(entity_kind,entity_id,operation,created_at)
                VALUES ('{kind}',OLD.id,'{op}',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS deleted_conversation_evidence
        AFTER DELETE ON conversations BEGIN
        UPDATE content_sources SET deleted=1, quote=NULL, source_title='已删除来源'
        WHERE source_id=OLD.id AND source_kind='conversation';
        END""")


def list_cleanup_jobs(limit: int = 100) -> list[dict]:
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM derivation_cleanup ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),))]


def process_cleanup_jobs(limit: int = 50) -> int:
    """Reconcile optional derived stores; never delete independent memories."""
    completed = 0
    with db() as conn:
        jobs = conn.execute("SELECT * FROM derivation_cleanup WHERE state='pending' ORDER BY id LIMIT ?", (limit,)).fetchall()
        for job in jobs:
            try:
                # Retrieval owns its schemas, triggers and cache invalidation.
                from app.services import retrieval_service
                handler = getattr(retrieval_service, "invalidate_entity", None)
                if not handler:
                    raise RuntimeError("Derived stores are not available for readback")
                handler(conn, job["entity_id"], deleted=job["operation"] == "delete")
                conn.execute("UPDATE derivation_cleanup SET state='completed', attempts=attempts+1, error=NULL, completed_at=? WHERE id=?", (now_iso(), job["id"]))
                completed += 1
            except Exception:
                # Do not persist exception bodies: plugins may embed source text.
                conn.execute("UPDATE derivation_cleanup SET attempts=attempts+1,error='cleanup_unavailable' WHERE id=?", (job["id"],))
    return completed
