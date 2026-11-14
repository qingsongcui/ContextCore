"""Read-only, hash-bound compatible local configuration migration into managed definitions.

No host apply, network request, package execution or implicit HOME discovery.
Secrets are re-read only at confirmed apply; plans persist metadata and digests.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import tempfile
import tomllib
from uuid import uuid4

from app.integrations.config_transactions import ConfigurationError, canonical, configuration_lock, redact
from app.integrations.secret_store import SecretUnavailable
from app.services import management_service as management

REFERENCE_COMMIT = 'd8983bb10b82a1ccc15ae5f44ab070902623e990'
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_ITEMS = 1000
APPS = {'claude','claude-desktop','codex','gemini','grokbuild','opencode','openclaw','hermes','agy','mcode','kimi','pi'}
SENSITIVE = re.compile(r'key|token|secret|password|authorization|cookie|credential', re.I)


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS management_migrations (
        id TEXT PRIMARY KEY,status TEXT NOT NULL,source_hash TEXT NOT NULL,
        identity_key TEXT NOT NULL UNIQUE,private_manifest TEXT NOT NULL,
        preview TEXT NOT NULL,receipt TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')


def _hash(value):
    return hashlib.sha256(value if isinstance(value,bytes) else canonical(value).encode()).hexdigest()


def _path(raw, directory=False):
    if not isinstance(raw,str) or not Path(raw).is_absolute() or '\x00' in raw:
        raise ConfigurationError('Select an explicit absolute source path',422)
    path = Path(raw)
    if any(p.is_symlink() for p in [path,*path.parents]):
        raise ConfigurationError('Selected paths must not traverse symbolic links',422)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise ConfigurationError('Selected source is unavailable',422) from None
    if directory and not resolved.is_dir():
        raise ConfigurationError('A selected Skill root must be a directory',422)
    return resolved


def _parent_fd(path):
    """Walk from / with directory FDs; no path component follows a symlink."""
    fd=os.open('/',os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:
        for component in Path(path).parent.parts[1:]:
            next_fd=os.open(component,os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0),dir_fd=fd)
            os.close(fd);fd=next_fd
        return fd
    except BaseException:
        os.close(fd);raise


def _file_info(info):
    return {'device':info.st_dev,'inode':info.st_ino,'size':info.st_size,
            'mtime_ns':info.st_mtime_ns,'ctime_ns':info.st_ctime_ns}


def _read(path, limit=MAX_SOURCE_BYTES):
    directory=None
    try:
        directory=_parent_fd(path)
        fd=os.open(Path(path).name,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0),dir_fd=directory)
        with os.fdopen(fd,'rb') as handle:
            info=os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>limit:
                raise ConfigurationError('Selected file is not a bounded regular file',422)
            data=handle.read(limit+1)
            if len(data)>limit or _file_info(info)!=_file_info(os.fstat(handle.fileno())):
                raise ConfigurationError('Selected source changed while reading',409)
        return data,_file_info(info)
    except (OSError,ValueError):
        raise ConfigurationError('Selected source could not be read safely',422) from None
    finally:
        if directory is not None: os.close(directory)


def _json(value):
    if isinstance(value,str):
        try:
            return json.loads(value)
        except (ValueError,RecursionError):
            raise ConfigurationError('Stored source JSON is malformed',422) from None
    return value


def _mapping(value, label):
    if not isinstance(value,dict):
        raise ConfigurationError(label+' must be an object',422)
    return value


def _sqlite(data):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    try:
        conn.enable_load_extension(False)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,2*1024*1024)
        conn.deserialize(data)
        conn.execute('PRAGMA trusted_schema=OFF')
        steps=[0]
        def progress():
            steps[0]+=1
            return int(steps[0]>100)
        conn.set_progress_handler(progress,10000)
        version=conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in (21, 22):
            raise ConfigurationError('Unsupported SQLite schema version; expected 22',422)
        schemas={r['name']:r['sql'] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
        required={'providers':{'id','app_type','name','settings_config'},'mcp_servers':{'id','name','server_config'},
                  'prompts':{'id','app_type','name','content'},'skills':{'id','name','directory'}}
        records={}
        for table,fields in required.items():
            if table not in schemas or 'VIRTUAL' in (schemas[table] or '').upper():
                raise ConfigurationError('Unsupported or incomplete SQLite schema: '+table,422)
            columns={r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            if not fields <= columns:
                raise ConfigurationError('Unsupported SQLite columns: '+table,422)
            # Read only audited definition columns, never sessions/accounts/logs.
            allowed=fields | ({'meta'} if table=='providers' else set())
            names=sorted(allowed & columns)
            rows=conn.execute('SELECT '+','.join('"'+x+'"' for x in names)+' FROM "'+table+'" ORDER BY '+('app_type,id' if 'app_type' in fields else 'id')+' LIMIT ?', (MAX_ITEMS+1,)).fetchall()
            if len(rows)>MAX_ITEMS:
                raise ConfigurationError('Source has too many definitions',422)
            records[table]=[dict(row) for row in rows]
            identities=[(r.get('app_type',''),r['id']) for r in records[table]]
            if len(set(identities))!=len(identities):
                raise ConfigurationError('Source has ambiguous duplicate definition identities',422)
        return records,version
    except (sqlite3.Error,OverflowError,MemoryError):
        raise ConfigurationError('Selected SQLite source is unreadable or unsupported',422) from None
    finally:
        conn.close()


def _sqlite_snapshot(path,expected_identity):
    """Stage verified main/WAL bytes; SQLite never reopens a user source path."""
    directory=None;handles=[]
    try:
        directory=_parent_fd(path)
        before={};data={}
        for suffix in ('','-wal'):
            try:
                fd=os.open(path.name+suffix,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0),dir_fd=directory)
            except FileNotFoundError:
                if suffix: continue
                raise
            handles.append((suffix,fd))
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_SOURCE_BYTES:
                raise ConfigurationError('source_unstable_or_wal_exceeds_size_limit',422)
            if not suffix and (info.st_dev,info.st_ino)!=(expected_identity['device'],expected_identity['inode']):
                raise ConfigurationError('source_identity_changed_before_snapshot',409)
            before[suffix]=_file_info(info)
            with os.fdopen(os.dup(fd),'rb') as reader: data[suffix]=reader.read(MAX_SOURCE_BYTES+1)
        # Two complete reads through the same FDs reject writes/checkpoints and
        # rotation during capture. Final name checks are metadata-only.
        for suffix,fd in handles:
            os.lseek(fd,0,os.SEEK_SET)
            with os.fdopen(os.dup(fd),'rb') as reader: again=reader.read(MAX_SOURCE_BYTES+1)
            named=os.stat(path.name+suffix,dir_fd=directory,follow_symlinks=False)
            if (again!=data[suffix] or before[suffix]!=_file_info(os.fstat(fd))
                    or not stat.S_ISREG(named.st_mode) or before[suffix]!=_file_info(named)):
                raise ConfigurationError('source_unstable_during_snapshot',409)
        if '-wal' not in before:
            try: os.stat(path.name+'-wal',dir_fd=directory,follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise ConfigurationError('source_wal_appeared_during_snapshot',409)
        with tempfile.TemporaryDirectory(prefix='senrelay-migration-') as temporary:
            stage=Path(temporary)/'source.db'
            for suffix,content in data.items():
                target=Path(str(stage)+suffix)
                target.write_bytes(content);target.chmod(0o600)
            source=sqlite3.connect(stage.as_uri()+'?mode=ro',uri=True,timeout=2)
            target=sqlite3.connect(':memory:')
            try:
                source.enable_load_extension(False)
                source.execute('PRAGMA mmap_size=0');source.execute('PRAGMA query_only=ON');source.execute('PRAGMA trusted_schema=OFF')
                if source.execute('PRAGMA page_count').fetchone()[0]*source.execute('PRAGMA page_size').fetchone()[0]>MAX_SOURCE_BYTES:
                    raise ConfigurationError('SQLite snapshot exceeds size limit',422)
                deadline=time.monotonic()+5
                def budget(status,remaining,total):
                    if time.monotonic()>deadline: raise ConfigurationError('source_unstable_or_snapshot_timeout',422)
                source.backup(target,pages=256,progress=budget,sleep=0.01)
                snapshot=bytearray(target.serialize());snapshot[18:20]=b'\x01\x01'
                return bytes(snapshot)
            finally:
                source.close();target.close()
    except (OSError,sqlite3.Error):
        raise ConfigurationError('source_unstable_or_sqlite_snapshot_unavailable',422) from None
    finally:
        for _,fd in handles: os.close(fd)
        if directory is not None: os.close(directory)


def _json_records(raw):
    raw=_mapping(raw,'Local config')
    if raw.get('version') != 2 or isinstance(raw.get('version'),bool):
        raise ConfigurationError('Unsupported JSON config version; expected 2',422)
    recognized=set(raw) & (APPS|{'mcp','prompts','skills'})
    if not recognized:
        raise ConfigurationError('JSON config contains no recognized definition sections',422)
    records={'providers':[],'mcp_servers':[],'prompts':[],'skills':[],'unsupported_sections':[]}
    for section in sorted(set(raw)-APPS-{'version','mcp','prompts','skills'}):
        if raw[section]: records['unsupported_sections'].append({'id':section})
    for app in sorted(APPS & set(raw)):
        group=_mapping(raw[app],'Provider manager')
        for key,value in _mapping(group.get('providers',{}),'Providers').items():
            value=_mapping(value,'Provider')
            records['providers'].append({'id':str(key),'app_type':app,'name':value.get('name'),
                'settings_config':value.get('settingsConfig'),'meta':value.get('meta') or {}})
    mcp=_mapping(raw.get('mcp',{}),'MCP root')
    for key,value in _mapping(mcp.get('servers') or {},'MCP servers').items():
        value=_mapping(value,'MCP server')
        records['mcp_servers'].append({'id':str(key),'name':value.get('name'),'server_config':value.get('server')})
    # Legacy per-app server shapes are also declared by McpRoot.
    for app in sorted((APPS|{'claudeDesktop','claude_desktop'}) & set(mcp)):
        group=_mapping(mcp[app],'Legacy MCP group')
        for key,value in _mapping(group.get('servers',{}),'Legacy MCP servers').items():
            value=_mapping(value,'Legacy MCP server')
            records['mcp_servers'].append({'id':app+':'+str(key),'name':value.get('name') or str(key),
                                          'server_config':value.get('server',value)})
    for app,group in _mapping(raw.get('prompts',{}),'Prompt root').items():
        if app not in APPS|{'claudeDesktop','claude_desktop'}:
            records['unsupported_sections'].append({'id':'prompts.'+app});continue
        for key,value in _mapping(_mapping(group,'Prompt group').get('prompts',{}),'Prompts').items():
            value=_mapping(value,'Prompt')
            records['prompts'].append({'id':str(key),'app_type':app,'name':value.get('name'),'content':value.get('content')})
    skills=_mapping(raw.get('skills',{}),'Skill store')
    if skills.get('repos'): records['unsupported_sections'].append({'id':'skills.repos'})
    for directory,state in _mapping(skills.get('skills',{}),'Skill states').items():
        if _mapping(state,'Skill state').get('installed') is True:
            records['skills'].append({'id':str(directory),'name':Path(directory).name,'directory':directory})
    if sum(map(len,records.values()))>MAX_ITEMS:
        raise ConfigurationError('Source has too many definitions',422)
    return records,2


def _secret(value,secrets):
    if not isinstance(value,str) or not value or len(value)>16384 or any(c in value for c in '\x00\r\n'):
        raise ConfigurationError('A source credential has an unsupported value',422)
    slot='secret_'+str(len(secrets)+1)
    secrets[slot]=value
    return '{{migration:'+slot+'}}'


def _asset_secrets(value,secrets,exclude):
    if isinstance(value,dict):
        out={}
        for key,item in value.items():
            if SENSITIVE.search(str(key)) and isinstance(item,str) and item:
                if exclude:
                    continue
                bearer=item.startswith('Bearer ')
                out[key]=('Bearer ' if bearer else '')+_secret(item[7:] if bearer else item,secrets)
            else:
                out[key]=_asset_secrets(item,secrets,exclude)
        return out
    if isinstance(value,list):
        return [_asset_secrets(x,secrets,exclude) for x in value]
    return value


def _provider(row,secrets,exclude):
    app=row['app_type']; cfg=_mapping(_json(row['settings_config']),'Provider settings')
    meta=_json(row.get('meta') or {})
    if isinstance(meta,dict) and meta.get('providerType') in ('codex_oauth','xai_oauth','github_copilot'):
        raise ConfigurationError('managed_oauth_requires_official_authorization',422)
    key=None; models={}
    if app=='claude':
        env=_mapping(cfg.get('env',{}),'Claude env')
        endpoint=env.get('ANTHROPIC_BASE_URL'); protocol='anthropic'
        keys=[env[k] for k in ('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY') if env.get(k)]
        if len(set(keys))>1:
            raise ConfigurationError('multiple_provider_credentials_require_selection',422)
        key=keys[0] if keys else None
        for k in ('ANTHROPIC_MODEL','ANTHROPIC_DEFAULT_HAIKU_MODEL','ANTHROPIC_DEFAULT_SONNET_MODEL','ANTHROPIC_DEFAULT_OPUS_MODEL'):
            if isinstance(env.get(k),str) and env[k]: models[k]=env[k]
    elif app=='codex':
        if not isinstance(cfg.get('config'),str):
            raise ConfigurationError('codex_requires_verified_toml_config',422)
        try: native=tomllib.loads(cfg['config'])
        except tomllib.TOMLDecodeError: raise ConfigurationError('codex_toml_is_malformed',422) from None
        selected=native.get('model_provider')
        provider=_mapping(_mapping(native.get('model_providers',{}),'Codex providers').get(selected),'Selected Codex provider')
        if provider.get('wire_api','responses') not in ('responses','chat'):
            raise ConfigurationError('unsupported_codex_wire_api',422)
        endpoint=provider.get('base_url'); protocol='openai'
        auth=_mapping(cfg.get('auth',{}),'Codex auth')
        keys=[x for x in (auth.get('OPENAI_API_KEY'),provider.get('experimental_bearer_token')) if x]
        if len(set(keys))>1: raise ConfigurationError('multiple_provider_credentials_require_selection',422)
        key=keys[0] if keys else None
        if isinstance(native.get('model'),str): models['default']=native['model']
    elif app=='gemini':
        env=_mapping(cfg.get('env',{}),'Gemini env')
        endpoint=env.get('GOOGLE_GEMINI_BASE_URL'); protocol='gemini'; key=env.get('GEMINI_API_KEY')
        if isinstance(env.get('GEMINI_MODEL'),str): models['default']=env['GEMINI_MODEL']
    else:
        raise ConfigurationError('provider_shape_not_yet_verified_for_'+str(app),422)
    if not isinstance(endpoint,str) or not endpoint:
        raise ConfigurationError('provider_requires_explicit_endpoint',422)
    data={'name':row['name'],'protocol':protocol,'base_url':management.ManagementService._endpoint(endpoint),'model_map':models}
    marker=_secret(key,secrets) if key and not exclude else None
    return data,marker


def _skill(row,roots):
    directory=row['directory']
    if not isinstance(directory,str) or not directory or '\\' in directory or '..' in Path(directory).parts:
        raise ConfigurationError('unsafe_skill_directory',422)
    candidates=[]
    for root in roots:
        target=Path(directory) if Path(directory).is_absolute() else root/directory
        if target.is_relative_to(root) and target.exists(): candidates.append((root,target))
    if not candidates:
        raise ConfigurationError('skill_source_files_required',422)
    if len(candidates)!=1:
        raise ConfigurationError('ambiguous_skill_roots',422)
    root,target=candidates[0]
    target=_path(str(target),directory=True)
    if not target.is_relative_to(root): raise ConfigurationError('skill_outside_authorized_root',422)
    files={}; fingerprints={}; total=0
    for base,dirs,names in os.walk(target,followlinks=False):
        for directory_name in dirs:
            if (Path(base)/directory_name).is_symlink(): raise ConfigurationError('skill_symlink_not_supported',422)
        for name in sorted(names):
            path=Path(base)/name
            if path.is_symlink(): raise ConfigurationError('skill_symlink_not_supported',422)
            content,info=_read(path,500000)
            total+=len(content)
            if total>2*1024*1024 or len(files)>=100: raise ConfigurationError('skill_exceeds_file_or_size_limit',422)
            relative=path.relative_to(target).as_posix()
            try: files[relative]=content.decode('utf-8')
            except UnicodeDecodeError: raise ConfigurationError('skill_contains_non_utf8_file',422) from None
            fingerprints[relative]={'sha256':_hash(content),**info}
    management.ManagementService._validate_skill(files)
    return files,{'root':str(root),'directory':str(target),'files':fingerprints}


def _dummy_refs(value):
    text=canonical(value)
    return json.loads(re.sub(r'\{\{migration:(secret_\d+)\}\}',lambda m:'{{credential:keychain:senrelay:'+hashlib.sha256(m[1].encode()).hexdigest()[:32]+'}}',text))


def _sensitive_values(value):
    """Redaction values only, including credentials in unsupported records."""
    found=[]
    if isinstance(value,dict):
        for key,item in value.items():
            if SENSITIVE.search(str(key)) and isinstance(item,str) and item:
                found.extend([item,item[7:]] if item.startswith('Bearer ') else [item])
            if key in ('settings_config','server_config','meta') and isinstance(item,str):
                try: found.extend(_sensitive_values(json.loads(item)))
                except (ValueError,RecursionError): pass
            elif key=='config' and isinstance(item,str):
                try: found.extend(_sensitive_values(tomllib.loads(item)))
                except (tomllib.TOMLDecodeError,RecursionError): pass
            else: found.extend(_sensitive_values(item))
    elif isinstance(value,list):
        for item in value: found.extend(_sensitive_values(item))
    return found


def _load(source_path,skill_roots,exclude=False):
    path=_path(source_path)
    if path.suffix.lower() not in ('.json','.db','.sqlite','.sqlite3'):
        raise ConfigurationError('Choose a JSON or SQLite definition file',422)
    data,identity=_read(path)
    if data.startswith(b'SQLite format 3\x00'):
        records,version=_sqlite(_sqlite_snapshot(path,identity)); format_name='cc-switch-sqlite'
        fresh=path.stat()
        if (fresh.st_dev,fresh.st_ino)!=(identity['device'],identity['inode']):
            raise ConfigurationError('source_replaced_during_snapshot',409)
        source_digest=_hash({'records':records,'version':version})
    else:
        if len(data)>8*1024*1024: raise ConfigurationError('JSON source exceeds 8 MiB',422)
        try: raw=json.loads(data.decode('utf-8'))
        except (ValueError,UnicodeDecodeError,RecursionError): raise ConfigurationError('Selected JSON is malformed',422) from None
        records,version=_json_records(raw); format_name='cc-switch-json'
        source_digest=_hash(data)
    roots=[_path(raw,directory=True) for raw in skill_roots]
    if len(set(map(str,roots)))!=len(roots): raise ConfigurationError('Skill roots must be distinct',422)
    specs=[];items=[];secrets={};skill_evidence={}
    redaction_values=sorted(set(_sensitive_values(records)),key=len,reverse=True)
    def unsafe_keys(value):
        if isinstance(value,dict):
            return any(any(secret in str(key) for secret in redaction_values) or unsafe_keys(item) for key,item in value.items())
        if isinstance(value,list): return any(unsafe_keys(item) for item in value)
        return False
    for collection,rows in records.items():
        if collection=='unsupported_sections':
            items.extend({'source_id':'section:'+row['id'],'entity_kind':'configuration','name':'Unsupported source section',
                'status':'unsupported','has_secret':False,'reason':'section_not_imported:'+row['id'],
                'redacted_diff':{'before':None,'after':{}}} for row in rows)
            continue
        for row in rows:
            source_id=collection+':'+str(row.get('app_type',''))+':'+str(row['id'])
            kind='provider' if collection=='providers' else 'asset'
            asset_kind={'mcp_servers':'mcp','prompts':'prompt','skills':'skill'}.get(collection)
            start_secrets=set(secrets)
            item={'source_id':source_id,'entity_kind':kind,'name':row.get('name') or str(row['id']),
                  'status':'supported','has_secret':False,'redacted_diff':{'before':None,'after':{}}}
            if asset_kind: item['asset_kind']=asset_kind
            try:
                if collection=='providers':
                    value,marker=_provider(row,secrets,exclude)
                elif collection=='mcp_servers':
                    cfg=copy.deepcopy(_mapping(_json(row['server_config']),'MCP config'))
                    if unsafe_keys(cfg): raise ConfigurationError('credential_value_in_dictionary_key',422)
                    if 'type' in cfg: cfg['transport']=cfg.pop('type')
                    value={'name':item['name'],'kind':'mcp','config':_asset_secrets(cfg,secrets,exclude)};marker=None
                    management.ManagementService._validate_mcp(_dummy_refs(value['config']))
                elif collection=='prompts':
                    value={'name':item['name'],'kind':'prompt','content':row['content']};marker=None
                    if not isinstance(row['content'],str) or len(row['content'])>200000: raise ConfigurationError('prompt_exceeds_text_limit',422)
                else:
                    files,evidence=_skill(row,roots);skill_evidence[source_id]=evidence
                    value={'name':item['name'],'kind':'skill','files':files};marker=None
                value['source']={'type':'cc-switch-migration','reference_commit':REFERENCE_COMMIT,'source_id':source_id}
                # Validate secret-free definitions using existing management contracts.
                management.reject_secrets(_dummy_refs(value))
                def contains_copied_secret(item):
                    if isinstance(item,dict): return any(contains_copied_secret(str(k)) or contains_copied_secret(v) for k,v in item.items())
                    if isinstance(item,list): return any(contains_copied_secret(x) for x in item)
                    return isinstance(item,str) and not re.fullmatch(r'(?:Bearer )?\{\{migration:secret_\d+\}\}',item) and any(secret in item for secret in redaction_values)
                # Repeated secrets in names/endpoints/prompt text cannot become
                # ordinary managed metadata, even when credential import is off.
                if contains_copied_secret(value):
                    raise ConfigurationError('credential_value_in_noncredential_field',422)
                if not isinstance(value.get('name'),str) or not value['name'].strip() or len(value['name'])>200:
                    raise ConfigurationError('invalid_definition_name',422)
                spec={'source_id':source_id,'kind':kind,'data':value,'provider_secret':marker}
                specs.append(spec)
                item['has_secret']=set(secrets)!=start_secrets
                after={'name':value['name'],'fields':sorted(value),'kind':asset_kind or kind}
                if kind=='provider': after.update(protocol=value['protocol'],base_url=value['base_url'],models=list(value['model_map'].values()))
                if asset_kind=='skill': after['file_count']=len(value['files'])
                if asset_kind=='prompt': after['characters']=len(value['content'])
                if asset_kind=='mcp': after['config_fields']=sorted(value['config'])
                item['redacted_diff']['after']=after
            except (ConfigurationError,TypeError,KeyError,ValueError) as exc:
                # Fixed diagnostics never embed untrusted config or secret text.
                reason=str(exc) if isinstance(exc,ConfigurationError) else 'unsupported_definition_shape'
                item.update(status='unsupported',reason=reason)
                skill_evidence.pop(source_id,None)
                for key in set(secrets)-start_secrets: secrets.pop(key)
            items.append(item)
    source={'format':format_name,'version':version,'sha256':source_digest,'basename':path.name,'reference_commit':REFERENCE_COMMIT}
    manifest={'source_path':str(path),'identity':identity,'source':source,'skill_roots':list(map(str,roots)),
              'exclude_secrets':exclude,'skill_evidence':skill_evidence,
              'spec_fingerprints':[_hash(_dummy_refs(s)) for s in specs]}
    def metadata_has_secret(value):
        if isinstance(value,dict): return any(metadata_has_secret(str(k)) or metadata_has_secret(v) for k,v in value.items())
        if isinstance(value,list): return any(metadata_has_secret(v) for v in value)
        return isinstance(value,str) and any(secret in value for secret in redaction_values)
    if metadata_has_secret(manifest):
        raise ConfigurationError('Credential value occurs in source metadata; select a sanitized source',422)
    counts={name:len(records[key]) for name,key in [('providers','providers'),('mcp','mcp_servers'),('prompts','prompts'),('skills','skills')]}
    counts.update(models=sum(len(set(s['data'].get('model_map',{}).values())) for s in specs if s['kind']=='provider'),
                  supported=len(specs),unsupported=sum(i['status']=='unsupported' for i in items),secrets=len(secrets))
    # Replace even accidental repeats of known credentials in labels before persisting.
    preview={'source':source,'counts':counts,'items':items,'requires_secret_confirmation':bool(secrets),'host_apply':'not_run'}
    def safe_preview(value):
        if isinstance(value,dict): return {k:safe_preview(v) for k,v in value.items()}
        if isinstance(value,list): return [safe_preview(v) for v in value]
        if isinstance(value,str):
            value=redact(value)
            for secret in redaction_values: value=value.replace(secret,'[REDACTED]')
        return value
    preview=safe_preview(preview)

    return manifest,preview,specs,secrets


class _TransactionConnection:
    def __init__(self,conn): self.conn=conn
    def execute(self,sql,parameters=()):
        if sql.strip().upper()=='BEGIN IMMEDIATE' and self.conn.in_transaction:
            return self.conn.execute('SELECT 1')
        return self.conn.execute(sql,parameters)
    def __getattr__(self,key): return getattr(self.conn,key)


class _TransactionalManager(management.ManagementService):
    @contextmanager
    def connect(self):
        yield _TransactionConnection(self._migration_connection)


class MigrationService:
    def __init__(self,manager=None):
        self.manager=manager or management.service()
        with self.manager.connect() as conn: ensure_schema(conn)

    def sources(self):
        # Audited upstream config.rs/database/mod.rs defaults, metadata only.
        candidates=[]
        for name,kind in (('cc-switch.db','cc-switch-sqlite'),('config.json','cc-switch-json')):
            path=self.manager.home/'.cc-switch'/name
            linked=any(p.is_symlink() for p in [path,*path.parents])
            try:
                info=path.stat() if not linked else None
                exists=bool(info and stat.S_ISREG(info.st_mode))
            except OSError: info=None;exists=False
            candidates.append({'source_path':str(path),'format':kind,'exists':exists,
                'size':info.st_size if exists else None,'mtime_ns':info.st_mtime_ns if exists else None,
                'read_granted':False,'status':'symlink_requires_manual_selection' if linked else 'available' if exists else 'missing'})
        return {'candidates':candidates,'mode':'metadata','content_read':False,'coverage':'default_locations_only','reference_commit':REFERENCE_COMMIT}

    def get(self,plan_id):
        with self.manager.connect() as conn:
            row=conn.execute('SELECT * FROM management_migrations WHERE id=?',(plan_id,)).fetchone()
        if not row: raise ConfigurationError('Migration plan was not found',404)
        result=json.loads(row['preview'])
        result.update(id=row['id'],status=row['status'])
        if row['receipt']: result['receipt']=json.loads(row['receipt'])
        return result

    def plan(self,request):
        roots=request.get('skill_roots',[])
        if not isinstance(roots,list) or len(roots)>8 or not all(isinstance(x,str) for x in roots):
            raise ConfigurationError('Select at most eight explicit Skill roots',422)
        exclude=request.get('exclude_secrets',False)
        if not isinstance(exclude,bool): raise ConfigurationError('exclude_secrets must be a boolean',422)
        manifest,preview,_,_=_load(request.get('source_path'),roots,exclude)
        identity=_hash(manifest)
        now=datetime.now(timezone.utc).isoformat()
        with self.manager.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing=conn.execute('SELECT id FROM management_migrations WHERE identity_key=?',(identity,)).fetchone()
            if existing: plan_id=existing['id']
            else:
                plan_id='migration_'+uuid4().hex
                conn.execute('INSERT INTO management_migrations VALUES(?,?,?,?,?,?,NULL,?,?)',
                    (plan_id,'planned',manifest['source']['sha256'],identity,canonical(manifest),canonical(preview),now,now))
        return self.get(plan_id)

    def apply(self,plan_id,*,confirmed=False,import_secrets_confirmed=False):
        if confirmed is not True: raise ConfigurationError('Explicit migration confirmation is required',400)
        with configuration_lock:
            with self.manager.connect() as conn:
                row=conn.execute('SELECT * FROM management_migrations WHERE id=?',(plan_id,)).fetchone()
            if not row: raise ConfigurationError('Migration plan was not found',404)
            if row['status'] in ('applied','partial'): return self.get(plan_id)
            if row['status']!='planned':
                raise ConfigurationError('Migration outcome requires review; it will not be replayed automatically',409)
            expected=json.loads(row['private_manifest'])
            manifest,preview,specs,secrets=_load(expected['source_path'],expected['skill_roots'],expected['exclude_secrets'])
            if _hash(manifest)!=row['identity_key']:
                raise ConfigurationError('Selected source or Skill files changed; create a new preview',409)
            if secrets and import_secrets_confirmed is not True:
                raise ConfigurationError('Explicit credential import confirmation is required',400)
            with self.manager.connect() as conn:
                changed=conn.execute("UPDATE management_migrations SET status='applying',updated_at=? WHERE id=? AND status='planned'",
                    (datetime.now(timezone.utc).isoformat(),plan_id))
                if changed.rowcount!=1: raise ConfigurationError('Migration state changed; read it back',409)
            references={};cleanup_ids=[]
            try:
                # Durable cleanup receipts precede adoption. No secret value is
                # placed in a plan, receipt, SQL parameter, or exception message.
                for slot,value in secrets.items():
                    ref=self.manager._store_secret(value)
                    references[slot]=ref
                    with self.manager.connect() as conn:
                        cleanup_ids.append(self.manager._cleanup_receipt(conn,ref,'migration_not_adopted'))
                with self.manager.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    tx=object.__new__(_TransactionalManager)
                    tx.__dict__.update(self.manager.__dict__)
                    tx._migration_connection=conn
                    imported=[]
                    for spec in specs:
                        encoded=canonical(spec['data'])
                        for slot,ref in references.items(): encoded=encoded.replace('{{migration:'+slot+'}}','{{credential:'+ref+'}}')
                        data=json.loads(encoded)
                        if spec['kind']=='provider' and spec['provider_secret']:
                            slot=re.fullmatch(r'\{\{migration:(secret_\d+)\}\}',spec['provider_secret'])[1]
                            # New providers only: inherited credentials are not modified.
                            created=tx.save('provider',data)
                            stored=tx.get('provider',created['id'])
                            stored['credential_ref']=references[slot]
                            value={k:v for k,v in stored.items() if k not in ('id','version','created_at','updated_at')}
                            conn.execute('UPDATE management_entities SET data=?,version=version+1 WHERE id=?', (canonical(value),created['id']))
                        else:
                            created=tx.save(spec['kind'],data)
                        imported.append({'source_id':spec['source_id'],'entity_kind':spec['kind'],'id':created['id']})
                        if spec['kind']=='provider':
                            for native in sorted(set(data.get('model_map',{}).values())):
                                model=tx.save('model',{'name':native,'model_id':native,'provider_id':created['id']})
                                imported.append({'source_id':spec['source_id']+':model:'+native,'entity_kind':'model','id':model['id']})
                    # Catch edits that happened while Keychain was being written.
                    current,_,_,_=_load(expected['source_path'],expected['skill_roots'],expected['exclude_secrets'])
                    if _hash(current)!=row['identity_key']:
                        raise ConfigurationError('Selected source changed during apply; imported definitions rolled back',409)
                    receipt={'imported':imported,'unsupported_count':preview['counts']['unsupported'],'host_apply':'not_run',
                             'source_hash':row['source_hash'],'source_version':manifest['source']['version'],'credential_values_exported':False}
                    status='partial' if preview['counts']['unsupported'] else 'applied'
                    conn.execute('UPDATE management_migrations SET status=?,receipt=?,updated_at=? WHERE id=?',
                        (status,canonical(receipt),datetime.now(timezone.utc).isoformat(),plan_id))
                    for cleanup_id in cleanup_ids:
                        conn.execute("UPDATE management_secret_cleanup SET status='completed' WHERE id=?",(cleanup_id,))
            except Exception as exc:
                with self.manager.connect() as conn:
                    conn.execute("UPDATE management_migrations SET status='failed',receipt=?,updated_at=? WHERE id=?",
                        (canonical({'imported':[],'host_apply':'not_run','reason':'apply_failed_no_definitions_committed'}),datetime.now(timezone.utc).isoformat(),plan_id))
                for cleanup_id in cleanup_ids: self.manager.retry_credential_cleanup(cleanup_id)
                if isinstance(exc,SecretUnavailable): raise SecretUnavailable('Credential import unavailable; no plaintext fallback') from None
                if isinstance(exc,ConfigurationError): raise exc
                raise ConfigurationError('Migration failed; no managed definitions were committed',422) from None
            return self.get(plan_id)


def service():
    return MigrationService()
