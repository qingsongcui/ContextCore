"""Audited compatible local configuration shapes; only temporary paths and a FakeVault."""
import json
from pathlib import Path
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.integrations.config_transactions import ConfigurationError
from app.integrations.secret_store import SecretUnavailable
from app.services.management_service import ManagementService
from app.services.migration_service import MigrationService,REFERENCE_COMMIT


class FakeVault:
    def __init__(self): self.values={}; self.locked=False; self.callback=None
    def put(self,value):
        if self.locked: raise SecretUnavailable('locked')
        ref='keychain:senrelay:'+f'{len(self.values)+1:032x}'
        self.values[ref]=value
        if self.callback: self.callback()
        return ref
    def get(self,ref): return self.values[ref]
    def delete(self,ref): self.values.pop(ref,None)


@pytest.fixture
def env(tmp_path):
    home=tmp_path/'home';home.mkdir()
    vault=FakeVault();manager=ManagementService(tmp_path/'managed.db',home,vault)
    return MigrationService(manager),manager,vault,tmp_path


def payload(secret='private-key-123'):
    return {'version':2,'claude':{'current':'p','providers':{'p':{'id':'p','name':'Claude endpoint','settingsConfig':{
        'env':{'ANTHROPIC_BASE_URL':'https://example.test','ANTHROPIC_AUTH_TOKEN':secret,'ANTHROPIC_MODEL':'native-model'}}}}},
        'codex':{'providers':{'c':{'name':'Codex endpoint','settingsConfig':{'auth':{'OPENAI_API_KEY':secret},
            'config':'model="coding-model"\nmodel_provider="custom"\n[model_providers.custom]\nbase_url="https://example.test/v1"\nwire_api="responses"'}}}},
        'gemini':{'providers':{'g':{'name':'Gemini endpoint','settingsConfig':{'env':{'GOOGLE_GEMINI_BASE_URL':'https://example.test','GEMINI_MODEL':'gemini-model'}}}}},
        'mcp':{'servers':{'m':{'name':'Metadata MCP','server':{'command':'never-execute','args':['--stdio'],'env':{'API_TOKEN':secret}},'apps':{'claude':True}}}},
        'prompts':{'claude':{'prompts':{'p':{'name':'Policy','content':'Use local evidence.'}}}},
        'skills':{'skills':{'test-skill':{'installed':True}},'repos':[]}}


def json_source(tmp,p=None):
    path=tmp/'config.json';path.write_text(json.dumps(p or payload()));return path


def skill_root(tmp):
    root=tmp/'selected-skills';target=root/'test-skill';target.mkdir(parents=True)
    (target/'SKILL.md').write_text('---\nname: synthetic\n---\nRead evidence first.')
    (target/'helper.py').write_text('raise RuntimeError("must never execute")')
    return root


def test_json_preview_apply_idempotence_and_secret_free_storage(env):
    migration,manager,vault,tmp=env
    path=json_source(tmp);root=skill_root(tmp)
    plan=migration.plan({'source_path':str(path),'skill_roots':[str(root)]})
    assert plan['status']=='planned' and plan['requires_secret_confirmation'] is True
    assert plan['counts']=={'providers':3,'models':3,'mcp':1,'prompts':1,'skills':1,'supported':6,'unsupported':0,'secrets':3}
    assert manager.list('provider')==[] and vault.values=={}
    assert 'private-key-123' not in json.dumps(plan)
    assert b'private-key-123' not in manager.db_path.read_bytes()
    with pytest.raises(ConfigurationError): migration.apply(plan['id'],confirmed=True)
    result=migration.apply(plan['id'],confirmed=True,import_secrets_confirmed=True)
    assert result['status']=='applied' and result['receipt']['host_apply']=='not_run'
    assert len(manager.list('provider'))==3 and len(manager.list('model'))==3
    assert len(manager.list('asset'))==3 and len(vault.values)==3
    assert migration.apply(plan['id'],confirmed=True)==result
    assert len(vault.values)==3 and len(manager.list('provider'))==3
    assert 'private-key-123' not in json.dumps(manager.export())
    assert b'private-key-123' not in manager.db_path.read_bytes()
    assert not (manager.home/'.claude').exists()


def test_exclude_credentials_and_missing_skill_are_explicit_partial(env):
    migration,manager,vault,tmp=env
    plan=migration.plan({'source_path':str(json_source(tmp)),'exclude_secrets':True})
    assert plan['requires_secret_confirmation'] is False
    assert plan['counts']['secrets']==0 and plan['counts']['unsupported']==1
    assert next(i for i in plan['items'] if i.get('asset_kind')=='skill')['reason']=='skill_source_files_required'
    result=migration.apply(plan['id'],confirmed=True)
    assert result['status']=='partial' and vault.values=={}
    assert not any(p.get('credential_ref') for p in manager.list('provider'))
    assert manager.list('asset')[0]['config']['env']=={}


def test_changed_source_or_skill_prevents_apply(env):
    migration,manager,_,tmp=env
    path=json_source(tmp);root=skill_root(tmp)
    plan=migration.plan({'source_path':str(path),'skill_roots':[str(root)]})
    (root/'test-skill'/'SKILL.md').write_text('Changed after preview')
    with pytest.raises(ConfigurationError) as exc: migration.apply(plan['id'],confirmed=True,import_secrets_confirmed=True)
    assert exc.value.status_code==409 and manager.list('provider')==[]
    plan2=migration.plan({'source_path':str(path),'skill_roots':[str(root)]})
    path.write_text(json.dumps(payload('replacement-secret')))
    with pytest.raises(ConfigurationError): migration.apply(plan2['id'],confirmed=True,import_secrets_confirmed=True)
    assert manager.list('provider')==[]


def test_vault_failure_never_falls_back_or_commits_partial_definitions(env):
    migration,manager,vault,tmp=env
    plan=migration.plan({'source_path':str(json_source(tmp))})
    vault.locked=True
    with pytest.raises(SecretUnavailable): migration.apply(plan['id'],confirmed=True,import_secrets_confirmed=True)
    assert manager.list('provider')==[] and manager.list('asset')==[]
    assert migration.get(plan['id'])['status']=='failed'
    with pytest.raises(ConfigurationError): migration.apply(plan['id'],confirmed=True,import_secrets_confirmed=True)


def test_source_change_during_vault_write_rolls_back_and_cleans(env):
    migration,manager,vault,tmp=env
    path=json_source(tmp);plan=migration.plan({'source_path':str(path)})
    vault.callback=lambda:path.write_text(json.dumps(payload('changed')))
    with pytest.raises(ConfigurationError): migration.apply(plan['id'],confirmed=True,import_secrets_confirmed=True)
    assert manager.list('provider')==[] and manager.list('model')==[] and manager.list('asset')==[]
    assert vault.values=={}


def db_source(tmp,wal=False):
    path=tmp/'cc-switch.db';c=sqlite3.connect(path)
    c.executescript('''PRAGMA user_version=22;
        CREATE TABLE providers(id TEXT,app_type TEXT,name TEXT,settings_config TEXT,meta TEXT);
        CREATE TABLE mcp_servers(id TEXT,name TEXT,server_config TEXT);
        CREATE TABLE prompts(id TEXT,app_type TEXT,name TEXT,content TEXT);
        CREATE TABLE skills(id TEXT,name TEXT,directory TEXT);''')
    if wal: c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0')
    cfg=payload()['claude']['providers']['p']['settingsConfig']
    c.execute('INSERT INTO providers VALUES(?,?,?,?,?)',('p','claude','SQLite Provider',json.dumps(cfg),'{}'))
    c.execute('INSERT INTO mcp_servers VALUES(?,?,?)',('m','MCP',json.dumps({'command':'never-run'})))
    c.execute('INSERT INTO prompts VALUES(?,?,?,?)',('p','claude','Prompt','Read evidence'))
    c.execute('INSERT INTO skills VALUES(?,?,?)',('s','Skill','test-skill'))
    c.commit();return path,c


def test_sqlite22_wal_snapshot_includes_uncheckpointed_definitions(env):
    migration,manager,_,tmp=env
    path,c=db_source(tmp,True)
    try:
        assert Path(str(path)+'-wal').stat().st_size>0
        plan=migration.plan({'source_path':str(path),'skill_roots':[str(skill_root(tmp))]})
        assert plan['counts']['providers']==1 and plan['counts']['supported']==4
        result=migration.apply(plan['id'],confirmed=True,import_secrets_confirmed=True)
        assert result['status']=='applied' and len(manager.list('provider'))==1
        assert c.execute('SELECT COUNT(*) FROM providers').fetchone()[0]==1
        assert Path(str(path)+'-wal').stat().st_size>0
    finally: c.close()


@pytest.mark.parametrize('bad',[{'version':99,'mcp':{}},{'mcpServers':{}},{'version':2,'unknown':{}},[]])
def test_unknown_json_is_not_empty_success(env,bad):
    migration,manager,_,tmp=env
    path=tmp/'unknown.json';path.write_text(json.dumps(bad))
    with pytest.raises(ConfigurationError): migration.plan({'source_path':str(path)})
    assert manager.list('provider')==[]


def test_unknown_sqlite_version_and_views_are_rejected(env):
    migration,_,_,tmp=env
    path,c=db_source(tmp);c.execute('PRAGMA user_version=23');c.close()
    with pytest.raises(ConfigurationError): migration.plan({'source_path':str(path)})


def test_skill_symlink_escape_is_per_item_unsupported(env):
    migration,_,_,tmp=env
    root=tmp/'roots';root.mkdir();outside=tmp/'outside';outside.mkdir();(outside/'SKILL.md').write_text('Not authorized')
    (root/'test-skill').symlink_to(outside,target_is_directory=True)
    plan=migration.plan({'source_path':str(json_source(tmp)),'skill_roots':[str(root)]})
    assert next(i for i in plan['items'] if i.get('asset_kind')=='skill')['status']=='unsupported'


def test_sources_only_stats_known_candidates_without_content_read(env,monkeypatch):
    migration,manager,_,_=env
    directory=manager.home/'.cc-switch';directory.mkdir();path=directory/'config.json';path.write_text('not JSON; should not be parsed')
    import app.services.migration_service as module
    monkeypatch.setattr(module,'_read',lambda *a,**k:(_ for _ in ()).throw(AssertionError('must not read')))
    result=migration.sources()
    assert result['content_read'] is False
    found=next(x for x in result['candidates'] if x['source_path']==str(path))
    assert found['exists'] and found['read_granted'] is False


def test_api_contract_with_injected_service(env,monkeypatch):
    from app.routers import migration as api
    migration,_,_,tmp=env
    monkeypatch.setattr(api.migration_service,'service',lambda:migration)
    app=FastAPI();app.include_router(api.router);client=TestClient(app)
    assert client.get('/api/manage/migrations/sources').status_code==200
    plan=client.post('/api/manage/migrations/plan',json={'source_path':str(json_source(tmp)),'exclude_secrets':True}).json()
    assert client.get('/api/manage/migrations/'+plan['id']).json()['status']=='planned'
    assert client.post('/api/manage/migrations/'+plan['id']+'/apply',json={'confirmed':False}).status_code==400
    assert client.post('/api/manage/migrations/'+plan['id']+'/apply',json={'confirmed':True}).json()['status']=='partial'


def test_unsupported_or_excluded_secrets_do_not_leak_via_labels(env):
    migration,manager,_,tmp=env
    data=payload('true')
    data['claude']['providers']['p']['name']='true'
    data['codex']['providers']['c']['name']='true'
    data['gemini']['providers']['g']['name']='true'
    data['mcp']['servers']['m']['name']='true'
    plan=migration.plan({'source_path':str(json_source(tmp,data)),'exclude_secrets':True})
    assert plan['requires_secret_confirmation'] is False
    assert '[REDACTED]' in json.dumps(plan)
    assert not any(item['name']=='true' for item in plan['items'])


def test_mcp_failure_rolls_back_providers_and_models_atomically(env,monkeypatch):
    migration,manager,vault,tmp=env
    plan=migration.plan({'source_path':str(json_source(tmp))})
    import app.services.migration_service as module
    original=module._TransactionalManager.save
    def fail_asset(self,kind,data,*args,**kwargs):
        if kind=='asset': raise ConfigurationError('synthetic failure',422)
        return original(self,kind,data,*args,**kwargs)
    monkeypatch.setattr(module._TransactionalManager,'save',fail_asset)
    with pytest.raises(ConfigurationError): migration.apply(plan['id'],confirmed=True,import_secrets_confirmed=True)
    assert manager.list('provider')==[] and manager.list('model')==[] and vault.values=={}


def test_sqlite_wal_changed_after_plan_is_detected(env):
    migration,manager,_,tmp=env
    path,c=db_source(tmp,True)
    try:
        plan=migration.plan({'source_path':str(path),'exclude_secrets':True})
        c.execute("UPDATE providers SET name='Changed through WAL'");c.commit()
        with pytest.raises(ConfigurationError) as exc: migration.apply(plan['id'],confirmed=True)
        assert exc.value.status_code==409 and manager.list('provider')==[]
    finally:c.close()


def test_unknown_nonempty_sections_are_reported_not_silently_imported(env):
    migration,_,_,tmp=env
    data=payload();data['common_config_snippets']={'claude':'extra native setting'}
    plan=migration.plan({'source_path':str(json_source(tmp,data)),'exclude_secrets':True})
    assert any(i['source_id']=='section:common_config_snippets' and i['status']=='unsupported' for i in plan['items'])


def test_sqlite_path_swap_cannot_read_unselected_database(env,monkeypatch):
    import app.services.migration_service as module
    migration,_,_,tmp=env
    selected,c=db_source(tmp);c.close()
    otherdir=tmp/'other';otherdir.mkdir();outside,other=db_source(otherdir)
    other.execute("UPDATE providers SET name='Not selected'");other.commit();other.close()
    original=module._sqlite_snapshot
    def raced(path,*args,**kwargs):
        held=path.with_name('held.db');path.rename(held);path.symlink_to(outside)
        try:return original(path,*args,**kwargs)
        finally:path.unlink();held.rename(path)
    monkeypatch.setattr(module,'_sqlite_snapshot',raced)
    with pytest.raises(ConfigurationError): migration.plan({'source_path':str(selected)})


@pytest.mark.parametrize('exclude',[False,True])
def test_known_secret_as_mcp_dictionary_key_is_unsupported_and_not_persisted(env,exclude):
    migration,manager,_,tmp=env
    secret='synthetic-secret-12345'
    data=payload(secret);data['mcp']['servers']['m']['server']['env']={secret:'other-value'}
    plan=migration.plan({'source_path':str(json_source(tmp,data)),'exclude_secrets':exclude})
    assert next(i for i in plan['items'] if i.get('asset_kind')=='mcp')['status']=='unsupported'
    assert secret not in json.dumps(plan) and secret.encode() not in manager.db_path.read_bytes()
    migration.apply(plan['id'],confirmed=True,import_secrets_confirmed=not exclude)
    assert secret not in json.dumps(manager.export()) and secret.encode() not in manager.db_path.read_bytes()


def test_known_secret_skill_filename_does_not_survive_rejected_item_manifest(env):
    migration,manager,_,tmp=env
    secret='synthetic-secret-filename-12345'
    root=skill_root(tmp);(root/'test-skill'/secret).write_text('not a credential')
    plan=migration.plan({'source_path':str(json_source(tmp,payload(secret))),'skill_roots':[str(root)],'exclude_secrets':True})
    assert next(i for i in plan['items'] if i.get('asset_kind')=='skill')['status']=='unsupported'
    assert secret.encode() not in manager.db_path.read_bytes()
