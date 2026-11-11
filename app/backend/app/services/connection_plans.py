"""Capability-scoped connections using the shared configuration transaction journal."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.config import settings
from app.integrations.config_transactions import ConfigurationTransactions, ConfigurationError, canonical, configuration_lock
from app.integrations.installer import apply_host_integration, _manifest_path
from app.integrations.constants import SUPPORTED_HOSTS
from app.integrations.recovery import plan_recovery, read_file
from app.integrations.secret_store import KeychainSecretStore
from app.services.host_registry import discover_hosts, HOST_DEFINITIONS

CAPABILITIES = {'context_tools': 'mcp', 'continuation': 'skill', 'lifecycle': 'hooks'}


def grants_key(host):
    return f'connection_grants:{host}'


def read_grants(conn, host):
    row = conn.execute('SELECT value FROM app_settings WHERE key=?', (grants_key(host),)).fetchone()
    return json.loads(row[0]) if row else None


class ConnectionPlans:
    def __init__(self, home=None):
        self.home = Path(home or Path.home()).resolve()
        self.isolated = home is not None
        self.transactions = ConfigurationTransactions(settings.db_path, settings.db_path.parent/'configuration-snapshots',
                                                       [self.home], KeychainSecretStore())

    def _state(self, host, conn=None):
        if conn is None:
            with self.transactions.connect() as connection:
                return self._state(host, connection)
        row = conn.execute('SELECT enabled,paused,paths,token_hash FROM connections WHERE id=?', (host,)).fetchone()
        if row is None: raise ConfigurationError('Connection was not found', 404)
        return {**dict(row), 'grants': read_grants(conn, host)}

    def _state_hash(self, host, conn=None):
        return hashlib.sha256(canonical(self._state(host, conn)).encode()).hexdigest()

    def _host_identity(self, host, surface):
        discovered = next(h for h in discover_hosts(home=self.home if self.isolated else None) if h['id'] == host)
        identity = []
        for item in discovered['discovery']['evidence']:
            if item['kind'] not in ('binary', 'application'): continue
            if surface == 'cli' and item['kind'] != 'binary': continue
            if surface != 'cli' and item['kind'] != 'application': continue
            p = Path(item['path'])
            record = {'path': str(p), 'kind': item['kind'], 'status': item['status']}
            try:
                st = p.stat()
                record.update(size=st.st_size, mtime_ns=st.st_mtime_ns, inode=st.st_ino, resolved=str(p.resolve()))
                if item['kind'] == 'application':
                    info = p/'Contents/Info.plist'
                    if info.is_file(): record['version_metadata_sha256'] = hashlib.sha256(info.read_bytes()).hexdigest()
            except OSError: pass
            identity.append(record)
        return identity

    def _token(self, host, *, preview):
        path = self.home/'.contextcore/credentials'/f'{host}.token'
        raw, mode = read_file(path, self.home)
        expected = self._state(host)['token_hash']
        if raw is not None:
            if mode & 0o077 or len(raw) > 4096:
                raise ConfigurationError('Credential permissions or size require explicit repair')
            token = raw.decode().strip()
            if not token or not expected or hashlib.sha256(token.encode()).hexdigest() != expected:
                raise ConfigurationError('Existing credential requires explicit repair; no token was rotated')
            return token
        if expected:
            raise ConfigurationError('Credential file is missing; explicit recovery is required')
        return '<credential-created-after-confirmation>' if preview else f'cc_agent_{host}_{secrets.token_hex(16)}'

    def _render(self, host, operation, components, base_url, *, preview):
        if operation in ('uninstall', 'pause', 'resume'):
            recovery = plan_recovery(host, self.home, components=components)
            if operation != 'uninstall':
                if set(components) & {'mcp', 'hooks'}: self._token(host, preview=True)
                return {str(c.path): c.before for c in recovery.changes}, None
            return {str(c.path): c.after for c in recovery.changes}, None
        token = self._token(host, preview=preview) if set(components) & {'mcp', 'hooks'} else ''
        result = apply_host_integration(host, token=token, confirmed=True, home=self.home,
                                        base_url=base_url, components=components, prepare_only=True)
        return result.prepared_files, token or None

    def create(self, host, body, base_url):
        if host not in SUPPORTED_HOSTS: raise ConfigurationError('Unknown connection', 404)
        operation = body.get('operation')
        capabilities = body.get('capabilities')
        surface = body.get('surface')
        if operation not in ('install', 'update', 'uninstall', 'pause', 'resume'):
            raise ConfigurationError('Select a supported connection operation', 422)
        if (not isinstance(capabilities, list) or not capabilities or
            any(not isinstance(c, str) or c not in CAPABILITIES for c in capabilities) or
            len(capabilities) != len(set(capabilities))):
            raise ConfigurationError('Explicit distinct connection capabilities are required', 422)
        if surface not in HOST_DEFINITIONS[host].surfaces:
            raise ConfigurationError('Select the target host entry', 422)
        components = [CAPABILITIES[c] for c in capabilities]
        from app.services.connection_service import resolve_trusted_base_url
        base_url = resolve_trusted_base_url(base_url)
        with configuration_lock:
            self._state(host)
            with self.transactions.connect() as conn:
                pending = conn.execute("SELECT id FROM management_plans WHERE status IN ('applying','recovery_required') AND json_extract(payload,'$.metadata.namespace')='connections' AND json_extract(payload,'$.metadata.host_id')=? LIMIT 1", (host,)).fetchone()
            if pending:
                raise ConfigurationError('An interrupted connection plan needs recovery before creating another plan')
            manifest = _manifest_path(host, self.home)
            if operation == 'update' and not manifest.exists():
                raise ConfigurationError('No installed components to update')
            files, _ = self._render(host, operation, components, base_url, preview=True)
            metadata = {'namespace': 'connections', 'host_id': host, 'surface': surface, 'operation': operation,
                        'capabilities': capabilities, 'components': components, 'base_url': base_url,
                        'host_identity': self._host_identity(host, surface), 'state_hash': self._state_hash(host),
                        'expires_at': (datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(),
                        'dependencies': ['Context tools must be available to execute the continuation command'] if 'continuation' in capabilities else [],
                        'risks': ['Host loading and real calls are not verified by a configuration write',
                                  'History access, model configuration and model calls are not authorized by this plan']}
            return self.transactions.create_deferred(list(files), metadata)

    def get(self, host, plan_id):
        plan = self.transactions.get(plan_id)
        meta = plan['payload']['metadata']
        if meta.get('namespace') != 'connections' or meta.get('host_id') != host:
            raise ConfigurationError('Connection plan was not found', 404)
        return plan

    def apply(self, host, plan_id, version, confirmed):
        if confirmed is not True or type(version) is not int:
            raise ConfigurationError('Explicit confirmation and integer plan version are required', 422)
        with configuration_lock:
            plan = self.get(host, plan_id)
            meta = plan['payload']['metadata']
            if plan['status'] != 'applied':
                if datetime.now(timezone.utc) >= datetime.fromisoformat(meta['expires_at']):
                    raise ConfigurationError('Plan expired; create a new preview')
                if self._host_identity(host, meta['surface']) != meta['host_identity']:
                    raise ConfigurationError('Host entry or version changed; create a new preview')
                if self._state_hash(host) != meta['state_hash']:
                    raise ConfigurationError('Connection permissions changed; create a new preview')
            token = None
            def render(_plan):
                nonlocal token
                files, token = self._render(host, meta['operation'], meta['components'], meta['base_url'], preview=False)
                return files
            def finalize(conn, applying):
                if self._state_hash(host, conn) != meta['state_hash']:
                    raise ConfigurationError('Connection changed during apply; recovery is required')
                state = self._state(host, conn)
                grants = state['grants'] or {c: bool(state['enabled'] and state['token_hash']) for c in CAPABILITIES}
                for capability in meta['capabilities']:
                    grants[capability] = meta['operation'] in ('install', 'update', 'resume')
                manifest_raw, _ = read_file(_manifest_path(host, self.home), self.home)
                installed = json.loads(manifest_raw).get('components', []) if manifest_raw else []
                for capability, component in CAPABILITIES.items():
                    if component not in installed: grants[capability] = False
                token_hash = hashlib.sha256(token.encode()).hexdigest() if token else state['token_hash']
                if not set(installed) & {'mcp', 'hooks'}: token_hash = None
                from app.database import set_app_setting
                set_app_setting(grants_key(host), grants, conn=conn)
                conn.execute('UPDATE connections SET token_hash=?,token_preview=?,updated_at=? WHERE id=?',
                             (token_hash, 'configured' if token_hash else None, datetime.now(timezone.utc).isoformat(), host))
            result = self.transactions.apply(plan_id, version, confirmed, finalize=finalize, renderer=render)
            return result

    def recover(self, host, plan_id, confirmed):
        self.get(host, plan_id)
        return self.transactions.recover(plan_id, confirmed)


def public_plan(plan):
    payload = plan['payload']
    meta = payload['metadata']
    return {'id': plan['id'], 'version': plan['version'], 'status': plan['status'],
            'host_id': meta['host_id'], 'surface': meta['surface'], 'operation': meta['operation'],
            'capabilities': meta['capabilities'], 'expires_at': meta['expires_at'],
            'host_identity': meta['host_identity'], 'dependencies': meta['dependencies'], 'risks': meta['risks'],
            'files': [{k: v for k, v in item.items() if k in ('path', 'before_hash', 'after_hash', 'stage')}
                      for item in payload['files']],
            'readback': payload['readback'], 'host_reload': payload['host_reload'], 'real_request': payload['real_request']}
