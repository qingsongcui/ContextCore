"""Read one selected host's public configuration and copy approved definitions into Senrelay."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.integrations.config_transactions import ConfigurationError, canonical, redact, reject_secrets
from app.services.management_service import HOSTS, configuration_locked


APIS = {
    'openai-responses': ('openai', 'responses'), '@ai-sdk/openai': ('openai', 'responses'),
    'codex_responses': ('openai', 'responses'), 'openai_responses': ('openai', 'responses'),
    'openai-completions': ('openai', 'chat'), '@ai-sdk/openai-compatible': ('openai', 'chat'),
    'chat_completions': ('openai', 'chat'), 'openai': ('openai', 'chat'),
    'anthropic-messages': ('anthropic', None), '@ai-sdk/anthropic': ('anthropic', None),
    'anthropic_messages': ('anthropic', None), 'anthropic': ('anthropic', None),
    'google-generative-ai': ('gemini', None),
}
KINDS = {'providers': 'provider_path', 'mcp': 'mcp_path', 'skills': 'skill_dir', 'rules': 'prompt_path'}
MAX_ITEMS = 500
MAX_BYTES = 32 * 1024 * 1024
MAX_DIR_ENTRIES = 4096


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise ConfigurationError('Invalid native name or model identifier', 422)
    reject_secrets(value)
    if 'keychain:senrelay:' in value:
        raise ConfigurationError('Local credential references cannot be copied', 422)
    return value.strip()


def _private_strings(config, *, mcp=False):
    # Withhold an opaque token repeated in another field (even when the
    # endpoint/model/name would otherwise pass ordinary syntax validation).
    found = set()
    def walk(value, private=False):
        if isinstance(value, dict):
            for key, item in value.items():
                marked = re.search(r'auth|api[_-]?key|token|secret|password|cookie|headers', str(key), re.I)
                walk(item, private or bool(marked) or (mcp and key in ('env', 'environment')))
        elif isinstance(value, list):
            for item in value:
                walk(item, private)
        elif private and isinstance(value, str) and value:
            found.add(value)
            if value.lower().startswith('bearer ') and value[7:]:
                found.add(value[7:])
    walk(config)
    return found


def _read(path, limit):
    # Follow neither directory aliases nor a final symlink, even if the path
    # changes between inspection and open. Never open FIFOs in blocking mode.
    dirflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open('/', dirflags)
    try:
        for component in path.parent.parts[1:]:
            parent = os.open(component, dirflags, dir_fd=directory)
            os.close(directory)
            directory = parent
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'rb') as reader:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                raise ConfigurationError('Native source must be a bounded regular file', 422)
            raw = reader.read(limit + 1)
            after = os.fstat(fd)
            if len(raw) > limit or before.st_mtime_ns != after.st_mtime_ns or before.st_size != after.st_size:
                raise ConfigurationError('Native source changed during reading', 409)
            return raw
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigurationError('Native source is unreadable or linked', 422) from None
    finally:
        os.close(directory)


def _document(raw, fmt):
    try:
        text = raw.decode('utf-8')
        if fmt in ('yaml', 'json5'):
            from app.integrations.structured_config import parse_document
            doc = parse_document(fmt, text)
        elif fmt == 'toml':
            doc = tomllib.loads(text)
        else:
            doc = json.loads(text)
        if not isinstance(doc, dict):
            raise ValueError
        return doc
    except ConfigurationError as exc:
        if exc.status_code == 503:
            raise
        raise ConfigurationError('The selected native configuration could not be parsed', 422) from None
    except (UnicodeError, ValueError, RecursionError):
        raise ConfigurationError('The selected native configuration could not be parsed', 422) from None


def _mapping(value):
    if not isinstance(value, dict):
        raise ConfigurationError('Unexpected native configuration structure', 422)
    return value


def _native_selection(host, doc, preferences):
    if host == 'pi':
        return preferences.get('defaultProvider'), preferences.get('defaultModel')
    if host == 'hermes' and isinstance(doc.get('model'), dict):
        return doc['model'].get('provider'), doc['model'].get('default')
    if host == 'kimi-code':
        selected = _mapping(doc.get('models', {})).get(doc.get('default_model'))
        if isinstance(selected, dict):
            return selected.get('provider'), selected.get('model')
    composite = None
    if host == 'omp':
        composite = _mapping(preferences.get('modelRoles', {})).get('default')
    elif host == 'opencode':
        composite = doc.get('model')
    elif host == 'minimax-code':
        raw = doc.get('defaultModel')
        if isinstance(raw, str) and raw.startswith('custom_provider:'):
            composite = raw.removeprefix('custom_provider:')
    elif host == 'openclaw':
        choice = _mapping(_mapping(doc.get('agents', {})).get('defaults', {})).get('model')
        composite = choice.get('primary') if isinstance(choice, dict) else choice
    if isinstance(composite, str) and '/' in composite:
        return composite.split('/', 1)
    return None, None


def _providers(host, doc, preferences):
    if host in ('codex', 'codex-desktop'):
        for key, data in _mapping(doc.get('model_providers', {})).items():
            data = _mapping(data)
            wire = {'responses': 'openai-responses', 'chat': 'openai-completions'}.get(data.get('wire_api', 'responses'))
            selected = doc.get('model') if key == doc.get('model_provider') else None
            yield key, data, data.get('base_url'), wire, [], selected
    elif host == 'claude-code':
        env = _mapping(doc.get('env', {}))
        if env.get('ANTHROPIC_BASE_URL') or env.get('ANTHROPIC_MODEL') or doc.get('model'):
            models = [env[k] for k in ('ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL') if env.get(k)]
            base = env.get('ANTHROPIC_BASE_URL', 'https://api.anthropic.com')
            if isinstance(base, str):
                base = base.rstrip('/').removesuffix('/v1') + '/v1'
            yield 'Claude Code', env, base, 'anthropic', models, env.get('ANTHROPIC_MODEL') or doc.get('model')
    elif host in ('hermes', 'opencode', 'minimax-code', 'openclaw', 'omp', 'pi', 'kimi-code'):
        if host == 'hermes':
            rows = doc.get('custom_providers', [])
            if not isinstance(rows, list):
                raise ConfigurationError('Unexpected native provider list', 422)
            entries = [(_mapping(entry).get('name'), entry) for entry in rows]
        else:
            container = {'opencode': 'provider', 'minimax-code': 'custom_provider'}.get(host, 'providers')
            root = _mapping(doc.get('models', {})) if host == 'openclaw' else doc
            entries = _mapping(root.get(container, {})).items()
        selected_provider, selected_model = _native_selection(host, doc, preferences)
        for key, data in entries:
            data = _mapping(data)
            options = _mapping(data.get('options', {}))
            base = options.get('baseURL') or data.get('baseUrl') or data.get('base_url')
            api = data.get('api') or data.get('api_mode') or data.get('npm') or data.get('type')
            models = data.get('models') or []
            windows = {}
            if host == 'kimi-code':
                entries = [item for item in _mapping(doc.get('models', {})).values() if isinstance(item, dict) and item.get('provider') == key]
                models = [item.get('model') for item in entries]
                windows = {item['model']: item['max_context_size'] for item in entries if isinstance(item.get('model'), str)
                           and type(item.get('max_context_size')) is int and 0 < item['max_context_size'] <= 9223372036854775807}
            elif isinstance(models, dict):
                models = list(models)
            elif isinstance(models, list):
                windows = {m['id']: m['contextWindow'] for m in models if isinstance(m, dict) and isinstance(m.get('id'), str)
                           and type(m.get('contextWindow')) is int and 0 < m['contextWindow'] <= 9007199254740991}
                models = [m.get('id') if isinstance(m, dict) else m for m in models]
            else:
                models = []
            yield key, data, base, api, models, selected_model if selected_provider == key else None, windows


def _provider_entry(manager, name, config, base, api, models, selected, windows=None):
    if api not in APIS:
        raise ConfigurationError('This native API needs manual configuration', 422)
    protocol, wire = APIS[api]
    models = list(dict.fromkeys(_label(m) for m in [*models, selected] if m))
    if len(models) > 200:
        raise ConfigurationError('Native provider has too many models', 422)
    endpoints = manager._endpoint(base)
    definition = {'name': _label(config.get('name') or name), 'base_url': endpoints, 'protocol': protocol,
                  'model_map': {m: m for m in models}}
    if windows:
        definition['model_context_windows'] = windows
    if wire:
        definition['wire_api'] = wire
    if selected:
        definition['default_model'] = _label(selected)
    sensitive = re.compile(r'key|token|auth|password|secret|headers', re.I)
    if any(sensitive.search(key) and bool(value) for fields in (config, config.get('options') or {}) for key, value in fields.items()):
        definition['credential_reattach_required'] = True
    return definition


def _mcp(config):
    conf = _mapping(config)
    if conf.get('url') or conf.get('serverUrl') or conf.get('httpUrl'):
        result = {'transport': 'sse' if conf.get('type') == 'sse' else 'http',
                  'url': conf.get('url') or conf.get('serverUrl') or conf.get('httpUrl')}
    else:
        command = conf.get('command')
        args = conf.get('args', [])
        if isinstance(command, list) and command:
            command, args = command[0], command[1:]
        result = {'transport': 'stdio', 'command': command, 'args': args}
        if conf.get('cwd'):
            result['cwd'] = conf['cwd']
    # Native environment/header values can be opaque tokens. Withhold these
    # and block application until the shared definition has been reviewed.
    needs_review = any(conf.get(key) for key in ('env', 'environment', 'headers', 'http_headers', 'env_http_headers',
                                                 'bearer_token_env_var', 'oauth'))
    return {'kind': 'mcp', 'config': result, **({'native_review_required': True} if needs_review else {})}


def _children(path):
    try:
        with os.scandir(path) as scan:
            names = []
            for entry in scan:
                if len(names) >= MAX_DIR_ENTRIES:
                    raise ConfigurationError('Native directory exceeds the inspection limit', 422)
                names.append(entry.name)
        return sorted(names)
    except OSError:
        raise ConfigurationError('Native directory is unreadable', 422) from None


def _skill(manager, root):
    stack, files, total, nodes = [root], {}, 0, 0
    while stack:
        folder = stack.pop()
        manager.transactions.safe_path(str(folder), directory=True)
        for name in _children(folder):
            path = folder / name
            nodes += 1
            if nodes > 400 or path.is_symlink():
                raise ConfigurationError('Linked or oversized Skill needs manual review', 422)
            if path.is_dir():
                stack.append(path)
                continue
            raw = _read(path, 500000)
            if raw is None:
                raise ConfigurationError('Native Skill changed during inspection', 409)
            total += len(raw)
            if total > 2 * 1024 * 1024 or len(files) >= 100:
                raise ConfigurationError('Native Skill exceeds the supported size', 422)
            text = raw.decode()
            if '\x00' in text:
                raise ConfigurationError('Native Skill contains a binary file', 422)
            # Credential literals are never adopted, including internal placeholders.
            text = re.sub(r'(?i)(?:authorization|x-api-key)\s*[:=]\s*[^\n]+|--(?:api[-_]key|token|secret|password|auth)(?:[=\s]+)[^\s]+', '[REDACTED]', text)
            files[path.relative_to(root).as_posix()] = redact(text)
    return {'kind': 'skill', 'install_mode': 'copy', 'files': files}


def _collect(manager, host_id, kind):
    if host_id not in HOSTS or kind not in KINDS:
        raise ConfigurationError('Choose an existing assistant and native resource kind', 422)
    host = manager.host_config(host_id)
    relative = host.get(KINDS[kind])
    if kind == 'providers' and host_id == 'codex':
        relative = host['mcp_path']
    if kind == 'rules' and not relative:
        relative = host.get('prompt_dir') if host.get('prompt_mode') == 'global_rule' else None
    header = {'host_id': host_id, 'kind': kind, 'source_path': str(manager.home / relative) if relative else None,
              'status': 'unsupported', 'credentials_copied': False, 'host_files_changed': False}
    entries, private, origins, evidence = [], {}, {}, []
    if not relative:
        return header, entries, private, origins
    target = manager.home / relative
    is_dir = kind == 'skills' or (kind == 'rules' and not host.get('prompt_path'))
    manager.transactions.safe_path(str(target), directory=is_dir)

    def add(key, make, opaque=(), *, native=None):
        if len(entries) >= MAX_ITEMS:
            raise ConfigurationError('Native source exceeds 500 entries', 422)
        ident = _digest([host_id, kind, relative, key])[:32]
        if any(item['id'] == ident for item in entries):
            raise ConfigurationError('Ambiguous native identity', 422)
        entry = {'id': ident, 'name': 'Unrecognized entry', 'available': False, 'summary': ''}
        try:
            name = _label(key)
            if any(secret == name or (len(secret) >= 6 and secret in name) for secret in opaque):
                raise ConfigurationError('Credential repeated in a native name', 422)
            entry['name'] = name
            definition = make()
            data = manager._validate('provider' if kind == 'providers' else 'asset', {
                'name': name, **definition, 'agent_ids': [host_id], 'source': {'type': 'host_config', 'host_id': host_id, 'path': str(target)}})
            serialized = canonical(data)
            if any(json.dumps(secret, ensure_ascii=False) in serialized or (len(secret) >= 6 and secret in serialized) for secret in opaque):
                raise ConfigurationError('Credential repeated in native metadata', 422)
            if 'keychain:senrelay:' in serialized:
                raise ConfigurationError('Native credential references cannot be borrowed', 422)
            payload_size = len(canonical(data).encode())
            if sum(size for _, size in evidence) + payload_size > MAX_BYTES:
                raise ConfigurationError('Native preview exceeds 32 MiB; select a smaller source', 413)
            private[ident] = data
            if kind != 'providers':
                if kind == 'mcp':
                    digest = _digest(native)
                    # Only adopt entries that our renderer can account for fully.
                    portable = not data.get('native_review_required') and not set(native) - {
                        'command','args','url','serverUrl','httpUrl','type','transport','enabled','cwd'}
                elif kind == 'rules':
                    digest = hashlib.sha256(data['content'].encode()).hexdigest()
                    portable = True
                else:
                    digest, portable = _digest(data['files']), False
                origins[ident] = ((relative, key), digest, portable)
            evidence.append((_digest(data), payload_size))
            entry.update(name=data['name'], available=True)
            if kind == 'providers':
                entry.update(base_url=data['base_url'], protocol=data['protocol'], model_ids=list(data['model_map'].values()),
                             requires_credential=data.get('credential_reattach_required', False), summary=data['base_url'])
            elif kind == 'mcp':
                entry.update(summary=data['config']['transport'], needs_review=bool(data.get('native_review_required')))
            elif kind == 'skills':
                entry.update(summary=f"{len(data['files'])} files", file_count=len(data['files']))
            else:
                entry['summary'] = Path(relative).name
        except ConfigurationError as exc:
            if exc.status_code == 413:
                raise
            entry['reason'] = 'manual_review_required'
        except (ValueError, OSError, TypeError, UnicodeError):
            entry['reason'] = 'manual_review_required'
        entries.append(entry)

    if is_dir:
        if not target.exists():
            return {**header, 'status': 'not_found'}, entries, private, origins
        for name in _children(target):
            item = target / name
            if name.startswith('.') or re.fullmatch(r'asset[-_][0-9a-f]{16}', name):
                continue
            if kind == 'skills':
                if item.is_symlink() or (item.is_dir() and (item / 'SKILL.md').exists()):
                    add(name, lambda: _skill(manager, item))
            elif name.endswith('.md'):
                add(name, lambda: {'kind': 'prompt', 'content': (_read(item, 500000) or b'').decode()})
    else:
        raw = _read(target, 4 * 1024 * 1024)
        if raw is None:
            return {**header, 'status': 'not_found'}, entries, private, origins
        evidence.append((hashlib.sha256(raw).hexdigest(), 0))
        if kind == 'rules':
            add(target.name, lambda: {'kind': 'prompt', 'content': raw.decode()})
        else:
            doc = _document(raw, host.get('provider_format', host.get('format')) if kind == 'providers' else host.get('format'))
            if kind == 'providers':
                preferences = {}
                companion = host.get('selection_path') if host_id == 'omp' else '.pi/agent/settings.json' if host_id == 'pi' else None
                if companion:
                    extra = _read(manager.home / companion, 4 * 1024 * 1024)
                    evidence.append((_digest(companion) if extra is None else hashlib.sha256(extra).hexdigest(), 0))
                    if extra is not None:
                        preferences = _document(extra, 'yaml' if host_id == 'omp' else 'json5')
                for values in _providers(host_id, doc, preferences):
                    if isinstance(values[0], str) and re.fullmatch(r'(?:provider|senrelay)_[0-9a-f]{16}', values[0]):
                        continue
                    add(values[0], lambda: _provider_entry(manager, *values), _private_strings(values[1]))
            else:
                key = host.get('container', 'mcp_servers')
                for name, config in _mapping(doc.get(key, {})).items():
                    if not re.fullmatch(r'(?:asset|senrelay)_[0-9a-f]{16}', name) and name != 'contextcore':
                        add(name, lambda: _mcp(config), _private_strings(config, mcp=True), native=config)
    return {**header, 'status': 'ready', 'fingerprint': _digest([host_id, kind, evidence, entries])}, entries, private, origins


def _match_import(conn, key, kind, data, origin):
    """Resolve old host-scoped import IDs to the same physical resource across surfaces."""
    keys = {key: data['source']['host_id']}
    if origin:
        (relative, name), _, _ = origin
        keys.update({_digest([host, kind, relative, name])[:32]: host for host in HOSTS})
    placeholders = ','.join('?' for _ in keys)
    rows = conn.execute(f"""SELECT i.source_key,i.definition_hash,e.id,e.kind,e.version,e.data
        FROM management_native_imports i JOIN management_entities e ON e.id=i.entity_id
        WHERE i.source_key IN ({placeholders})""", tuple(keys)).fetchall()
    prior = next((row for row in rows if row['source_key'] == key), None)
    if prior is None and rows:
        if len(rows) > 1:
            raise ConfigurationError('Multiple saved definitions match this native source; review them in the library', 409)
        prior = rows[0]
        # Keep the original source and membership for fingerprints. Opening a second
        # surface must not create a version, replace user edits or move the asset.
        source_host = keys[prior['source_key']]
        data = {**data, 'agent_ids': [source_host], 'source': {**data['source'], 'host_id': source_host}}
    return prior, data


def _disposition(prior, data):
    if prior is None:
        return 'create'
    if prior['definition_hash'] == _digest(data):
        return 'unchanged'
    # Preserve independent edits to the shared definition. Only an unchanged
    # previously imported definition can be replaced by a native revision.
    return 'update' if prior['definition_hash'] == _digest(json.loads(prior['data'])) else 'conflict'


def preview(manager, host_id, kind):
    result, entries, private, origins = _collect(manager, host_id, kind)
    with manager.connect() as conn:
        for item in entries:
            if item['available']:
                key = item['id']
                prior, data = _match_import(conn, key, kind, private[key], origins.get(key))
                action = _disposition(prior, data)
                item['disposition'] = action
                if action == 'conflict':
                    item.update(available=False, reason='definition_edited')
    return {**result, 'candidates': entries, 'fingerprint': result.get('fingerprint')}


@configuration_locked
def import_selection(manager, host_id, body):
    selected = body.get('selected_ids')
    if body.get('confirmed') is not True or not isinstance(selected, list) or not selected or len(selected) > MAX_ITEMS:
        raise ConfigurationError('Confirm a nonempty native selection', 422)
    if any(not isinstance(key, str) or not re.fullmatch('[a-f0-9]{32}', key) for key in selected) or len(set(selected)) != len(selected):
        raise ConfigurationError('Invalid or duplicate native selection', 422)
    result, _, private, origins = _collect(manager, host_id, body.get('kind'))
    if not result.get('fingerprint') or body.get('fingerprint') != result['fingerprint']:
        raise ConfigurationError('Native source changed; preview again', 409)
    if any(key not in private for key in selected):
        raise ConfigurationError('Selected native definition requires manual review', 422)
    entities, imported, unchanged = [], 0, 0
    now = datetime.now(timezone.utc).isoformat()
    with manager.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        for key in selected:
            data = private[key]
            kind = 'provider' if body['kind'] == 'providers' else 'asset'
            prior, data = _match_import(conn, key, body['kind'], data, origins.get(key))
            action = _disposition(prior, data)
            if action == 'conflict':
                raise ConfigurationError('Native source and saved definition both changed; review your edits', 409)
            if action == 'unchanged':
                if kind == 'asset':
                    from app.services import native_asset_origin
                    native_asset_origin.record(conn, prior['id'], *origins[key])
                unchanged += 1
                entities.append({'id': prior['id'], 'name': json.loads(prior['data'])['name'], 'kind': prior['kind']})
                continue
            if action == 'create':
                entity_id, version = kind + '_' + uuid4().hex[:16], 1
                conn.execute('INSERT INTO management_entities VALUES(?,?,?,1,?,?)', (entity_id, kind, canonical(data), now, now))
            else:
                entity_id, version = prior['id'], prior['version'] + 1
                conn.execute('UPDATE management_entities SET data=?,version=?,updated_at=? WHERE id=?',
                             (canonical(data), version, now, entity_id))
            if kind == 'asset':
                from app.services import native_asset_origin
                native_asset_origin.record(conn, entity_id, *origins[key])
                conn.execute('INSERT INTO management_asset_versions VALUES(?,?,?,?)', (entity_id, version, canonical(data), now))
            else:
                models = {row[0] for row in conn.execute("""SELECT json_extract(data,'$.model_id') FROM management_entities
                    WHERE kind='model' AND json_extract(data,'$.provider_id')=?""", (entity_id,))}
                for model in data['model_map'].values():
                    if model in models:
                        continue
                    model_data = {'name': model, 'model_id': model, 'provider_id': entity_id}
                    if model in data.get('model_context_windows', {}):
                        model_data['context_window'] = data['model_context_windows'][model]
                    conn.execute('INSERT INTO management_entities VALUES(?,?,?,1,?,?)',
                                 ('model_' + uuid4().hex[:16], 'model', canonical(model_data), now, now))
            conn.execute('INSERT OR REPLACE INTO management_native_imports VALUES(?,?,?)', (prior['source_key'] if prior else key, entity_id, _digest(data)))
            imported += 1
            entities.append({'id': entity_id, 'name': data['name'], 'kind': kind})
        conn.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)', ('aud_'+uuid4().hex, 'user', 'native_import', host_id, 'success', now))
    return {'host_id': host_id, 'kind': body['kind'], 'imported': imported, 'unchanged': unchanged, 'entities': entities, 'host_files_changed': False}
