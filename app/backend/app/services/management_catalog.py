"""Bounded, scoped summaries for management lists; full bodies load on demand."""
from __future__ import annotations

import json

from app.integrations.config_transactions import ConfigurationError
from app.services.management_service import ENTITY_KINDS, HOSTS
from app.services.provider_credentials import account_status


def _owned_scope(manager, host_id, alias):
    from app.integrations.host_management_native import effective_host, configuration_paths
    host = effective_host(HOSTS[host_id], manager.home)
    paths = configuration_paths(host, manager.home)
    if host_id in ('codex', 'codex-desktop'):
        paths.append(str(manager.home / '.codex/config.toml'))
    prefix = str(manager.home / host['skill_dir']) + '/' if host.get('skill_dir') else '\x00'
    prompt_prefix = str(manager.home / host['prompt_dir']) + '/' if host.get('prompt_dir') else '\x00'
    path_sql = ('o.path IN (' + ','.join('?' for _ in paths) + ') OR ' if paths else '') + 'substr(o.path,1,?)=? OR substr(o.path,1,?)=?'
    return (f"EXISTS (SELECT 1 FROM management_owned o WHERE o.owner={alias}.id AND ({path_sql})) OR "
            f"EXISTS (SELECT 1 FROM management_activations a WHERE a.host_id=? AND json_extract(a.data,'$.provider_id')={alias}.id)",
            [*paths, len(prefix), prefix, len(prompt_prefix), prompt_prefix, host_id])


def _assigned(manager, host_id, kind, alias='e'):
    if kind == 'binding':
        return f"json_extract({alias}.data,'$.host_id')=?", [host_id]
    if kind == 'model':
        expression, params = _assigned(manager, host_id, 'provider', 'p')
        return f"EXISTS (SELECT 1 FROM management_entities p WHERE p.kind='provider' AND p.id=json_extract({alias}.data,'$.provider_id') AND ({expression}))", params
    # An explicit [] means deliberately unassigned; only legacy providers infer
    # membership from old model bindings. Native owned entries stay discoverable.
    expression = (f"CASE WHEN json_type({alias}.data,'$.agent_ids')='array' THEN "
                  f"EXISTS(SELECT 1 FROM json_each({alias}.data,'$.agent_ids') WHERE value=?) ELSE "
                  f"EXISTS(SELECT 1 FROM json_each({alias}.data,'$.host_ids') WHERE value=?)")
    params = [host_id, host_id]
    if kind == 'provider':
        expression += (f" OR EXISTS(SELECT 1 FROM management_entities b WHERE b.kind='binding' AND "
                       f"json_extract(b.data,'$.provider_id')={alias}.id AND json_extract(b.data,'$.host_id')=?)")
        params.append(host_id)
    expression += ' END'
    installed, installed_params = _owned_scope(manager, host_id, alias)
    return '(' + expression + ') OR (' + installed + ')', params + installed_params


# Only these bounded scalar fields and small scope arrays enter list responses.
# No config/env/header bodies, Skill files, prompts, model maps or secret refs.
TEXT_FIELDS = {'name': 200, 'kind': 80, 'protocol': 40, 'wire_api': 20, 'credential_source': 20, 'base_url': 1024,
               'provider_id': 100, 'account_id': 100, 'model_id': 200,
               'host_id': 100, 'project_id': 100, 'install_mode': 40, 'status': 80}
SOURCE_FIELDS = {'type': 80, 'url': 1024, 'path': 1024, 'ref': 120, 'manifest_url': 1024}


def catalog_page(manager, kind, *, agent_id=None, resource_kind=None, provider_id=None,
                 query='', scope='assigned', limit=50, offset=0, entity_ids=None, include_unbound=False):
    if kind not in ENTITY_KINDS or (agent_id is not None and agent_id not in HOSTS):
        raise ConfigurationError('Unknown management kind or assistant', 422)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ConfigurationError('Use a page size of 1–100 and a nonnegative offset', 422)
    if not isinstance(query, str) or len(query) > 200 or scope not in ('assigned', 'available', 'all'):
        raise ConfigurationError('Invalid management search or scope', 422)
    if scope == 'available' and agent_id is None:
        raise ConfigurationError('Select an assistant before browsing available resources', 422)
    if resource_kind is not None and (kind != 'asset' or resource_kind not in ('mcp', 'skill', 'prompt')):
        raise ConfigurationError('Choose a supported asset kind', 422)
    where, params = ['e.kind=?'], [kind]
    if entity_ids is not None:
        if not isinstance(entity_ids, list) or len(entity_ids) > 100 or any(not isinstance(value, str) or not value or len(value) > 100 for value in entity_ids):
            raise ConfigurationError('Select at most 100 resource identifiers', 422)
        if entity_ids:
            where.append('e.id IN (' + ','.join('?' for _ in entity_ids) + ')')
            params.extend(entity_ids)
        else:
            where.append('0')
    if resource_kind:
        where.append("json_extract(e.data,'$.kind')=?")
        params.append(resource_kind)
    if type(include_unbound) is not bool or (include_unbound and kind != 'account'):
        raise ConfigurationError('Unbound provider choices apply only to accounts', 422)
    if provider_id is not None:
        if kind not in ('model', 'account', 'binding'):
            raise ConfigurationError('Provider filtering is unsupported for this collection', 422)
        where.append("(json_extract(e.data,'$.provider_id')=? OR coalesce(json_extract(e.data,'$.provider_id'),'')='')" if include_unbound else "json_extract(e.data,'$.provider_id')=?")
        params.append(provider_id)
    if agent_id and scope != 'all':
        expression, values = _assigned(manager, agent_id, kind)
        where.append(('NOT ' if scope == 'available' else '') + '(' + expression + ')')
        params += values
    if query.strip():
        pattern = '%' + query.strip().casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        where.append("(" + ' OR '.join(f"catalog_casefold(json_extract(e.data,'$.{field}')) LIKE ? ESCAPE '\\'" for field in ('name', 'base_url', 'model_id', 'source.url', 'source.path')) + ")")
        params += [pattern] * 5
    fields = ["e.id", "e.version", "e.created_at", "e.updated_at"]
    fields += [f"substr(json_extract(e.data,'$.{name}'),1,{size}) AS {name}" for name, size in TEXT_FIELDS.items()]
    fields += [f"substr(json_extract(e.data,'$.source.{name}'),1,{size}) AS source_{name}" for name, size in SOURCE_FIELDS.items()]
    known_hosts = ','.join("'" + host.replace("'", "''") + "'" for host in HOSTS)
    fields += [f"CASE WHEN json_type(e.data,'$.{name}')='array' THEN (SELECT json_group_array(DISTINCT value) FROM json_each(e.data,'$.{name}') WHERE value IN ({known_hosts})) ELSE NULL END AS {name}" for name in ('agent_ids', 'host_ids', 'cc_switch_current_hosts')]
    fields += ["CASE WHEN json_type(e.data,'$.supported_endpoints')='array' THEN (SELECT json_group_array(DISTINCT value) FROM json_each(e.data,'$.supported_endpoints') WHERE value IN ('chat/completions','responses','messages','generateContent')) ELSE NULL END AS supported_endpoints"]
    fields += ["CASE WHEN e.kind='provider' THEN substr(coalesce("
               "json_extract(e.data,'$.default_model'), "
               "json_extract(e.data,'$.model_map.default'), "
               "(SELECT value FROM json_each(CASE WHEN json_type(e.data,'$.model_map')='object' THEN json_extract(e.data,'$.model_map') ELSE '{}' END) WHERE typeof(value)='text' LIMIT 1), "
               "(SELECT json_extract(m.data,'$.model_id') FROM management_entities m WHERE m.kind='model' AND json_extract(m.data,'$.provider_id')=e.id ORDER BY m.created_at, m.id LIMIT 1)"
               "),1,200) ELSE NULL END AS default_model"]
    fields += ["CASE WHEN json_type(e.data,'$.context_window')='integer' THEN json_extract(e.data,'$.context_window') ELSE NULL END AS context_window",
               "CASE WHEN json_type(e.data,'$.sort_index')='integer' THEN json_extract(e.data,'$.sort_index') ELSE NULL END AS sort_index",
               "json_extract(e.data,'$.enabled') AS enabled", "json_extract(e.data,'$.credential_reattach_required') AS credential_reattach_required", "json_extract(e.data,'$.credential_ref') IS NOT NULL AS has_credential"]
    fields += ["(SELECT substr(json_extract(a.data,'$.name'),1,200) FROM management_entities a WHERE a.kind='account' AND a.id=json_extract(e.data,'$.account_id')) AS account_name"]
    clause = ' AND '.join(where)
    with manager.connect() as conn:
        conn.create_function('catalog_casefold', 1, lambda value: str(value).casefold() if value is not None else '')
        conn.execute('BEGIN')
        if kind == 'account':
            for name in ('expires_at', 'authorization_expires_at'):
                fields.append(f"json_type(e.data,'$.{name}') NOT IN ('integer','real','null') AS {name}_invalid")
            fields.append("CASE WHEN json_type(e.data,'$.expires_at') IN ('integer','real') THEN json_extract(e.data,'$.expires_at') END AS expires_at")
            deadline = "CASE WHEN json_type(e.data,'$.authorization_expires_at') IN ('integer','real') THEN json_extract(e.data,'$.authorization_expires_at') END"
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='management_oauth_flows'").fetchone():
                deadline = f"coalesce({deadline}, (SELECT f.expires_at FROM management_oauth_flows f WHERE f.state_hash=json_extract(e.data,'$.oauth_flow') AND f.account_id=e.id))"
            fields.append(deadline + ' AS authorization_expires_at')
        total = conn.execute('SELECT COUNT(*) FROM management_entities e WHERE ' + clause, params).fetchone()[0]
        rows = conn.execute('SELECT ' + ','.join(fields) + ' FROM management_entities e WHERE ' + clause + " ORDER BY COALESCE(CASE WHEN json_type(e.data,'$.sort_index')='integer' THEN json_extract(e.data,'$.sort_index') END, 999999), e.created_at, e.id LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
    items = []
    for row in rows:
        item = {name: row[name] for name in ('id', 'version', 'created_at', 'updated_at')}
        item.update({name: row[name] for name in TEXT_FIELDS if row[name] is not None})
        item['source'] = {name: row['source_' + name] for name in SOURCE_FIELDS if row['source_' + name] is not None}
        for name in ('agent_ids', 'host_ids', 'cc_switch_current_hosts'):
            if row[name] is not None:
                item[name] = json.loads(row[name])
        if row['default_model'] is not None:
            item['default_model'] = row['default_model']
        if row['supported_endpoints'] is not None:
            item['supported_endpoints'] = json.loads(row['supported_endpoints'])
        if row['context_window'] is not None:
            item['context_window'] = row['context_window']
        if row['sort_index'] is not None:
            item['sort_index'] = row['sort_index']
        if row['enabled'] is not None:
            item['enabled'] = bool(row['enabled'])
        if row['credential_reattach_required'] is not None:
            item['credential_reattach_required'] = bool(row['credential_reattach_required'])
        item['has_credential'] = bool(row['has_credential'])
        if kind == 'account' and item.get('kind') == 'oauth':
            for name in ('expires_at', 'authorization_expires_at'):
                if row[name] is not None:
                    item[name] = row[name]
            item['status'] = account_status(item)
            if (item['status'] == 'authorized' and row['expires_at_invalid']) or (item['status'] == 'authorizing' and row['authorization_expires_at_invalid']):
                item['status'] = 'error'
        item['_summary'] = True
        if row['account_name'] is not None:
            item['account_name'] = row['account_name']
        items.append(item)
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset,
            'has_more': offset + len(items) < total, 'agent_id': agent_id, 'scope': scope, 'summary': True}

