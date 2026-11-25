"""Remove verified owned provider entries without reconstructing old secrets."""
import json
import tomllib

from app.integrations.config_transactions import ConfigurationError


def _document(manager, relative, format):
    path = manager.transactions.safe_path(manager.home / relative)
    raw = manager.transactions._read(path)
    if raw is None:
        return {}
    if len(raw) > 4 * 1024 * 1024:
        raise ConfigurationError('Native selection document exceeds the read limit', 422)
    try:
        if format in ('yaml', 'json5'):
            from app.integrations.structured_config import parse_document
            value = parse_document(format, raw.decode())
        else:
            value = tomllib.loads(raw.decode()) if format == 'toml' else json.loads(raw)
    except (ValueError, UnicodeError):
        raise ConfigurationError('Native selection cannot be read safely; configuration was preserved', 422) from None
    if not isinstance(value, dict):
        raise ConfigurationError('Native selection root must be an object', 422)
    return value


def _guard_references(manager, host_id, host, provider_id, replacing_selected):
    selected = False
    if host_id in ('codex',):
        document = _document(manager, host['mcp_path'], 'toml')
        selected = document.get('model_provider') == 'senrelay_' + provider_id.replace('provider_', '')
    elif host_id == 'opencode':
        document = _document(manager, host['provider_path'], 'json')
        selected = str(document.get('model', '')).startswith(provider_id + '/')
    elif host_id == 'omp':
        document = _document(manager, host['selection_path'], 'yaml')
        prefix = provider_id + '/'
        def references(value):
            if isinstance(value, str):
                return any(part.strip().startswith(prefix) for part in value.split(','))
            if isinstance(value, dict):
                return any(references(key) or references(item) for key, item in value.items())
            if isinstance(value, list):
                return any(references(item) for item in value)
            return False
        # Do not orphan non-default roles, explicit fallbacks or task overrides.
        # These settings are preserved rather than silently cleared on uninstall.
        for key in ('modelRoles', 'retry', 'task', 'modelPresets', 'enabledModels', 'cycleOrder'):
            if references(document.get(key)):
                raise ConfigurationError('Change or restore OMP model roles, fallbacks and task/preset selectors before uninstalling their provider', 409)
    if selected and not replacing_selected:
        raise ConfigurationError('Switch or restore the current model selection before uninstalling its provider', 409)


def owned_specs(manager, host_id, host, provider_id, *, replacing_selected=False):
    from app.services.management_experience import _spec
    paths = [str(manager.home / host['provider_path'])] if host.get('provider_path') else []
    if host_id in ('codex',):
        paths.append(str(manager.home / host['mcp_path']))
    if not paths:
        raise ConfigurationError(host.get('provider_unsupported_reason', 'Provider configuration format is unsupported for this host'), 422)
    _guard_references(manager, host_id, host, provider_id, replacing_selected)
    from app.services.selection_receipts import HEADER_SQL, selected_state
    with manager.connect() as conn:
        row = conn.execute(f'SELECT {HEADER_SQL} data FROM management_activations WHERE host_id=?', (host_id,)).fetchone()
        active = selected_state(conn, host_id, json.loads(row['data'])) if row else {}
        if active.get('provider_id') == provider_id and not replacing_selected:
            raise ConfigurationError('Switch or restore the active model selection before uninstalling its provider', 409)
        rows = conn.execute('SELECT * FROM management_owned WHERE owner=? AND path IN (' + ','.join('?' for _ in paths) + ') ORDER BY path,item_key', [provider_id, *paths]).fetchall()
    if not rows:
        raise ConfigurationError('Only installed Senrelay-owned provider entries may be uninstalled', 409)
    return [{**_spec(row, set()), 'value': None} for row in rows]
