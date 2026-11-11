"""Native context boundaries are distinct from persistent chat session IDs."""
import hashlib
from app.database import db, new_id


def ensure_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS host_context_epochs(consumer_id TEXT PRIMARY KEY,epoch TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS host_context_boundaries(consumer_id TEXT NOT NULL,event_key TEXT NOT NULL,epoch TEXT NOT NULL,PRIMARY KEY(consumer_id,event_key))")


def epoch_for_record(host_id, record):
    native = str(record.get("session_id") or record.get("external_id") or "unknown")
    consumer = f"{host_id}:{native}"
    metadata = record.get("metadata") or {}
    event = str(record.get("event") or "").lower().replace("_", "").replace("-", "")
    boundary = metadata.get("source") == "compact" or event in {"sessionstart", "postcompact", "aftercompact"}
    explicit = record.get("context_epoch") or record.get("compaction_id")
    identity = explicit or record.get("event_id") or record.get("timestamp")
    with db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT epoch FROM host_context_epochs WHERE consumer_id=?", (consumer,)).fetchone()
        epoch = current[0] if current else new_id("epoch")
        if boundary or explicit:
            if identity:
                key = hashlib.sha256(f"{event}:{metadata.get('source')}:{identity}".encode()).hexdigest()
                prior = conn.execute("SELECT epoch FROM host_context_boundaries WHERE consumer_id=? AND event_key=?", (consumer, key)).fetchone()
                # A delayed old notification cannot roll the live context back.
                if prior:
                    return consumer, current[0] if current else prior[0]
                epoch = new_id("epoch")
                conn.execute("INSERT OR IGNORE INTO host_context_boundaries VALUES(?,?,?)", (consumer, key, epoch))
            else:
                # No native identity: prefer safe re-emission over dropping constraints.
                epoch = new_id("epoch")
        conn.execute("INSERT INTO host_context_epochs VALUES(?,?) ON CONFLICT(consumer_id) DO UPDATE SET epoch=excluded.epoch", (consumer, epoch))
    return consumer, epoch
