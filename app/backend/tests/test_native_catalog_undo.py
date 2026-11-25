"""Isolated native takeover and verified undo through the workbench's HTTP routes."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.config_transactions import ConfigurationError
from app.routers import manage
from app.services import management_undo, native_catalog
from tests.test_management_experience import manager, setup


def client(manager, monkeypatch):
    monkeypatch.setattr(manage.management, 'service', lambda: manager)
    app = FastAPI()
    app.include_router(manage.router)
    return TestClient(app)


def test_native_codex_change_rejected_and_selected_records_retry_without_host_writes(manager, monkeypatch):
    api = client(manager, monkeypatch)
    path = manager.home / '.codex/config.toml'
    path.parent.mkdir()
    text = """model_provider = "mine"
model = "chosen"
unmanaged = true
[model_providers.mine]
name = "Private provider"
base_url = "https://local.example/v1"
wire_api = "responses"
env_key = "SECRET_VARIABLE"
[model_providers.mine.http_headers]
Authorization = "opaque-private-value"
"""
    path.write_text(text)
    route = '/api/manage/hosts/codex/native/'
    preview = api.post(route+'preview', json={'kind':'providers'}).json()
    candidate, = preview['candidates']
    assert candidate['available'] and candidate['model_ids'] == ['chosen'] and candidate['requires_credential']
    assert candidate['base_url'] == 'https://local.example/v1'
    assert 'opaque-private-value' not in json.dumps(preview)
    payload = {'kind':'providers', 'selected_ids':[candidate['id']], 'fingerprint':preview['fingerprint'], 'confirmed':True}
    assert api.post(route+'import', json={**payload, 'confirmed':False}).status_code == 422
    path.write_text(text+'# external change\n')
    assert api.post(route+'import', json=payload).status_code == 409
    assert not manager.list('provider')
    path.write_text(text)
    receipt = api.post(route+'import', json=payload).json()
    assert receipt['imported'] == 1 and not receipt['host_files_changed'] and path.read_text() == text
    assert api.post(route+'import', json=payload).json()['unchanged'] == 1
    saved = manager.list('provider')[0]
    assert saved['credential_reattach_required'] and manager.list('model')[0]['model_id'] == 'chosen'
    assert 'opaque-private-value' not in json.dumps(manager.export())
    assert api.post('/api/manage/hosts/codex/quick-switch', json={'provider_id':saved['id'], 'auto_apply':True}).status_code == 409


def test_partial_mcp_requires_review_and_skills_copy_files_without_following_links(manager):
    path = manager.home/'.cursor/mcp.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'mcpServers':{'lookup':{'command':'local-command','args':['--stdio'], 'env':{'PRIVATE':'opaque'}}}}))
    source = native_catalog.preview(manager, 'cursor','mcp')
    mcp, = source['candidates']
    assert mcp['available'] and mcp['needs_review']
    imported = native_catalog.import_selection(manager,'cursor',{'kind':'mcp','selected_ids':[mcp['id']], 'fingerprint':source['fingerprint'],'confirmed':True})
    entry = imported['entities'][0]['id']
    with pytest.raises(ConfigurationError, match='Review'):
        manager.plan({'host_id':'claude-code','asset_ids':[entry]})
    asset = manager.get('asset', entry)
    assert 'opaque' not in json.dumps(asset)
    manager.save('asset',{'config':asset['config'], 'mcp_reviewed':True},entry,asset['version'])
    with pytest.raises(ConfigurationError,match='Native source'):
        manager.plan({'host_id':'cursor','asset_ids':[entry]})
    assert manager.plan({'host_id':'claude-code','asset_ids':[entry]})['status'] == 'planned'
    skill = manager.home/'.cursor/skills/demo'
    skill.mkdir(parents=True)
    (skill/'SKILL.md').write_text('# Demo')
    (skill/'guide.txt').write_text('Use the local project.')
    rogue = skill.parent/'linked'
    rogue.symlink_to(manager.home)
    source = native_catalog.preview(manager,'cursor','skills')
    candidates = {item['name']:item for item in source['candidates']}
    assert not candidates['linked']['available']
    receipt = native_catalog.import_selection(manager,'cursor',{'kind':'skills','selected_ids':[candidates['demo']['id']], 'fingerprint':source['fingerprint'],'confirmed':True})
    assert manager.get('asset',receipt['entities'][0]['id'])['files'] == {'SKILL.md':'# Demo','guide.txt':'Use the local project.'}


def test_multiple_models_need_explicit_selection_and_first_takeover_needs_review(manager):
    provider = manager.save('provider', {'name':'Multi','protocol':'openai','base_url':'https://fixture.example/v1','model_map':{'a':'one','b':'two'}})
    assert not manager.catalog_page('provider')['items'][0].get('default_model')
    with pytest.raises(ConfigurationError,match='explicit model'):
        manager.quick_switch_provider('codex',provider['id'],auto_apply=True)
    assert manager.get('provider', provider['id'])['version'] == 1
    path = manager.home/'.codex/config.toml'
    path.parent.mkdir()
    path.write_text('model = "old"\n')
    result = manager.quick_switch_provider('codex', provider['id'],'two',auto_apply=True)
    assert result['status'] == 'review_required' and path.read_text() == 'model = "old"\n'


def test_undo_restores_immediate_files_selection_ownership_and_resists_external_edits(manager, monkeypatch):
    api = client(manager, monkeypatch)
    original = b'# original\nmodel = "pre-existing"\n'
    path = manager.home/'.codex/config.toml'
    path.parent.mkdir()
    path.write_bytes(original)
    first = manager.setup_provider(setup())
    second = manager.setup_provider({**setup(token='setup-request-0002'), 'model_id':'second'})
    def switch(obj):
        plan = manager.quick_switch_provider('codex',obj['provider']['id'],obj['model']['id'])['plan']
        return manager.apply(plan['id'],1,True)
    first_plan = switch(first)
    first_bytes = path.read_bytes()
    second_plan = switch(second)
    assert path.read_bytes() != first_bytes
    url = '/api/manage/plans/'+second_plan['id']+'/undo/preview'
    assert api.post('/api/manage/plans/'+first_plan['id']+'/undo/preview').status_code == 409
    response = api.post(url)
    assert response.status_code == 200, response.text
    undo = response.json()
    assert setup()['secret'] not in response.text and 'undo_changes' not in response.text
    path.write_bytes(path.read_bytes()+b'# external\n')
    assert api.post('/api/manage/plans/'+undo['id']+'/apply',json={'confirmed':True,'expected_version':1}).status_code == 409
    assert path.read_bytes().endswith(b'# external\n')
    path.write_bytes(path.read_bytes().removesuffix(b'# external\n'))
    undo = api.post(url).json()
    applied = api.post('/api/manage/plans/'+undo['id']+'/apply',json={'confirmed':True,'expected_version':1})
    assert applied.status_code == 200,applied.text
    assert applied.json()['payload']['readback'] == 'verified' and path.read_bytes() == first_bytes
    state = manager.host_state('codex')['selection']
    assert state['status'] == 'verified' and state['provider_id'] == first['provider']['id']
    restore = manager.plan({'host_id':'codex','operation':'restore'})
    manager.apply(restore['id'],1,True)
    assert 'pre-existing' in path.read_text() and manager.host_state('codex')['selection']['provider_id'] is None


@pytest.mark.parametrize('mode', ['copy', 'symlink'])
def test_skill_undo_reverts_files_registry_and_ownership(manager, mode):
    item = manager.save('asset',{'kind':'skill','name':'demo','files':{'SKILL.md':'hi'},'install_mode':mode})
    plan = manager.plan({'host_id':'codex','asset_ids':[item['id']]})
    applied = manager.apply(plan['id'],1,True)
    path = manager.home/'.codex/skills'/item['id']/'SKILL.md'
    assert path.read_text().split('---\n\n', 1)[1] == 'hi'
    undo = management_undo.preview(manager,applied['id'])
    manager.apply(undo['id'],1,True)
    assert not path.exists() and not path.is_symlink() and not manager.transactions._skill_registry(str(path))


def test_claude_takeover_never_forwards_external_key_and_restores_it(manager):
    path = manager.home/'.claude/settings.json'
    path.parent.mkdir()
    native = {'env':{'ANTHROPIC_AUTH_TOKEN':'opaque-token', 'ANTHROPIC_API_KEY':'opaque-api-key',
                     'ANTHROPIC_CUSTOM_HEADERS':'private-header', 'UNRELATED':'keep'}}
    path.write_text(json.dumps(native))
    p = manager.save('provider', {'name':'Mine','protocol':'anthropic','base_url':'https://mine.example','model_map':{'default':'chosen'}})
    with pytest.raises(ConfigurationError,match='authentication'):
        manager.plan({'host_id':'claude-code','provider_id':p['id']})
    result = manager.quick_switch_provider('claude-code',p['id'],auto_apply=True)
    assert result['status'] == 'review_required'
    assert all(value not in json.dumps(result) for value in ('opaque-token','opaque-api-key','private-header'))
    manager.apply(result['plan_id'],1,True)
    env = json.loads(path.read_text())['env']
    assert env['UNRELATED'] == 'keep'
    assert not any(env.get(key) for key in ('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY','ANTHROPIC_CUSTOM_HEADERS'))
    restore = manager.plan({'host_id':'claude-code','operation':'restore'})
    manager.apply(restore['id'],1,True)
    assert json.loads(path.read_text()) == native


def test_native_updates_are_previewed_and_independent_edits_conflict(manager):
    path = manager.home/'.codex/AGENTS.md'
    path.parent.mkdir()
    path.write_text('# initial')
    def choose():
        source = native_catalog.preview(manager,'codex','rules')
        candidate, = source['candidates']
        return candidate,dict(kind='rules',fingerprint=source['fingerprint'],selected_ids=[candidate['id']],confirmed=True)
    candidate, body = choose()
    assert candidate['disposition'] == 'create'
    item = native_catalog.import_selection(manager,'codex',body)['entities'][0]
    path.write_text('# revised')
    candidate, body = choose()
    assert candidate['disposition'] == 'update'
    native_catalog.import_selection(manager,'codex',body)
    saved = manager.get('asset',item['id'])
    assert saved['version'] == 2 and saved['content'] == '# revised'
    manager.save('asset',{'content':'edited in Senrelay'},saved['id'],saved['version'])
    path.write_text('# another change')
    candidate, body = choose()
    assert not candidate['available'] and candidate['disposition'] == 'conflict'
    with pytest.raises(ConfigurationError,match='both changed'):
        native_catalog.import_selection(manager,'codex',body)
    assert manager.get('asset',item['id'])['content'] == 'edited in Senrelay'


def test_preview_withholds_secret_repeated_in_native_name_or_endpoint(manager):
    path = manager.home/'.config/opencode/opencode.json'
    path.parent.mkdir(parents=True)
    secret = 'opaque-private-credential'
    providers = {name: {'npm':'@ai-sdk/openai','options':{'apiKey':secret,'baseURL':'https://example.org/'+secret}}
                 for name in (secret, 'otherwise-safe')}
    path.write_text(json.dumps({'provider':providers}))
    output = native_catalog.preview(manager,'opencode','providers')
    assert secret not in json.dumps(output)
    assert not any(item['available'] for item in output['candidates'])


@pytest.mark.parametrize(('host', 'relative', 'document', 'selection_path', 'selection'), [
    ('omp', '.omp/agent/models.yml', 'providers:\n  provider_local:\n    baseUrl: https://api.example.org/v1\n    api: openai-responses\n    models:\n      - id: one\n        contextWindow: 8192\n      - id: two\n', '.omp/agent/config.yml', 'modelRoles:\n  default: provider_local/two\n'),
    ('opencode', '.config/opencode/opencode.json', json.dumps({'model':'provider_local/two','provider':{'provider_local':{'npm':'@ai-sdk/openai','options':{'baseURL':'https://api.example.org/v1'},'models':{'one':{},'two':{}}}}}), None, None),
])
def test_native_selector_and_capacity_match_existing_host_formats(manager, host, relative, document, selection_path, selection):
    path = manager.home/relative
    path.parent.mkdir(parents=True)
    path.write_text(document)
    if selection:
        (manager.home/selection_path).write_text(selection)
    preview = native_catalog.preview(manager,host,'providers')
    candidate, = preview['candidates']
    assert candidate['available'] and 'two' in candidate['model_ids']
    receipt = native_catalog.import_selection(manager,host,{'kind':'providers','selected_ids':[candidate['id']],'fingerprint':preview['fingerprint'],'confirmed':True})
    definition = manager.get('provider',receipt['entities'][0]['id'])
    assert definition['default_model'] == 'two'
    if host == 'omp':
        assert 8192 in definition['model_context_windows'].values()
        assert any(model.get('context_window') == 8192 for model in manager.list('model'))
    assert path.read_text() == document


def test_codex_both_surfaces_share_undo_warning_and_old_credentials_cannot_be_revived(manager):
    first = manager.setup_provider(setup())
    next_ = manager.setup_provider({**setup(token='setup-request-0002'),'model_id':'other'})
    for selected in (first, next_):
        change = manager.quick_switch_provider('codex',selected['provider']['id'],selected['model']['id'])['plan']
        manager.apply(change['id'],1,True)
    assert management_undo.preview(manager,change['id'])['payload']['metadata']['shared_configuration']
    manager.credential('provider',first['provider']['id'],'rotated-private-value')
    with pytest.raises(ConfigurationError,match='credentials changed'):
        management_undo.preview(manager,change['id'])


def take_native(manager, host, kind):
    preview = native_catalog.preview(manager,host,kind)
    candidate, = preview['candidates']
    assert candidate['available']
    return native_catalog.import_selection(manager,host,{'kind':kind,'selected_ids':[candidate['id']],
        'fingerprint':preview['fingerprint'],'confirmed':True})['entities'][0]['id']


def test_mcp_takeover_retains_name_and_neighbours_and_restores_snapshot(manager):
    path = manager.home/'.cursor/mcp.json'
    path.parent.mkdir()
    original = {'mcpServers':{'local_entry':{'command':'local-server','args':['--serve']}}, 'unrelated': {'retain':True}}
    path.write_text(json.dumps(original))
    baseline = path.read_bytes()
    entry = take_native(manager,'cursor','mcp')
    assert manager.host_state('cursor')['resources'][entry]['status'] == 'native_available'
    change = manager.quick_toggle_asset('cursor',entry,True,auto_apply=True)
    assert change['status'] == 'review_required' and path.read_bytes() == baseline
    manager.apply(change['plan_id'],1,True)
    assert json.loads(path.read_text()) == original
    assert manager.host_state('cursor')['resources'][entry]['status'] == 'verified'
    disable = manager.quick_toggle_asset('cursor',entry,False,auto_apply=True)
    assert disable['status'] == 'review_required'
    manager.apply(disable['plan_id'],1,True)
    assert json.loads(path.read_text()) == {**original,'mcpServers':{}}
    undo = management_undo.preview(manager,disable['plan_id'])
    manager.apply(undo['id'],1,True)
    assert json.loads(path.read_text()) == original


def test_native_mcp_reimport_upgrades_source_receipt_and_external_removal_is_never_an_uninstall(manager):
    path = manager.home/'.cursor/mcp.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'mcpServers': {'local': {'command':'server'}}}))
    entry = take_native(manager,'cursor','mcp')
    with manager.connect() as conn:
        conn.execute('DELETE FROM management_native_origins WHERE entity_id=?', (entry,))
    assert manager.host_state('cursor')['resources'][entry]['status'] == 'native_read_only'
    assert take_native(manager,'cursor','mcp') == entry
    plan = manager.quick_toggle_asset('cursor', entry, True, auto_apply=True)['plan']
    manager.apply(plan['id'],1,True)
    managed = path.read_bytes()
    path.write_text('{"mcpServers":{}}')
    with pytest.raises(ConfigurationError):
        manager.quick_toggle_asset('cursor', entry, True, auto_apply=True)
    path.write_bytes(managed)
    undo = management_undo.preview(manager, plan['id'])
    manager.apply(undo['id'],1,True)
    path.write_text('{"mcpServers":{}}')
    assert manager.host_state('cursor')['resources'][entry]['status'] == 'native_source_missing'
    with pytest.raises(ConfigurationError):
        manager.quick_toggle_asset('cursor', entry, True, auto_apply=True)


def test_native_rule_preserves_one_copy_and_conflicts_on_external_edit(manager):
    path = manager.home/'.codex/AGENTS.md'
    path.parent.mkdir()
    path.write_text('# original rule\n')
    entry = take_native(manager,'codex','rules')
    assert manager.host_state('codex')['resources'][entry]['status'] == 'native_available'
    other = manager.save('asset', {'kind':'prompt', 'name':'other', 'content':'separate'})
    with pytest.raises(ConfigurationError, match='overlaps'):
        manager.plan({'host_id':'codex', 'asset_ids':[entry, other['id']]})
    plan = manager.plan({'host_id':'codex','asset_ids':[entry]})
    path.write_text('# edited externally\n')
    with pytest.raises(ConfigurationError):
        manager.apply(plan['id'],1,True)
    assert manager.host_state('codex')['resources'][entry]['status'] == 'native_source_changed'
    path.write_text('# original rule\n')
    plan = manager.plan({'host_id':'codex','asset_ids':[entry]})
    manager.apply(plan['id'],1,True)
    assert path.read_text() == '# original rule\n'
    with pytest.raises(ConfigurationError,match='overlaps'):
        manager.plan({'host_id':'codex','asset_ids':[other['id']]})
    undo = management_undo.preview(manager,plan['id'])
    manager.apply(undo['id'],1,True)
    assert path.read_text() == '# original rule\n'


def test_native_skills_are_not_duplicated_in_same_or_shared_path(manager):
    path = manager.home/'.codex/skills/local/SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text('# test')
    entry = take_native(manager,'codex','skills')
    for host in ('codex','codex'):
        assert manager.host_state(host)['resources'][entry]['status'] == 'native_read_only'
        with pytest.raises(ConfigurationError):
            manager.quick_toggle_asset(host,entry,True,auto_apply=True)
    assert [child.name for child in path.parent.parent.iterdir()] == ['local']
    plan = manager.plan({'host_id':'claude-code','asset_ids':[entry]})
    manager.apply(plan['id'],1,True)
    assert len(list((manager.home/'.claude/skills').rglob('SKILL.md'))) == 1


def test_deleted_definition_stays_deleted_after_uninstall(manager):
    asset = manager.save('asset',{'kind':'mcp','name':'server','config':{'command':'local'}})
    for op in ('install','uninstall'):
        plan = manager.plan({'host_id':'cursor','asset_ids':[asset['id']],'operation':op})
        manager.apply(plan['id'],1,True)
    manager.delete('asset',asset['id'],asset['version'])
    with pytest.raises(ConfigurationError,match='definitions'):
        management_undo.preview(manager,plan['id'])
    assert json.loads((manager.home/'.cursor/mcp.json').read_text())['mcpServers'] == {}


def test_undo_rechecks_deleted_or_revised_asset_after_its_preview(manager):
    asset = manager.save('asset', {'kind':'mcp', 'name':'my-server', 'config':{'command':'one'}})
    for op in ('install', 'uninstall'):
        plan = manager.plan({'host_id':'cursor', 'asset_ids':[asset['id']], 'operation':op})
        manager.apply(plan['id'], 1, True)
    undo = management_undo.preview(manager, plan['id'])
    manager.save('asset', {'config':{'command':'two'}}, asset['id'], asset['version'])
    with pytest.raises(ConfigurationError):
        manager.apply(undo['id'], 1, True)
    assert json.loads((manager.home/'.cursor/mcp.json').read_text())['mcpServers'] == {}


def test_codex_toml_takes_over_shared_native_mcp_table_without_touching_neighbours(manager):
    import tomllib
    path = manager.home/'.codex/config.toml'
    path.parent.mkdir()
    before = '# my options\nmodel="custom"\n[mcp_servers."my.local"]\ncommand="tool"\nargs=["--ready"]\n# keep this comment\n[features]\nfast=true\n'
    path.write_text(before)
    key = take_native(manager,'codex','mcp')
    assert manager.host_state('codex')['resources'][key]['status'] == 'native_available'
    plan = manager.quick_toggle_asset('codex', key, True, auto_apply=True)
    assert plan['status'] == 'review_required'
    manager.apply(plan['plan_id'],1,True)
    assert path.read_text() == before
    assert manager.host_state('codex')['resources'][key]['status'] == 'verified'
    asset = manager.get('asset',key)
    manager.save('asset',{'config':{'transport':'stdio','command':'new-tool','args':['--ready']}},key,asset['version'])
    plan = manager.plan({'host_id':'codex','asset_ids':[key]})
    manager.apply(plan['id'],1,True)
    after = tomllib.loads(path.read_text())
    assert after['mcp_servers'] == {'my.local':{'command':'new-tool','args':['--ready']}}
    assert after['features'] == {'fast':True} and '# keep this comment' in path.read_text()
    plan = manager.plan({'host_id':'codex','asset_ids':[key],'operation':'uninstall'})
    manager.apply(plan['id'],1,True)
    assert 'mcp_servers' not in tomllib.loads(path.read_text())
    undo = management_undo.preview(manager,plan['id'])
    manager.apply(undo['id'],1,True)
    assert tomllib.loads(path.read_text()) == after


@pytest.mark.parametrize('first,second', [('codex', 'codex'), ('codex', 'codex')])
def test_shared_source_import_reuses_one_definition_and_preserves_edits(manager, first, second):
    skill = manager.home / '.codex/skills/local-notes'
    skill.mkdir(parents=True)
    (skill/'SKILL.md').write_text('# Notes')
    attachment = skill/'example.md'
    attachment.write_text('First example')

    def select(host):
        source = native_catalog.preview(manager, host, 'skills')
        candidate, = source['candidates']
        return candidate, {'kind':'skills', 'fingerprint':source['fingerprint'],
                           'selected_ids':[candidate['id']], 'confirmed': True}

    asset_id = native_catalog.import_selection(manager, first, select(first)[1])['entities'][0]['id']
    candidate, payload = select(second)
    assert candidate['disposition'] == 'unchanged'
    assert native_catalog.import_selection(manager, second, payload)['unchanged'] == 1
    assert manager.get('asset', asset_id)['version'] == 1
    attachment.write_text('Second example')
    candidate, payload = select(second)
    assert candidate['disposition'] == 'update'
    assert native_catalog.import_selection(manager, second, payload)['entities'][0]['id'] == asset_id
    assert manager.get('asset', asset_id)['files']['example.md'] == 'Second example'
    assert manager.get('asset', asset_id)['source']['host_id'] == first
    manager.save('asset', {'name':'Renamed in the library'}, asset_id, 2)
    assert select(second)[0]['disposition'] == 'unchanged'
    attachment.write_text('Concurrent external edit')
    candidate, payload = select(second)
    assert candidate['disposition'] == 'conflict' and not candidate['available']
    with pytest.raises(ConfigurationError,match='both changed'):
        native_catalog.import_selection(manager, second, payload)
    assert len(manager.list('asset')) == 1
