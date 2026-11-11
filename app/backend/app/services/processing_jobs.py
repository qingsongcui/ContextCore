"""Durable distillation identities, leases and commit fences (metadata only).

The legacy queue remains the public compatibility view. This ledger owns whether
an attempt may run: an invoked attempt with no receipt is never replayed.
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any

from fastapi import HTTPException
from app.database import db, now_iso, new_id


def ensure_schema(conn) -> None:
    # Deliberately avoid executescript: callers already hold ingestion transactions.
    statements = [
        """CREATE TABLE IF NOT EXISTS processing_epochs (
            id INTEGER PRIMARY KEY CHECK(id=1), governance INTEGER NOT NULL DEFAULT 0,
            policy INTEGER NOT NULL DEFAULT 0)""",
        "INSERT OR IGNORE INTO processing_epochs(id) VALUES(1)",
        """CREATE TABLE IF NOT EXISTS processing_sources (
            conversation_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
            fingerprint TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS processing_jobs (
            id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
            request_key TEXT NOT NULL UNIQUE, payload_hash TEXT NOT NULL,
            source_revision INTEGER NOT NULL, source_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
              ('pending','running','succeeded','failed','unknown','cancelled')),
            owner TEXT, lease_until TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            invoked INTEGER NOT NULL DEFAULT 0, policy_revision INTEGER,
            governance_revision INTEGER, receipt TEXT, error_code TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS processing_connection_epochs (
            connection_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS processing_content_epochs (
            content_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)""",
        "CREATE INDEX IF NOT EXISTS idx_processing_jobs_state ON processing_jobs(state,created_at)",
        """CREATE TABLE IF NOT EXISTS processing_events (
            event_key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
            job_id TEXT NOT NULL, created_at TEXT NOT NULL)""",
    ]
    for statement in statements:
        conn.execute(statement)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    columns = {r[1] for r in conn.execute('PRAGMA table_info(processing_jobs)')}
    if 'target_fences' not in columns:
        conn.execute("ALTER TABLE processing_jobs ADD COLUMN target_fences TEXT NOT NULL DEFAULT '{}'")
    if 'base_request_key' not in columns:
        conn.execute("ALTER TABLE processing_jobs ADD COLUMN base_request_key TEXT")
    if 'connection_fences' not in columns:
        conn.execute("ALTER TABLE processing_jobs ADD COLUMN connection_fences TEXT NOT NULL DEFAULT '{}'")
    # Per-entity monotonic fences: unrelated writes do not poison in-flight work.
    for table in ('contents','tombstones','conflicts'):
        for event in ('insert','update','delete'):
            conn.execute(f'DROP TRIGGER IF EXISTS processing_fence_{table}_{event}')
    if 'contents' in tables:
        for event in ('INSERT','UPDATE','DELETE'):
            ref = 'OLD' if event == 'DELETE' else 'NEW'
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS processing_content_{event.lower()}
                AFTER {event} ON contents BEGIN
                INSERT INTO processing_content_epochs(content_id,revision) VALUES({ref}.id,1)
                ON CONFLICT(content_id) DO UPDATE SET revision=revision+1; END""")
    if 'connections' in tables:
        conn.execute('DROP TRIGGER IF EXISTS processing_fence_connection')
        conn.execute('DROP TRIGGER IF EXISTS processing_fence_connection_delete')
        conn.execute("""CREATE TRIGGER IF NOT EXISTS processing_connection_update
            AFTER UPDATE OF enabled,paused,token_hash ON connections
            WHEN OLD.enabled IS NOT NEW.enabled OR OLD.paused IS NOT NEW.paused
              OR OLD.token_hash IS NOT NEW.token_hash BEGIN
            INSERT INTO processing_connection_epochs(connection_id,revision) VALUES(NEW.id,1)
            ON CONFLICT(connection_id) DO UPDATE SET revision=revision+1; END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS processing_connection_delete
            AFTER DELETE ON connections BEGIN
            INSERT INTO processing_connection_epochs(connection_id,revision) VALUES(OLD.id,1)
            ON CONFLICT(connection_id) DO UPDATE SET revision=revision+1; END""")
    if 'conversations' in tables:
        conn.execute("""CREATE TRIGGER IF NOT EXISTS processing_fence_source
            AFTER UPDATE OF agent,external_id,project_id,cwd,source_path,title,messages,process_summary,content_hash ON conversations
            WHEN OLD.agent IS NOT NEW.agent OR OLD.external_id IS NOT NEW.external_id
              OR OLD.project_id IS NOT NEW.project_id OR OLD.cwd IS NOT NEW.cwd
              OR OLD.source_path IS NOT NEW.source_path OR OLD.title IS NOT NEW.title
              OR OLD.messages IS NOT NEW.messages OR OLD.process_summary IS NOT NEW.process_summary
              OR OLD.content_hash IS NOT NEW.content_hash BEGIN
            UPDATE processing_sources SET revision=revision+1,fingerprint='' WHERE conversation_id=NEW.id; END""")
    if 'app_settings' in tables:
        # set_app_setting uses INSERT OR REPLACE, so monitor INSERT, not only UPDATE.
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            ref = 'OLD' if event == 'DELETE' else 'NEW'
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS processing_fence_settings_{event.lower()}
                AFTER {event} ON app_settings
                WHEN {ref}.key IN ('compute_host','compute_authorized') BEGIN
                UPDATE processing_epochs SET policy=policy+1 WHERE id=1; END""")


def source_fingerprint(conv) -> str:
    material = {k: conv[k] for k in (
        'id', 'agent', 'external_id', 'project_id', 'cwd', 'source_path',
        'title', 'messages', 'process_summary', 'content_hash')}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_revision(conn, conv) -> tuple[int, str]:
    fingerprint = source_fingerprint(conv)
    row = conn.execute('SELECT * FROM processing_sources WHERE conversation_id=?', (conv['id'],)).fetchone()
    revision = (row['revision'] + (bool(row['fingerprint']) and row['fingerprint'] != fingerprint)) if row else 1
    conn.execute("""INSERT INTO processing_sources(conversation_id,revision,fingerprint) VALUES(?,?,?)
        ON CONFLICT(conversation_id) DO UPDATE SET revision=excluded.revision,fingerprint=excluded.fingerprint""",
        (conv['id'], revision, fingerprint))
    return revision, fingerprint


def epochs(conn) -> tuple[int, int]:
    row = conn.execute('SELECT policy,governance FROM processing_epochs WHERE id=1').fetchone()
    return row['policy'], row['governance']


def source_is_current(conn, job, conv) -> bool:
    row = conn.execute('SELECT revision FROM processing_sources WHERE conversation_id=?', (conv['id'],)).fetchone()
    return bool(row and row['revision'] == job['source_revision']
                and source_fingerprint(conv) == job['source_fingerprint'])


def register_job(conn, conv, queue_id: str, *, event_key: str | None = None,
                 event_payload_hash: str | None = None) -> dict[str, Any]:
    ensure_schema(conn)
    revision, fingerprint = source_revision(conn, conv)
    request_key = hashlib.sha256(f"distill:v2:{conv['id']}:{revision}:{fingerprint}".encode()).hexdigest()
    payload_hash = event_payload_hash or fingerprint
    if event_key:
        event = conn.execute('SELECT * FROM processing_events WHERE event_key=?', (event_key,)).fetchone()
        if event:
            recorded = conn.execute('SELECT * FROM processing_jobs WHERE id=?', (event['job_id'],)).fetchone()
            if (event['payload_hash'] != payload_hash or not recorded
                    or recorded['conversation_id'] != conv['id']):
                raise HTTPException(status_code=409, detail='Processing event key reused with different content')
            return dict(recorded)
    base_key = request_key
    existing = conn.execute('SELECT * FROM processing_jobs WHERE request_key=? OR base_request_key=? ORDER BY rowid DESC LIMIT 1', (request_key,request_key)).fetchone()
    if existing and existing['state'] == 'cancelled':
        # A confirmed discarded response is safe to explicitly recompute. It
        # differs from unknown: the previous invocation definitively returned.
        count = conn.execute('SELECT COUNT(*) FROM processing_jobs WHERE conversation_id=?', (conv['id'],)).fetchone()[0]
        request_key = hashlib.sha256(f"{request_key}:refresh:{count}".encode()).hexdigest()
        existing = None
    if existing:
        job_id = existing['id']
    else:
        now = now_iso()
        # Coalesce only work that has never entered the model. A running/unknown
        # job is immutable, so its late receipt cannot complete the newer source.
        pending = conn.execute("SELECT * FROM processing_jobs WHERE conversation_id=? AND state='pending' AND invoked=0 ORDER BY created_at LIMIT 1", (conv['id'],)).fetchone()
        job_id = pending['id'] if pending else queue_id
        if pending:
            conn.execute("""UPDATE processing_jobs SET request_key=?,base_request_key=?,payload_hash=?,source_revision=?,
                source_fingerprint=?,updated_at=?,error_code=NULL WHERE id=?""",
                (request_key,base_key,payload_hash,revision,fingerprint,now,job_id))
        else:
            conn.execute("""INSERT INTO processing_jobs(id,conversation_id,request_key,base_request_key,payload_hash,
                source_revision,source_fingerprint,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'pending',?,?)""",
                (job_id,conv['id'],request_key,base_key,payload_hash,revision,fingerprint,now,now))
    if event_key:
        conn.execute('INSERT INTO processing_events VALUES(?,?,?,?)', (event_key,payload_hash,job_id,now_iso()))
    return dict(conn.execute('SELECT * FROM processing_jobs WHERE id=?', (job_id,)).fetchone())


def initialize_processing_jobs(conn=None) -> dict[str, int]:
    with nullcontext(conn) if conn is not None else db() as connection:
        ensure_schema(connection)
        rows = connection.execute("""SELECT q.* FROM distillation_queue q
            LEFT JOIN processing_jobs j ON j.id=q.id WHERE j.id IS NULL
            ORDER BY CASE q.status WHEN 'processing' THEN 0 WHEN 'completed' THEN 1
                     WHEN 'pending' THEN 2 ELSE 3 END,q.created_at""").fetchall()
        migrated = 0
        for row in rows:
            conv = connection.execute('SELECT * FROM conversations WHERE id=?', (row['conversation_id'],)).fetchone()
            if not conv:
                continue
            revision, fingerprint = source_revision(connection, conv)
            state = {'completed':'succeeded','error':'failed','processing':'unknown'}.get(row['status'],'pending')
            # Preserve every legacy ID and receipt. Old queue rows may not
            # describe the current source; never invent that historical proof.
            try:
                payload = json.loads(row['payload'] or '{}')
            except (TypeError, ValueError):
                payload = {}
            known_current = isinstance(payload,dict) and payload.get('content_hash') == conv['content_hash']
            canonical = hashlib.sha256(f"distill:v2:{conv['id']}:{revision}:{fingerprint}".encode()).hexdigest()
            request_key = canonical if known_current else hashlib.sha256(f"legacy:{row['id']}".encode()).hexdigest()
            existing = connection.execute('SELECT * FROM processing_jobs WHERE request_key=?',(request_key,)).fetchone()
            error = None
            if existing:
                request_key = hashlib.sha256(f"legacy:{row['id']}".encode()).hexdigest()
                if state == 'pending':
                    state, error = 'cancelled', 'legacy_duplicate'
            if state == 'pending' and isinstance(payload,dict) and payload.get('content_hash') and not known_current:
                state, error = 'cancelled', 'legacy_stale_source'
            if state == 'unknown':
                error = 'outcome_unknown'
            now = now_iso()
            connection.execute("""INSERT INTO processing_jobs(id,conversation_id,request_key,payload_hash,
                source_revision,source_fingerprint,state,invoked,receipt,error_code,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row['id'],conv['id'],request_key,fingerprint,revision,fingerprint,state,
                 int(row['status']=='processing'),row['result'] if state=='succeeded' else None,
                 error,row['created_at'],now))
            if error:
                connection.execute("UPDATE distillation_queue SET status='error',error=? WHERE id=?", (error,row['id']))
            migrated += 1
        return {'migrated': migrated}


def recover_processing_jobs(conn=None, *, now: str | None = None) -> dict[str, int]:
    with nullcontext(conn) if conn is not None else db() as connection:
        ensure_schema(connection)
        instant = now or now_iso()
        rows = connection.execute("SELECT * FROM processing_jobs WHERE state='running' AND (lease_until IS NULL OR lease_until<=?)", (instant,)).fetchall()
        result = {'pending':0,'unknown':0}
        for row in rows:
            state = 'unknown' if row['invoked'] else 'pending'
            result[state] += 1
            finish_job(connection,row['id'],state,error_code='outcome_unknown' if row['invoked'] else 'lease_expired')
        return result


def list_processing_jobs(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    with db() as conn:
        ensure_schema(conn)
        where, values = (' WHERE state=?', [status]) if status else ('', [])
        rows = conn.execute('SELECT * FROM processing_jobs'+where+' ORDER BY created_at LIMIT ?', (*values,max(1,min(limit,1000)))).fetchall()
        return [dict(r) for r in rows]


def title_key(category: str, title: str) -> str:
    return hashlib.sha256(f"{category}:{title.strip()}".encode()).hexdigest()


def capture_target_fences(conn, queue_id: str) -> dict[str, Any]:
    source = conn.execute('SELECT conversation_id FROM processing_jobs WHERE id=?',(queue_id,)).fetchone()
    if not source:
        return {}
    conv = conn.execute('SELECT project_id FROM conversations WHERE id=?',(source['conversation_id'],)).fetchone()
    if not conv:
        return {}
    rows = conn.execute("""SELECT c.id,c.category,c.title,c.status,coalesce(e.revision,0) AS epoch
        FROM contents c LEFT JOIN processing_content_epochs e ON e.content_id=c.id
        WHERE c.project_id IS ? AND c.kind='memory'""",(conv['project_id'],)).fetchall()
    return {r['id']:{'epoch':r['epoch'],'title_key':title_key(r['category'],r['title']),
                    'dependency':r['category']=='decision' and r['status']=='active'} for r in rows}


def targets_are_current(conn, job, memories=None) -> bool:
    snapshot = json.loads(job['target_fences'] or '{}')
    touched_ids, touched_titles = set(), set()
    for memory in memories or []:
        if not isinstance(memory,dict):
            continue
        touched_titles.add(title_key(str(memory.get('category','knowledge')),str(memory.get('title',''))))
        target = memory.get('supersedes')
        if isinstance(target,dict) and target.get('id'):
            touched_ids.add(target['id'])
    for content_id, fence in snapshot.items():
        if not (fence['dependency'] or content_id in touched_ids or fence['title_key'] in touched_titles):
            continue
        epoch = conn.execute('SELECT revision FROM processing_content_epochs WHERE content_id=?',(content_id,)).fetchone()
        if (epoch[0] if epoch else 0) != fence['epoch']:
            return False
    return True


def capture_connection_fences(conn, queue_id: str) -> dict[str, int]:
    row = conn.execute("""SELECT c.agent FROM conversations c JOIN processing_jobs j
        ON j.conversation_id=c.id WHERE j.id=?""",(queue_id,)).fetchone()
    identities = {'omp',row['agent']} if row else {'omp'}
    return {identity:(epoch[0] if (epoch := conn.execute(
        'SELECT revision FROM processing_connection_epochs WHERE connection_id=?',(identity,)).fetchone()) else 0)
        for identity in identities}


def authorization_is_current(conn, job) -> bool:
    if epochs(conn)[0] != job['policy_revision']:
        return False
    for identity, expected in json.loads(job['connection_fences'] or '{}').items():
        epoch = conn.execute('SELECT revision FROM processing_connection_epochs WHERE connection_id=?',(identity,)).fetchone()
        if (epoch[0] if epoch else 0) != expected:
            return False
    return True


def claim_job(conn, queue_id: str, *, lease_seconds: int = 300) -> dict[str, Any] | None:
    owner = new_id('lease')
    now = now_iso()
    deadline = (datetime.now(timezone.utc)+timedelta(seconds=lease_seconds)).isoformat()
    policy, governance = epochs(conn)
    fences = capture_target_fences(conn, queue_id)
    connection_fences = capture_connection_fences(conn, queue_id)
    cur = conn.execute("""UPDATE processing_jobs SET state='running',owner=?,lease_until=?,attempts=attempts+1,
        policy_revision=?,governance_revision=?,target_fences=?,connection_fences=?,updated_at=? WHERE id=? AND state='pending' AND invoked=0""",
        (owner,deadline,policy,governance,json.dumps(fences),json.dumps(connection_fences),now,queue_id))
    return dict(conn.execute('SELECT * FROM processing_jobs WHERE id=?',(queue_id,)).fetchone()) if cur.rowcount else None


def mark_invoked(conn, queue_id: str, owner: str) -> bool:
    return bool(conn.execute("UPDATE processing_jobs SET invoked=1,updated_at=? WHERE id=? AND owner=? AND state='running' AND lease_until>?",
                            (now_iso(),queue_id,owner,now_iso())).rowcount)


def finish_job(conn, queue_id: str, state: str, *, error_code: str | None = None,
               receipt: str | None = None, owner: str | None = None) -> bool:
    values = [state,error_code,receipt,now_iso(),queue_id]
    guard = ''
    if owner is not None:
        guard = " AND owner=? AND state='running'"
        values.append(owner)
    cur = conn.execute("UPDATE processing_jobs SET state=?,error_code=?,receipt=?,updated_at=?,owner=NULL,lease_until=NULL WHERE id=?"+guard, values)
    if not cur.rowcount:
        return False
    legacy = {'pending':'pending','running':'processing','succeeded':'completed'}.get(state,'error')
    conn.execute('UPDATE distillation_queue SET status=?,error=?,updated_at=? WHERE id=?', (legacy,error_code,now_iso(),queue_id))
    return True


def cancel_conversation_jobs(conn, conversation_id: str) -> None:
    ensure_schema(conn)
    conn.execute("""UPDATE processing_jobs SET state='cancelled',owner=NULL,lease_until=NULL,
        receipt=NULL,error_code='source_deleted',updated_at=? WHERE conversation_id=? AND state NOT IN ('succeeded','cancelled')""",
        (now_iso(),conversation_id))
