"""Durable groups of existing, independently recoverable host configuration plans."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.integrations.config_transactions import ConfigurationError, ConfigurationTransactions, configuration_lock
from app.integrations.secret_store import SecretUnavailable


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS management_shared_batches (
        id TEXT PRIMARY KEY, operation TEXT NOT NULL, version INTEGER NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shared_batches_created ON management_shared_batches(created_at DESC, id DESC)")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_shared_targets (
        batch_id TEXT NOT NULL, position INTEGER NOT NULL, host_id TEXT NOT NULL, asset_versions TEXT NOT NULL,
        asset_names TEXT NOT NULL, plan_id TEXT, error_code TEXT, PRIMARY KEY(batch_id, host_id))""")


@contextmanager
def _write_lock(manager):
    # In-process host operations use an RLock. A second CLI/server process can
    # share the database, so also serialize whole-batch revision claims across
    # processes. OS locks release automatically if an applying process exits.
    import fcntl
    with configuration_lock:
        fd = os.open(manager.db_path.parent / 'shared-batches.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_targets(items):
    from app.services.management_service import HOSTS
    if not isinstance(items, list) or not 1 <= len(items) <= len(HOSTS):
        raise ConfigurationError('Select one or more known assistants', 422)
    hosts = set()
    for target in items:
        if not isinstance(target, dict):
            raise ConfigurationError('Invalid target', 422)
        host, versions = target.get('host_id'), target.get('asset_versions')
        if not isinstance(host, str) or host not in HOSTS or host in hosts:
            raise ConfigurationError('Unknown or duplicate assistant', 422)
        if not isinstance(versions, dict) or not 1 <= len(versions) <= 100 or any(
            not re.fullmatch(r'asset_[a-f0-9]{16}', key) or type(v) is not int or v < 1 for key, v in versions.items()
        ):
            raise ConfigurationError('Select 1 to 100 versioned resources per assistant', 422)
        hosts.add(host)


def _error_code(exc, fallback):
    if isinstance(exc, SecretUnavailable):
        return 'credential_unavailable'
    if isinstance(exc, (OSError, sqlite3.Error)):
        return 'storage_error'
    if exc.status_code == 404 or ('since preview' in str(exc) and 'asset' in str(exc).lower()):
        return 'stale_asset'
    if 'expired' in str(exc).lower():
        return 'plan_expired'
    return 'unsupported' if exc.status_code == 422 else fallback


_EXPECTED_FAILURE = (ConfigurationError, SecretUnavailable, OSError, sqlite3.Error)


def _destinations(plan):
    files = plan['payload']['files']
    occupied = {('file', item['path']) for item in files}
    # Native adapters can render a different folder name for the same Skill
    # (e.g. CLI / desktop). Sharing one library must not install it twice.
    manifests = {}
    for item in files:
        path = Path(item['path'])
        if path.name != 'SKILL.md':
            continue
        for spec in item['specs']:
            if spec.get('managed_skill'):
                previous = manifests.get(spec['owner'])
                if previous is None or len(path.parts) < len(previous.parts):
                    manifests[spec['owner']] = path
    occupied.update(('skill', str(path.parent.parent), owner) for owner, path in manifests.items())
    return occupied


def _prepare(manager, operation, target, occupied, refresh=False):
    host, versions = target['host_id'], dict(target['asset_versions'])
    names, plan_id, error = {}, None, None
    try:
        assets = [manager.get('asset', key) for key in versions]
        names = {asset['id']: asset['name'] for asset in assets}
        current = {asset['id']: asset['version'] for asset in assets}
        if not refresh and current != versions:
            error = 'stale_asset'
        else:
            versions = current
            plan = manager.plan({'host_id': host, 'asset_ids': list(versions), 'operation': operation})
            paths = _destinations(plan)
            reviewed = plan['payload']['metadata']['entity_versions']
            if any(reviewed.get(key) != version for key, version in versions.items()):
                error = 'stale_asset'
            elif paths & occupied:
                error = 'shared_target'
            if error:
                # Also prevent a rejected preview being applied via the individual-plan endpoint.
                with manager.connect() as conn:
                    conn.execute("UPDATE management_plans SET status='conflict' WHERE id=? AND status='planned'", (plan['id'],))
            else:
                plan_id = plan['id']
                occupied.update(paths)
    except _EXPECTED_FAILURE as exc:
        error = _error_code(exc, 'preview_rejected')
    return json.dumps(versions), json.dumps(names), plan_id, error


def create(manager, body):
    operation, targets = body.get('operation'), body.get('targets')
    if operation not in ('install', 'uninstall'):
        raise ConfigurationError('Choose install or uninstall', 422)
    _validate_targets(targets)
    batch_id = 'batch_' + uuid4().hex
    with _write_lock(manager):
        occupied = set()
        records = [(batch_id, i, row['host_id'], *_prepare(manager, operation, row, occupied))
                   for i, row in enumerate(targets)]
        now = _now()
        with manager.connect() as conn:
            conn.execute("INSERT INTO management_shared_batches VALUES (?,?,1,?,?)", (batch_id, operation, now, now))
            conn.executemany("INSERT INTO management_shared_targets VALUES (?,?,?,?,?,?,?)", records)
        return detail(manager, batch_id)


def _target_state(row):
    status = row['plan_status']
    if status == 'applied':
        return 'applied', None
    if status in ('applying', 'recovery_required'):
        return 'recovery_required', 'recovery_required'
    if row['error_code']:
        return 'failed', row['error_code']
    if status == 'planned':
        try:
            ConfigurationTransactions.require_fresh({'status': status, 'created_at': row['plan_time']})
        except ConfigurationError:
            return 'failed', 'plan_expired'
        return 'planned', None
    return 'failed', 'apply_conflict'


def _read(conn, batch, *, include_targets):
    # Summary reads only stage and timestamp, never deserializes plan bodies.
    fields = ',p.payload' if include_targets else ''
    rows = conn.execute("""SELECT t.*,p.status plan_status,p.created_at plan_time""" + fields + """
        FROM management_shared_targets t LEFT JOIN management_plans p ON t.plan_id=p.id
        WHERE t.batch_id=? ORDER BY t.position""", (batch['id'],))
    counts = dict.fromkeys(('planned', 'applied', 'failed', 'recovery_required'), 0)
    targets = []
    for row in rows:
        status, error = _target_state(row)
        counts[status] += 1
        if include_targets:
            payload = json.loads(row['payload']) if row['payload'] else {}
            targets.append({
                'host_id': row['host_id'], 'asset_versions': json.loads(row['asset_versions']),
                'asset_names': json.loads(row['asset_names']), 'plan_id': row['plan_id'],
                'status': status, 'error_code': error, 'can_apply': status == 'planned',
                'can_repreview': status == 'failed',
                'files': [{'path': file['path'], 'diff': file['diff']} for file in payload.get('files', [])],
                'file_readback': payload.get('readback', 'not_run'),
                'host_reload': payload.get('host_reload', 'not_run'),
            })
    size = sum(counts.values())
    state = 'partial'
    if counts['planned'] == size:
        state = 'planned'
    elif counts['applied'] == size:
        state = 'applied'
    elif counts['failed'] + counts['recovery_required'] == size:
        state = 'failed'
    result = {**dict(batch), 'status': state, 'counts': counts}
    if include_targets:
        result['targets'] = targets
    return result


def detail(manager, batch_id):
    with manager.connect() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM management_shared_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise ConfigurationError('Batch not found', 404)
        return _read(conn, row, include_targets=True)


def history(manager, limit, offset):
    with manager.connect() as conn:
        conn.execute("BEGIN")
        total = conn.execute("SELECT COUNT(*) FROM management_shared_batches").fetchone()[0]
        rows = conn.execute("SELECT * FROM management_shared_batches ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                            (limit, offset))
        return {'items': [_read(conn, row, include_targets=False) for row in rows], 'total': total,
                'limit': limit, 'offset': offset}


def _select(batch, body, action):
    version = body.get('expected_version')
    if type(version) is not int or version != batch['version']:
        raise ConfigurationError('Batch changed; refresh and review again', 409)
    eligible = {row['host_id']: row for row in batch['targets'] if row['can_' + action]}
    hosts = body.get('host_ids', list(eligible) if action == 'apply' else None)
    if not isinstance(hosts, list) or not hosts or any(not isinstance(key, str) or key not in eligible for key in hosts):
        raise ConfigurationError('Select eligible targets for this batch', 422)
    if len(set(hosts)) != len(hosts):
        raise ConfigurationError('Duplicate assistant', 422)
    return [eligible[key] for key in hosts]


def update(manager, batch_id, body, action):
    if action == 'apply' and body.get('confirmed') is not True:
        raise ConfigurationError('Explicit confirmation is required', 422)
    with _write_lock(manager):
        batch = detail(manager, batch_id)
        selected = _select(batch, body, action)
        if action == 'repreview':
            # Reserve previously reviewed targets, even ones that have already applied.
            occupied = set()
            for target in batch['targets']:
                if target['status'] in ('planned', 'applied', 'recovery_required'):
                    occupied.update(_destinations(manager.transactions.get(target['plan_id'])))
            # Publish the replacement previews and their revision atomically. A
            # crash must never leave an unseen new preview at a reviewed version.
            prepared = [(*_prepare(manager, batch['operation'], row, occupied, refresh=True), batch_id, row['host_id'])
                        for row in selected]
            with manager.connect() as conn:
                # A pre-write failure may leave a still-runnable old plan. Retire
                # it when replacing it so a stale host history view cannot apply it.
                conn.executemany("UPDATE management_plans SET status='conflict' WHERE id=? AND status='planned'",
                                 [(row['plan_id'],) for row in selected if row['plan_id']])
                conn.executemany("""UPDATE management_shared_targets SET asset_versions=?,asset_names=?,plan_id=?,error_code=?
                                    WHERE batch_id=? AND host_id=?""", prepared)
                conn.execute("UPDATE management_shared_batches SET version=version+1,updated_at=? WHERE id=?", (_now(), batch_id))
        else:
            # Claim before host writes. Individual plans are the authoritative
            # receipts after a crash, including a committed write with lost response.
            with manager.connect() as conn:
                conn.execute("UPDATE management_shared_batches SET version=version+1,updated_at=? WHERE id=?", (_now(), batch_id))
            for row in selected:
                try:
                    plan = manager.transactions.get(row['plan_id'])
                    manager.apply(plan['id'], plan['version'], True)
                except _EXPECTED_FAILURE as exc:
                    with manager.connect() as conn:
                        conn.execute("UPDATE management_shared_targets SET error_code=? WHERE batch_id=? AND host_id=?",
                                     (_error_code(exc, 'apply_conflict'), batch_id, row['host_id']))
        return detail(manager, batch_id)
