import json
import tomllib
from pathlib import Path

import pytest

from app.integrations.config_transactions import ConfigurationError, ConfigurationTransactions
from app.integrations.host_formats_v2 import (
    HOST_FORMATS, PROVIDER_FORMAT_EVIDENCE, UPSTREAM_COMMIT, render_mcp_specs,
    render_provider_specs, validate_prompt,
)

class Vault:
    def __init__(self, value='fixture-key'):
        self.value = value
    def get(self, reference):
        return self.value


def spec(tmp_path, host, config, name='senrelay_example'):
    return render_mcp_specs(host, HOST_FORMATS[host], config, name=name, owner='asset_example', operation='install', home=tmp_path)[0]


def test_grok_toml_infers_transport_and_uses_native_headers(tmp_path):
    result = spec(tmp_path, 'grok-build', {'transport': 'http', 'url': 'https://example.com/mcp', 'headers': {'X-Trace': 'fixture'}})
    native = tomllib.loads(result['value'])['mcp_servers']['senrelay_example']
    assert native == {'url': 'https://example.com/mcp', 'headers': {'X-Trace': 'fixture'}}
    assert 'http_headers' not in native and 'type' not in native
    assert result['format'] == 'toml'
    assert result['path'] == str(tmp_path / '.grok/config.toml')


def test_kimi_native_transport_and_minimax_enabled_mapping(tmp_path):
    kimi = spec(tmp_path, 'kimi-code', {'transport': 'sse', 'url': 'https://example.com/mcp'})
    assert kimi['value'] == {'url': 'https://example.com/mcp', 'transport': 'sse', 'enabled': True}
    mini = spec(tmp_path, 'minimax-code', {'transport': 'http', 'url': 'https://example.com/mcp'})
    assert mini['value']['type'] == 'http' and mini['value']['enabled'] is True
    assert HOST_FORMATS['minimax-code']['provider_format'] == 'yaml'
    assert HOST_FORMATS['minimax-code']['provider_path'] == '.minimax/config.yaml'


@pytest.mark.parametrize('host', ['pi', 'antigravity-cli'])
def test_sse_is_rejected_for_native_hosts(tmp_path, host):
    with pytest.raises(ConfigurationError, match='not SSE'):
        spec(tmp_path, host, {'transport': 'sse', 'url': 'https://example.com/sse'})


def test_agy_cli_uses_url_and_disabled_without_desktop_serverurl(tmp_path):
    result = spec(tmp_path, 'antigravity-cli', {'transport': 'http', 'url': 'https://example.com/mcp'})
    assert result['value'] == {'url': 'https://example.com/mcp', 'disabled': False}
    assert 'serverUrl' not in result['value'] and 'type' not in result['value']
    assert HOST_FORMATS['antigravity-cli']['prompt_path'] == '.gemini/config/AGENTS.md'
    assert 'provider_path' not in HOST_FORMATS['antigravity-cli']


def test_pi_normalized_name_collision_preserves_existing_file(tmp_path):
    path = tmp_path / '.pi/agent/mcp.json'; path.parent.mkdir(parents=True)
    original = json.dumps({'mcpServers': {'senrelay-example': {'command': 'foreign'}}})
    path.write_text(original)
    with pytest.raises(ConfigurationError, match='identical'):
        spec(tmp_path, 'pi', {'command': 'fixture'}, name='senrelay_example')
    assert path.read_text() == original


def test_native_yaml_and_json5_are_capabilities_not_permanent_unsupported(tmp_path):
    assert HOST_FORMATS['hermes']['format'] == 'yaml' and HOST_FORMATS['hermes']['mcp_path']
    assert HOST_FORMATS['hermes']['provider_path']
    assert HOST_FORMATS['openclaw']['provider_format'] == 'json5' and HOST_FORMATS['openclaw']['provider_path']
    assert 'mcp_path' not in HOST_FORMATS['openclaw']
    with pytest.raises(ConfigurationError):
        spec(tmp_path, 'openclaw', {'command': 'fixture'})


def provider():
    return {'id': 'provider_example', 'protocol': 'openai', 'base_url': 'https://example.com/v1',
            'credential_ref': 'keychain:senrelay:' + 'a' * 32, 'model_map': {'friendly': 'native-model'},
            'model_context_windows': {'native-model': 32000}}


def test_kimi_requires_explicit_model_capacity_and_does_not_select_default(tmp_path):
    config = provider(); del config['model_context_windows']
    with pytest.raises(ConfigurationError, match='model_context_windows'):
        render_provider_specs('kimi-code', HOST_FORMATS['kimi-code'], config, 'install', home=tmp_path)
    result = render_provider_specs('kimi-code', HOST_FORMATS['kimi-code'], provider(), 'install', home=tmp_path)[0]
    value = tomllib.loads(result['value'])
    assert value['providers']['provider_example']['type'] == 'openai'
    assert value['models']['provider_example:friendly'] == {'model': 'native-model', 'provider': 'provider_example', 'max_context_size': 32000}
    assert 'default_model' not in value and 'oauth' not in result['value']


def test_kimi_toml_secret_is_data_and_existing_defaults_survive_transaction(tmp_path):
    path = tmp_path / '.kimi-code/config.toml'; path.parent.mkdir(parents=True)
    path.write_text('default_model = "native-existing"\n[providers.existing]\ntype = "kimi"\n')
    result = render_provider_specs('kimi-code', HOST_FORMATS['kimi-code'], provider(), 'install', home=tmp_path)
    secret = 'quote"\n[evil]\nx="injected'
    transaction = ConfigurationTransactions(tmp_path / 'app.db', tmp_path / 'snapshots', [tmp_path], Vault(secret))
    plan = transaction.create(result)
    transaction.apply(plan['id'], plan['version'], confirmed=True)
    value = tomllib.loads(path.read_text())
    assert value['default_model'] == 'native-existing' and value['providers']['existing']['type'] == 'kimi'
    assert value['providers']['provider_example']['api_key'] == secret
    assert 'evil' not in value


def test_pi_provider_uses_models_file_and_preserves_native_selection(tmp_path):
    result = render_provider_specs('pi', HOST_FORMATS['pi'], provider(), 'install', home=tmp_path)[0]
    assert result['path'] == str(tmp_path / '.pi/agent/models.json')
    assert result['pointer'] == ['providers', 'provider_example']
    assert result['value']['models'] == [{'id': 'native-model', 'name': 'friendly'}]
    assert result['value']['api'] == 'openai-completions'
    assert not any('default' in k.lower() for k in result['value'])


def test_grok_provider_global_selection_is_explicitly_unsupported(tmp_path):
    assert 'provider_path' not in HOST_FORMATS['grok-build']
    assert PROVIDER_FORMAT_EVIDENCE['grok-build']['selection'] == 'models.default'
    with pytest.raises(ConfigurationError, match='default'):
        render_provider_specs('grok-build', HOST_FORMATS['grok-build'], provider(), 'install', home=tmp_path)


def test_prompt_constraints_are_native_utf8_bytes():
    validate_prompt('minimax-code', 'a' * 32768)
    with pytest.raises(ConfigurationError, match='32 KiB'):
        validate_prompt('minimax-code', '中' * 10923)
    validate_prompt('hermes', '中' * 10923)
    assert len(UPSTREAM_COMMIT) == 40


def test_renderer_rejects_path_escape_and_invalid_native_key(tmp_path):
    external = tmp_path / 'outside'; external.mkdir(); home = tmp_path / 'home'; home.mkdir()
    (home / '.pi').symlink_to(external, target_is_directory=True)
    with pytest.raises(ConfigurationError, match='escapes'):
        spec(home, 'pi', {'command': 'fixture'})
    with pytest.raises(ConfigurationError, match='managed keys'):
        spec(tmp_path, 'grok-build', {'command': 'fixture'}, name='bad\n[table]')



def apply_native_specs(home, specs):
    from app.integrations.structured_config import render_document
    for spec in specs:
        path = Path(spec['path'])
        text = path.read_text() if path.exists() else '{}'
        operations = [{'pointer': spec['pointer'] + [field], 'operation': 'remove'} for field in spec.get('remove_fields', [])]
        operations.append({'pointer': spec['pointer'], 'value': spec['value'],
                           'operation': ('merge' if spec.get('merge') else 'install') if spec['operation'] != 'uninstall' else 'remove'})
        output = render_document(spec['kind'], text, operations)
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(output)
    return output


def test_hermes_mcp_native_yaml_transport_update_keeps_extra_fields_comments(tmp_path):
    from app.integrations.structured_config import parse_document
    path = tmp_path / '.hermes/config.yaml'; path.parent.mkdir(parents=True)
    path.write_text('# host settings\nmcp_servers:\n  senrelay_example:\n    command: old\n    args: [old.js]\n    auth: oauth # native policy\n    timeout: 70\n    tools: [only_safe]\nmodel: {provider: official}\n')
    result = spec(tmp_path, 'hermes', {'transport': 'http', 'url': 'https://example.com/mcp'})
    assert result['kind'] == 'yaml' and result['merge']
    output = apply_native_specs(tmp_path, [result]); doc = parse_document('yaml', output)
    assert doc['mcp_servers']['senrelay_example'] == {'url': 'https://example.com/mcp', 'enabled': True, 'auth': 'oauth', 'timeout': 70, 'tools': ['only_safe']}
    assert '# native policy' in output and doc['model']['provider'] == 'official'


def test_hermes_provider_uses_named_sequence_and_preserves_foreign_entries(tmp_path):
    from app.integrations.structured_config import parse_document
    path = tmp_path / '.hermes/config.yaml'; path.parent.mkdir(parents=True)
    path.write_text('# own settings\ncustom_providers:\n- name: foreign\n  base_url: https://foreign.example.com # leave\n- name: provider_example\n  key_env: USER_KEY # native key env\n  request_timeout_seconds: 300\n  models: {old-model: {}}\nmodel: {provider: foreign, default: original}\n')
    config = provider(); config['protocol'] = 'anthropic'
    specs = render_provider_specs('hermes', HOST_FORMATS['hermes'], config, 'install', home=tmp_path)
    assert specs[0]['pointer'] == ['custom_providers', 1]
    output = apply_native_specs(tmp_path, specs); doc = parse_document('yaml', output)
    assert doc['custom_providers'][0]['name'] == 'foreign' and '# leave' in output
    own = doc['custom_providers'][1]
    assert own['api_mode'] == 'anthropic_messages' and own['model'] == 'native-model'
    assert own['models'] == {'native-model': {}} and own['request_timeout_seconds'] == 300
    assert own['key_env'] == 'USER_KEY' and '# native key env' in output
    assert doc['model'] == {'provider': 'foreign', 'default': 'original'}


def test_minimax_provider_yaml_keeps_default_and_blocks_selected_model_removal(tmp_path):
    from app.integrations.structured_config import parse_document
    path = tmp_path / '.minimax/config.yaml'; path.parent.mkdir(parents=True)
    path.write_text('# native MiniMax\ndefaultModel: custom_provider:provider_example/native-model\ndefaultLightModel: custom_provider:provider_example/native-model\ncustom_provider:\n  provider_example:\n    name: Existing\n    unknownNative: keep # untouched\n    models: {native-model: {}}\n')
    specs = render_provider_specs('minimax-code', HOST_FORMATS['minimax-code'], provider(), 'install', home=tmp_path)
    output = apply_native_specs(tmp_path, specs); doc = parse_document('yaml', output)
    assert doc['custom_provider']['provider_example']['api'] == 'openai-completions'
    assert doc['custom_provider']['provider_example']['unknownNative'] == 'keep' and '# untouched' in output
    assert doc['defaultLightModel'] == 'custom_provider:provider_example/native-model'
    with pytest.raises(ConfigurationError, match='default/light'):
        render_provider_specs('minimax-code', HOST_FORMATS['minimax-code'], provider(), 'uninstall', home=tmp_path)
    changed = provider(); changed['model_map'] = {'new': 'other-model'}
    with pytest.raises(ConfigurationError):
        render_provider_specs('minimax-code', HOST_FORMATS['minimax-code'], changed, 'install', home=tmp_path)


def test_openclaw_provider_json5_keeps_comments_models_mode_and_native_selection(tmp_path):
    from app.integrations.structured_config import parse_document
    path = tmp_path / '.openclaw/openclaw.json'; path.parent.mkdir(parents=True)
    path.write_text("// native OpenClaw\n{models:{mode:'merge',providers:{foreign:{baseUrl:'https://foreign.example.com'}}},agents:{defaults:{model:{primary:'foreign/current',fallbacks:[]}}},gateway:{port:0x4e20},}")
    specs = render_provider_specs('openclaw', HOST_FORMATS['openclaw'], provider(), 'install', home=tmp_path)
    assert specs[0]['kind'] == 'json5'
    output = apply_native_specs(tmp_path, specs); doc = parse_document('json5', output)
    assert doc['models']['mode'] == 'merge' and 'foreign' in doc['models']['providers']
    assert doc['models']['providers']['provider_example']['api'] == 'openai-completions'
    assert doc['agents']['defaults']['model']['primary'] == 'foreign/current'
    assert '// native OpenClaw' in output and 'port:0x4e20' in output


@pytest.mark.parametrize('host,text', [('hermes','model: {provider: provider_example, default: native-model}\n'),('openclaw',"{agents:{defaults:{model:{primary:'provider_example/native-model'}}}}")])
def test_native_active_provider_removal_is_guarded(tmp_path,host,text):
    path = tmp_path / HOST_FORMATS[host]['provider_path']; path.parent.mkdir(parents=True); path.write_text(text)
    with pytest.raises(ConfigurationError,match='native'):
        render_provider_specs(host,HOST_FORMATS[host],provider(),'uninstall',home=tmp_path)


@pytest.mark.parametrize('host,original', [
    ('pi', "// native Pi settings\n{defaultProvider:'original',defaultModel:'old-model',thinkingLevel:'high',unknown:{keep:true},}"),
    ('kimi-code', "# native Kimi\ndefault_model = 'old#alias' # user selected model\n[thinking]\neffort = 'high'\n"),
    ('hermes', '# native Hermes\nmodel:\n  provider: original # user provider\n  default: old-model # user model\n  max_tokens: 8000\nagent: {max_turns: 30}\n'),
    ('minimax-code', '# native MiniMax\ndefaultModel: minimax/original # user model\ndefaultLightModel: minimax/light\nunknown: {keep: true}\n'),
    ('openclaw', "// native OpenClaw\n{agents:{defaults:{model:{primary:'original/old-model',fallbacks:['backup/model']}}},gateway:{port:0x4e20},}"),
])
def test_native_default_old_to_a_to_b_and_private_snapshot_restore(tmp_path, host, original):
    from app.integrations.host_formats_v2 import render_activation_specs, ACTIVATION_FORMATS
    from app.integrations.structured_config import parse_document
    definition = ACTIVATION_FORMATS[host]
    path = tmp_path / definition['path']; path.parent.mkdir(parents=True); path.write_text(original)
    tx = ConfigurationTransactions(tmp_path / 'app.db', tmp_path / 'snapshots', [tmp_path], Vault())
    first_provider = provider()
    a = render_activation_specs(host, first_provider, 'native-model', owner='activation:' + host, home=tmp_path, original_text=original)
    assert all(item['activation_field'] and item['takeover_selected_field'] for item in a)
    assert all(item['restore_metadata']['mode'] == 'first_activation_snapshot' for item in a)
    first = tx.create(a); applied_a = tx.apply(first['id'], first['version'], confirmed=True)
    second_provider = provider(); second_provider['id'] = 'provider_second'; second_provider['model_map'] = {'other': 'second-model'}; second_provider['model_context_windows'] = {'second-model': 64000}
    b = render_activation_specs(host, second_provider, 'second-model', owner='activation:' + host, home=tmp_path, original_text=path.read_text())
    second = tx.create(b); tx.apply(second['id'], second['version'], confirmed=True)
    text_b = path.read_text()
    if definition['format'] == 'toml':
        changed = tomllib.loads(text_b)
        assert changed['default_model'] == 'provider_second:other'
    else:
        changed = parse_document(definition['format'], text_b)
        if host == 'pi': assert changed['defaultProvider'] == 'provider_second' and changed['defaultModel'] == 'second-model'
        elif host == 'hermes': assert changed['model']['provider'] == 'provider_second' and changed['model']['max_tokens'] == 8000
        elif host == 'minimax-code': assert changed['defaultModel'] == 'custom_provider:provider_second/second-model' and changed['defaultLightModel'] == 'minimax/light'
        else: assert changed['agents']['defaults']['model']['primary'] == 'provider_second/second-model' and changed['agents']['defaults']['model']['fallbacks'] == ['backup/model']
    # Restore from the FIRST activation's private baseline, not provider A.
    baseline = applied_a['payload']['files'][0]
    restore_specs = [{**field, 'merge': False, 'value': '[Previous selected value]', 'relinquish': True,
                      'restore_from': {'snapshot': baseline['snapshot'], 'hash': baseline['before_hash'],
                                       'pointer': field['pointer'], 'format': definition['format']}}
                     for field in a]
    restore = tx.create(restore_specs); tx.apply(restore['id'], restore['version'], confirmed=True)
    restored = path.read_text()
    parse = tomllib.loads if definition['format'] == 'toml' else lambda text: parse_document(definition['format'], text)
    assert parse(restored) == parse(original)
    # Comments are part of the user's document, even when fields are selected.
    if host == 'kimi-code': assert '# user selected model' in restored and '# native Kimi' in restored
    elif host in {'pi', 'openclaw'}: assert original.split('\n')[0] in restored
    elif host == 'hermes': assert '# user provider' in restored and '# user model' in restored
    else: assert '# user model' in restored


@pytest.mark.parametrize('host', ['grok-build','antigravity-cli','cursor','omp','gemini-cli','claude-desktop'])
def test_unverified_surfaces_do_not_receive_invented_activation_fields(tmp_path,host):
    from app.integrations.host_formats_v2 import render_activation_specs
    with pytest.raises(ConfigurationError,match='not been verified'):
        render_activation_specs(host,provider(),'native-model',owner='activation:'+host,home=tmp_path)


def test_kimi_activation_targets_generated_model_key_and_requires_capacity(tmp_path):
    from app.integrations.host_formats_v2 import render_activation_specs
    config = provider()
    specs = render_activation_specs('kimi-code',config,'friendly',owner='activation:kimi-code',home=tmp_path)
    assert specs[0]['value'] == 'provider_example:friendly'
    del config['model_context_windows']
    with pytest.raises(ConfigurationError,match='context window'):
        render_activation_specs('kimi-code',config,'native-model',owner='activation:kimi-code',home=tmp_path)


def test_activation_does_not_read_native_files_without_explicit_text(tmp_path, monkeypatch):
    from app.integrations.host_formats_v2 import render_activation_specs
    def no_read(*args,**kwargs): raise AssertionError('activation renderer read a file')
    monkeypatch.setattr(Path,'read_text',no_read)
    assert render_activation_specs('hermes',provider(),'native-model',owner='activation:hermes',home=tmp_path)
    config = provider(); config['enabled'] = False
    with pytest.raises(ConfigurationError,match='disabled'):
        render_activation_specs('pi',config,'native-model',owner='activation:pi',home=tmp_path)
    with pytest.raises(ConfigurationError,match='declared map'):
        render_activation_specs('pi',provider(),'invented-model',owner='activation:pi',home=tmp_path)


class ManagedVault:
    def __init__(self): self.values = {}
    def put(self,value):
        reference = 'keychain:senrelay:' + f'{len(self.values)+1:032x}'
        self.values[reference] = value
        return reference
    def get(self,reference): return self.values[reference]
    def delete(self,reference): self.values.pop(reference,None)


_NATIVE_BASELINES = {
    'pi': "// native Pi settings\n{defaultProvider:'original',defaultModel:'old-model',thinkingLevel:'high',unknown:{keep:true},}",
    'kimi-code': "# native Kimi\ndefault_model = 'old#alias' # user selected model\n[thinking]\neffort = 'high'\n",
    'hermes': '# native Hermes\nmodel:\n  provider: original # user provider\n  default: old-model # user model\n  max_tokens: 8000\nagent: {max_turns: 30}\n',
    'minimax-code': '# native MiniMax\ndefaultModel: minimax/original # user model\ndefaultLightModel: minimax/light\nunknown: {keep: true}\n',
    'openclaw': "// native OpenClaw\n{agents:{defaults:{model:{primary:'original/old-model',fallbacks:['backup/model']}}},gateway:{port:0x4e20},}",
}


@pytest.mark.parametrize('host', list(_NATIVE_BASELINES))
def test_management_explicit_activation_a_b_restore_retains_baseline_and_comments(tmp_path, host):
    from app.services.management_service import ManagementService
    from app.integrations.host_formats_v2 import ACTIVATION_FORMATS
    from app.integrations.structured_config import parse_document
    manager = ManagementService(tmp_path / 'service.db', tmp_path / 'home', ManagedVault())
    definition = ACTIVATION_FORMATS[host]
    path = manager.home / definition['path']; path.parent.mkdir(parents=True)
    original = _NATIVE_BASELINES[host]; path.write_text(original)
    parse = tomllib.loads if definition['format'] == 'toml' else lambda value: parse_document(definition['format'], value)
    baseline = parse(original)
    selection_pointers = {
        'pi': [('defaultProvider',), ('defaultModel',)],
        'kimi-code': [('default_model',)],
        'hermes': [('model','provider'), ('model','default')],
        'minimax-code': [('defaultModel',)],
        'openclaw': [('agents','defaults','model')],
    }[host]
    def selected(document):
        output = []
        for pointer in selection_pointers:
            node = document
            for component in pointer: node = node[component]
            output.append(node)
        return output
    originals = selected(baseline)
    providers = []
    for name, native in [('A','native-a'), ('B','native-b')]:
        saved = manager.save('provider', {'name':name, 'base_url':'https://example.com/v1', 'protocol':'openai',
                                         'model_map':{'friendly':native}, 'model_context_windows':{native:32000}})
        manager.credential('provider', saved['id'], 'synthetic-secret-' + name)
        providers.append((saved,native))
    # Named-provider installation by itself preserves the native selection.
    saved,native = providers[0]
    coexist = manager.plan({'host_id':host, 'provider_id':saved['id']})
    manager.apply(coexist['id'],coexist['version'],True)
    assert selected(parse(path.read_text())) == originals
    for saved,native in providers:
        plan = manager.plan({'host_id':host,'provider_id':saved['id'],'activate_provider':True,'model_id':native})
        result = manager.apply(plan['id'],plan['version'],True)
        assert result['payload']['readback'] == 'verified'
        assert result['payload']['host_reload'] == 'not_run'
        current = selected(parse(path.read_text()))
        assert native in str(current) or (host == 'kimi-code' and saved['id'] + ':friendly' in current)
        assert saved['id'] in str(current)
    restore = manager.plan({'host_id':host,'operation':'restore'})
    manager.apply(restore['id'],restore['version'],True)
    restored_text = path.read_text(); restored = parse(restored_text)
    assert selected(restored) == originals
    assert original.split('\n')[0] in restored_text
    if host == 'pi': assert restored['thinkingLevel'] == 'high' and restored['unknown'] == baseline['unknown']
    elif host == 'kimi-code': assert restored['thinking'] == baseline['thinking'] and '# user selected model' in restored_text
    elif host == 'hermes': assert restored['model']['max_tokens'] == 8000 and '# user provider' in restored_text
    elif host == 'minimax-code': assert restored['defaultLightModel'] == 'minimax/light' and restored['unknown'] == baseline['unknown']
    else: assert restored['gateway'] == baseline['gateway']


def test_management_openclaw_absent_native_model_restores_absence(tmp_path):
    from app.services.management_service import ManagementService
    from app.integrations.structured_config import parse_document
    manager = ManagementService(tmp_path/'service.db',tmp_path/'home',ManagedVault())
    path = manager.home/'.openclaw/openclaw.json'; path.parent.mkdir(parents=True)
    path.write_text('// originally no model\n{agents:{defaults:{workspace:"fixture"}},gateway:{port:0x4e20}}')
    for name in ['A','B']:
        saved = manager.save('provider',{'name':name,'base_url':'https://example.com/v1','protocol':'openai','model_map':{'friendly':'native-'+name}})
        manager.credential('provider',saved['id'],'synthetic-'+name)
        plan = manager.plan({'host_id':'openclaw','provider_id':saved['id'],'activate_provider':True,'model_id':'friendly'})
        manager.apply(plan['id'],plan['version'],True)
    restore = manager.plan({'host_id':'openclaw','operation':'restore'})
    manager.apply(restore['id'],restore['version'],True)
    data = parse_document('json5',path.read_text())
    assert 'model' not in data['agents']['defaults']
    assert data['agents']['defaults']['workspace'] == 'fixture'
    assert '// originally no model' in path.read_text()
