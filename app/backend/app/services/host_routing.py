"""Reviewed per-host gateway configuration and recoverable native transitions.

Profiles contain references and hashes only. Upstream credentials stay in the
vault; the distinct native gateway credential is provisioned only at apply.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import secrets
from ipaddress import ip_address
from urllib.parse import urlsplit
from uuid import uuid4

from app.database import now_iso
from app.integrations.config_transactions import ConfigurationError, canonical
from app.integrations.secret_store import SecretUnavailable
from app.services.provider_endpoints import require_endpoint

HOST_ENDPOINTS = {
    'codex': ('responses',), 'claude-code': ('messages',),
    'opencode': ('chat/completions', 'responses', 'messages'),
    'pi': ('chat/completions', 'messages'),
    'kimi-code': ('chat/completions', 'responses', 'messages'),
    'hermes': ('chat/completions', 'responses', 'messages'),
    'minimax-code': ('chat/completions', 'responses', 'messages'),
    'openclaw': ('chat/completions', 'responses', 'messages'),
}
DEFAULT_ALIAS = 'senrelay-default'
NUMERIC = {'request_timeout_seconds': (120, 1, 600), 'circuit_threshold': (3, 1, 100), 'circuit_cooldown_seconds': (60, 1, 3600)}


def _identifier(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ConfigurationError('Invalid ' + label, 422)
    return value


def _origin(value):
    parsed = urlsplit(value)
    try:
        local = parsed.hostname == 'localhost' or ip_address(parsed.hostname or '').is_loopback
        valid_port = parsed.port is None or parsed.port > 0
    except ValueError:
        local, valid_port = False, False
    if not local or not valid_port or parsed.scheme not in ('http', 'https') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ConfigurationError('Gateway origin must be the current loopback service', 422)
    return value.rstrip('/')


class _ApplyVault:
    def __init__(self, vault, logical, actual):
        self.vault, self.logical, self.actual = vault, logical, actual

    def get(self, reference):
        return self.vault.get(self.actual if reference == self.logical else reference)


class HostRouting:
    def __init__(self, manager):
        self.manager = manager

    def private_state(self, host_id, conn=None):
        from app.services.management_service import HOSTS
        if host_id not in HOSTS:
            raise ConfigurationError('Unknown host', 422)
        if conn is None:
            with self.manager.connect() as local:
                return self.private_state(host_id, local)
        row = conn.execute('SELECT version,data,pending FROM management_routing_profiles WHERE host_id=?', (host_id,)).fetchone()
        if not row:
            return {'host_id': host_id, 'version': 0, 'mode': 'direct', 'direct': None, 'pending': None}
        return {**json.loads(row['data']), 'host_id': host_id, 'version': row['version'], 'pending': json.loads(row['pending']) if row['pending'] else None}

    def state(self, host_id):
        value = self.private_state(host_id)
        result = {key: copy.deepcopy(value[key]) for key in ('host_id', 'version', 'mode', 'direct', 'endpoint', 'target', 'fallbacks', 'members', 'default_alias', *NUMERIC) if key in value}
        result.update(supported=host_id in HOST_ENDPOINTS, supported_endpoints=list(HOST_ENDPOINTS.get(host_id, ())), token_configured=bool(value.get('credential_hash')), host_loaded='not_verified', real_request='not_verified')
        result['gateway_url'] = value.get('gateway', {}).get('base_url')
        result['pending'] = None
        with self.manager.connect() as conn:
            if value.get('pending'):
                pending = value['pending']
                plan = conn.execute('SELECT status FROM management_plans WHERE id=?', (pending['plan_id'],)).fetchone()
                result['pending'] = {'plan_id': pending['plan_id'], 'status': plan['status'] if plan else 'recovery_required'}
            stale = []
            for entity_id, version in value.get('entity_versions', {}).items():
                row = conn.execute('SELECT version FROM management_entities WHERE id=?', (entity_id,)).fetchone()
                if not row or row['version'] != version:
                    stale.append(entity_id)
        result['definition_state'] = 'update_available' if stale else 'current'
        result['state'] = 'recovery_required' if result['pending'] else 'applied' if value.get('last_plan_id') else 'not_configured'
        result['changed_definition_count'] = len(stale)
        return result

    def _entry(self, entry, endpoint, versions):
        if not isinstance(entry, dict) or set(entry) - {'provider_id', 'model_id', 'alias'}:
            raise ConfigurationError('Choose a provider and saved model for each route', 422)
        provider = self.manager.get('provider', _identifier(entry.get('provider_id'), 'provider identifier'))
        model = self.manager.get('model', _identifier(entry.get('model_id'), 'saved model identifier'))
        if not provider.get('enabled') or model['provider_id'] != provider['id']:
            raise ConfigurationError('Route model must belong to the selected enabled provider', 422)
        required_protocol = 'anthropic' if endpoint == 'messages' else 'openai'
        if provider['protocol'] != required_protocol:
            raise ConfigurationError('Per-host routes require matching native protocols; conversion is a separate capability', 422)
        require_endpoint(provider, endpoint)
        versions[provider['id']], versions[model['id']] = provider['version'], model['version']
        from app.services.provider_credentials import binding
        versions.update(binding(self.manager, provider)['dependencies'])
        return {'provider_id': provider['id'], 'provider_name': provider['name'], 'model_id': model['id'], 'model_name': model['name'], 'native_model': model['model_id'], 'context_window': model.get('context_window')}

    def _target(self, host_id, body, current, origin):
        allowed = {'expected_version', 'mode', 'endpoint', 'target', 'fallbacks', 'members', 'default_alias', *NUMERIC}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ConfigurationError('Unsupported assistant connection setting', 422)
        if type(body.get('expected_version')) is not int or body['expected_version'] != current['version']:
            raise ConfigurationError('Assistant connection settings changed; reload before preview', 409)
        if current.get('pending'):
            raise ConfigurationError('Recover the pending assistant connection change first', 409)
        mode = body.get('mode')
        if mode not in ('routing', 'aggregation'):
            raise ConfigurationError('Choose routing or aggregation', 422)
        endpoint = body.get('endpoint')
        if endpoint not in HOST_ENDPOINTS.get(host_id, ()):
            raise ConfigurationError('This assistant does not support the selected native gateway API', 422)
        versions = {}
        target = {'host_id': host_id, 'mode': mode, 'endpoint': endpoint, 'direct': current.get('direct'), 'entity_versions': versions}
        for field, (default, low, high) in NUMERIC.items():
            value = body.get(field, default)
            if type(value) is not int or not low <= value <= high:
                raise ConfigurationError('Invalid ' + field, 422)
            target[field] = value
        if current['mode'] == 'direct':
            from app.services.selection_receipts import HEADER_SQL, selected_state
            with self.manager.connect() as conn:
                row = conn.execute(f'SELECT {HEADER_SQL} data FROM management_activations WHERE host_id=?', (host_id,)).fetchone()
                active = selected_state(conn, host_id, json.loads(row['data'])) if row else {}
            if active.get('selection_receipt_unknown'):
                raise ConfigurationError('Legacy selection receipt is incomplete; explicitly activate a model before enabling routing', 409)
            target['direct'] = {'provider_id': active.get('provider_id'), 'model_id': active.get('model_id')}
        if mode == 'routing':
            target['target'] = self._entry(body.get('target'), endpoint, versions)
            fallbacks = body.get('fallbacks', [])
            if not isinstance(fallbacks, list) or len(fallbacks) > 8:
                raise ConfigurationError('Choose up to eight explicitly allowed fallback routes', 422)
            target['fallbacks'] = [self._entry(entry, endpoint, versions) for entry in fallbacks]
            ids = [target['target']['provider_id'], *[row['provider_id'] for row in target['fallbacks']]]
            if len(set(ids)) != len(ids):
                raise ConfigurationError('Each fallback provider may appear only once', 422)
            aliases = {DEFAULT_ALIAS: target['target']}
            default_alias = DEFAULT_ALIAS
        else:
            members = body.get('members')
            if not isinstance(members, list) or not 1 <= len(members) <= 200:
                raise ConfigurationError('Choose between one and 200 aggregation aliases', 422)
            target['members'], aliases = [], {}
            for entry in members:
                resolved = self._entry(entry, endpoint, versions)
                alias = _identifier(entry.get('alias'), 'model alias')
                if alias in aliases:
                    raise ConfigurationError('Aggregation aliases must be unique', 422)
                aliases[alias] = resolved
                target['members'].append({**resolved, 'alias': alias})
            default_alias = body.get('default_alias')
            if not isinstance(default_alias, str) or default_alias not in aliases:
                raise ConfigurationError('Select another default before removing the default aggregation model', 422)
            if body.get('fallbacks') or body.get('target'):
                raise ConfigurationError('Aggregation dispatches one explicit alias without fallback', 422)
        target['default_alias'] = default_alias
        logical_ref = current.get('gateway', {}).get('credential_ref') or 'keychain:senrelay:' + uuid4().hex
        gateway_id = 'gateway_' + host_id.replace('-', '_')
        target['gateway'] = {'id': gateway_id, 'name': 'Senrelay local gateway', 'base_url': _origin(origin) + '/api/routing/v1/hosts/' + host_id, 'protocol': 'anthropic' if endpoint == 'messages' else 'openai', 'enabled': True, 'credential_ref': logical_ref,
                             'model_map': {alias: alias for alias in aliases}, 'model_context_windows': {alias: value['context_window'] for alias, value in aliases.items()}}
        if endpoint != 'messages':
            target['gateway'].update(wire_api='responses' if endpoint == 'responses' else 'chat', supported_endpoints=[endpoint])
        return target

    def preview(self, host_id, body, origin):
        current = self.private_state(host_id)
        if host_id not in HOST_ENDPOINTS:
            raise ConfigurationError('Native gateway configuration is unsupported for this assistant', 422)
        target = self._target(host_id, body, current, origin)
        specs, metadata = self.manager._configuration_specs({'host_id': host_id, 'provider_id': target['gateway']['id'], 'activate_provider': True, 'model_id': target['default_alias']}, provider_override=target['gateway'])
        for spec in specs:
            spec['activation_field'] = True
        metadata['entity_versions'].update(target['entity_versions'])
        metadata['host_routing'] = {'expected_version': current['version'], 'target': target}
        metadata['connection_change'] = {'before': current['mode'], 'after': target['mode'], 'gateway_url': target['gateway']['base_url'], 'upstream_keys_written_to_host': False, 'client_configuration_changed': current.get('gateway') != target['gateway'] or current.get('default_alias') != target['default_alias'], 'fallback': 'connect_or_429_only' if target['mode'] == 'routing' else 'none'}
        metadata['connection_change']['routes'] = ([{'alias': DEFAULT_ALIAS, **target['target']}, *target['fallbacks']] if target['mode'] == 'routing' else target['members'])
        metadata['connection_change']['default_alias'] = target['default_alias']
        return self.manager.transactions.create(specs, metadata)

    def direct_plan(self, request, current):
        host_id = current['host_id']
        if current.get('pending'):
            raise ConfigurationError('Recover the pending assistant connection change first', 409)
        direct = current.get('direct') or {}
        if request.get('operation') == 'restore' and not direct.get('provider_id'):
            plan = self.manager._restore_activation(host_id)
            metadata = plan['payload']['metadata']
        else:
            selected = request if request.get('activate_provider') is True else {'host_id': host_id, 'provider_id': direct['provider_id'], 'model_id': direct['model_id'], 'activate_provider': True}
            specs, metadata = self.manager._configuration_specs(selected)
            if host_id != 'claude-code':
                from app.services.management_service import HOSTS
                specs += self.manager._provider_specs(host_id, HOSTS[host_id], current['gateway'], 'uninstall', replacing_selected=True)
            plan = self.manager.transactions.create(specs, metadata)
        target = {'host_id': host_id, 'mode': 'direct', 'direct': {'provider_id': metadata.get('provider_id'), 'model_id': metadata.get('model_id')}, 'entity_versions': {}, 'credential_hash': None}
        metadata['host_routing'] = {'expected_version': current['version'], 'target': target}
        metadata['connection_change'] = {'before': current['mode'], 'after': 'direct', 'client_configuration_changed': True, 'gateway_credential_revoked': True}
        plan['payload']['metadata'] = metadata
        with self.manager.connect() as conn:
            conn.execute('UPDATE management_plans SET payload=? WHERE id=?', (canonical(plan['payload']), plan['id']))
        return plan

    def prepare(self, plan, version, confirmed):
        host_id = plan['payload']['metadata'].get('host_id')
        change = plan['payload']['metadata'].get('host_routing')
        current = self.private_state(host_id)
        if not change:
            if current.get('pending'):
                raise ConfigurationError('Recover the pending assistant connection change first', 409)
            return None
        if confirmed is not True or type(version) is not int or version != plan['version']:
            raise ConfigurationError('Explicit confirmation and current plan version are required', 422)
        if plan['status'] == 'applied':
            return None
        if plan['status'] != 'planned' or current.get('pending'):
            raise ConfigurationError('Recover the pending assistant connection change first', 409)
        if current['version'] != change['expected_version']:
            raise ConfigurationError('Assistant connection settings changed since preview', 409)
        target = change['target']
        pending = {'plan_id': plan['id'], 'new_ref': None, 'credential_hash': current.get('credential_hash'), 'credential_ref': current.get('gateway', {}).get('credential_ref')}
        if target['mode'] != 'direct' and not pending['credential_ref']:
            token = secrets.token_urlsafe(40)
            try:
                reference = self.manager.vault.put(token)
            except SecretUnavailable as exc:
                if exc.reference:
                    self._cleanup(exc.reference)
                raise
            pending.update(new_ref=reference, credential_hash=hashlib.sha256(token.encode()).hexdigest())
            pending['credential_ref'] = pending['new_ref']
        try:
            with self.manager.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('INSERT OR IGNORE INTO management_routing_profiles VALUES(?,0,?,NULL,?)', (host_id, canonical({'mode': 'direct', 'direct': None}), now_iso()))
                claimed = conn.execute('UPDATE management_routing_profiles SET pending=?,updated_at=? WHERE host_id=? AND version=? AND pending IS NULL', (canonical(pending), now_iso(), host_id, current['version']))
                if claimed.rowcount != 1:
                    raise ConfigurationError('Assistant connection settings changed before apply', 409)
        except Exception:
            if pending['new_ref']:
                self._cleanup(pending['new_ref'])
            raise
        return pending

    def apply_vault(self, plan, pending):
        target = plan['payload']['metadata']['host_routing']['target']
        return _ApplyVault(self.manager.vault, target['gateway']['credential_ref'], pending['credential_ref']) if target.get('gateway') else self.manager.vault

    def finalize(self, conn, plan):
        change = plan['payload']['metadata'].get('host_routing')
        if not change:
            return
        host_id = plan['payload']['metadata']['host_id']
        current = self.private_state(host_id, conn)
        pending = current.get('pending') or {}
        if current['version'] != change['expected_version'] or pending.get('plan_id') != plan['id']:
            raise ConfigurationError('Assistant connection claim changed during apply', 409)
        target = copy.deepcopy(change['target'])
        if target['mode'] != 'direct':
            target['credential_hash'] = pending['credential_hash']
            target['gateway']['credential_ref'] = pending['credential_ref']
        target['last_plan_id'] = plan['id']
        conn.execute('UPDATE management_routing_profiles SET version=version+1,data=?,pending=NULL,updated_at=? WHERE host_id=?', (canonical(target), now_iso(), host_id))
        if target['mode'] == 'direct' and current.get('gateway', {}).get('credential_ref'):
            self.manager._cleanup_receipt(conn, current['gateway']['credential_ref'], 'gateway_returned_to_direct')

    def _cleanup(self, reference):
        with self.manager.connect() as conn:
            receipt = self.manager._cleanup_receipt(conn, reference, 'gateway_change_not_adopted')
        self.manager.retry_credential_cleanup(receipt)

    def recover(self, plan_id, confirmed):
        if confirmed is not True:
            raise ConfigurationError('Explicit recovery confirmation is required', 422)
        plan = self.manager.transactions.get(plan_id)
        change = plan['payload']['metadata'].get('host_routing')
        if not change:
            return self.manager.transactions.recover(plan_id, confirmed)
        current = self.private_state(plan['payload']['metadata']['host_id'])
        pending = current.get('pending')
        if not pending or pending['plan_id'] != plan_id:
            if plan['status'] == 'recovered':
                return plan
            raise ConfigurationError('This plan has no pending connection recovery', 409)
        if plan['status'] in ('planned', 'conflict'):
            self.manager.transactions._save(plan, 'recovered')
            result = self.manager.transactions.get(plan_id)
        else:
            result = self.manager.transactions.recover(plan_id, confirmed)
        with self.manager.connect() as conn:
            conn.execute('UPDATE management_routing_profiles SET pending=NULL,updated_at=? WHERE host_id=? AND json_extract(pending,\'$.plan_id\')=?', (now_iso(), current['host_id'], plan_id))
            receipt = self.manager._cleanup_receipt(conn, pending['new_ref'], 'gateway_change_recovered') if pending.get('new_ref') else None
        if receipt:
            self.manager.retry_credential_cleanup(receipt)
        return result

    def runtime(self, host_id, authorization):
        value = self.private_state(host_id)
        token = authorization[7:] if isinstance(authorization, str) and authorization.startswith('Bearer ') else ''
        expected = value.get('credential_hash')
        if not token or len(token) > 256 or any(c.isspace() for c in token) or not expected or not hmac.compare_digest(expected, hashlib.sha256(token.encode()).hexdigest()):
            raise ConfigurationError('Invalid, revoked or mismatched assistant gateway credential', 401)
        if value.get('pending') or value['mode'] == 'direct':
            raise ConfigurationError('Assistant gateway is unavailable until connection recovery completes', 409)
        self.validate_references(value)
        return value

    def validate_references(self, value):
        versions = value['entity_versions']
        with self.manager.connect() as conn:
            rows = conn.execute('SELECT id,version FROM management_entities WHERE id IN (' + ','.join('?' for _ in versions) + ')', list(versions)).fetchall() if versions else []
        current = {row['id']: row['version'] for row in rows}
        if any(current.get(entity_id) != version for entity_id, version in versions.items()):
            raise ConfigurationError('Route definition changed; preview and apply the updated connection first', 409)

    @staticmethod
    def dispatch(value, model):
        if value['mode'] == 'routing':
            if model != DEFAULT_ALIAS:
                raise ConfigurationError('Select the configured assistant gateway model', 422)
            entries = [value['target'], *value['fallbacks']]
        else:
            entries = [entry for entry in value['members'] if entry['alias'] == model]
            if not entries:
                raise ConfigurationError('Unknown aggregation model alias', 422)
        ids = [entry['provider_id'] for entry in entries]
        return {'enabled': True, 'provider_id': ids[0], 'fallback_provider_ids': ids[1:], 'model_routes': {}, 'routing_strategy': 'ordered', '_scope': value['host_id'], '_models': {entry['provider_id']: entry['native_model'] for entry in entries}, **{key: value[key] for key in NUMERIC}}

    @staticmethod
    def assert_not_referenced(conn, entity_id):
        for row in conn.execute('SELECT data,pending FROM management_routing_profiles'):
            data = json.loads(row['data'])
            pending = json.loads(row['pending']) if row['pending'] else None
            if pending:
                plan = conn.execute("SELECT json_extract(payload,'$.metadata.entity_versions') versions FROM management_plans WHERE id=?", (pending['plan_id'],)).fetchone()
                if plan and entity_id in json.loads(plan['versions'] or '{}'):
                    raise ConfigurationError('Recover the pending assistant connection change before deleting a referenced definition', 409)
            if entity_id in data.get('entity_versions', {}) or (data.get('mode') != 'direct' and entity_id == (data.get('direct') or {}).get('provider_id')):
                raise ConfigurationError('Change the assistant route/default or return to direct before deleting a referenced definition', 409)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_settings'").fetchone():
            row = conn.execute("SELECT value FROM app_settings WHERE key='routing_v2_settings'").fetchone()
            if row and row[0]:
                try:
                    independent = json.loads(row[0])
                except (ValueError, TypeError):
                    independent = {}
                if isinstance(independent, dict):
                    fallbacks = independent.get('fallback_provider_ids') if isinstance(independent.get('fallback_provider_ids'), list) else []
                    routes = independent.get('model_routes') if isinstance(independent.get('model_routes'), dict) else {}
                    routed_ids = [pid for items in routes.values() if isinstance(items, list) for pid in items]
                    if entity_id == independent.get('provider_id') or entity_id in fallbacks or entity_id in routed_ids:
                        raise ConfigurationError('Change or clear the independent client routing provider, fallbacks or model routes before deleting a referenced definition', 409)
