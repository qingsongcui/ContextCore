import importlib
import json

import pytest

from app.integrations.config_transactions import ConfigurationError
from app.integrations import structured_config as native


def apply(format, text, pointer, value=None, operation='install'):
    return native.render_document(format, text, [{'pointer': pointer, 'value': value, 'operation': operation}])


def test_yaml_hermes_merge_keeps_native_extras_and_foreign_comments():
    original = '''# user-owned Hermes header
model:
  provider: official # active choice
  default: native-current
mcp_servers:
  foreign:
    command: 'node' # foreign tool
    args: [server.js]
  senrelay:
    url: https://old.example.com/mcp
    auth: oauth # native auth policy
    tools: [safe_tool]
    timeout: 70
agent:
  max_turns: 19 # leave untouched
'''
    result = apply('yaml', original, ['mcp_servers', 'senrelay'], {'url': 'https://new.example.com/mcp', 'enabled': True}, 'merge')
    parsed = native.parse_document('yaml', result)
    assert parsed['mcp_servers']['senrelay'] == {'url': 'https://new.example.com/mcp', 'auth': 'oauth', 'tools': ['safe_tool'], 'timeout': 70, 'enabled': True}
    assert parsed['model'] == {'provider': 'official', 'default': 'native-current'}
    for comment in ['# user-owned Hermes header', '# active choice', '# foreign tool', '# native auth policy', '# leave untouched']:
        assert comment in result
    assert "command: 'node'" in result


def test_yaml_minimax_additive_provider_keeps_native_default_and_unknowns():
    original = '''# MiniMax native file
defaultModel: custom_provider:chosen/model # active native choice
lightModel: custom_provider:chosen/light
custom_provider:
  chosen:
    kind: custom
    options: {baseURL: 'https://existing.example.com'}
    models: {model: {}, light: {}}
unknown: [a, b] # user extension
'''
    provider = {'name': 'New', 'kind': 'custom', 'enabled': True, 'api': 'openai-responses',
                'options': {'baseURL': 'https://example.com/v1', 'apiKey': '{{credential:keychain:senrelay:' + 'a'*32 + '}}'},
                'models': {'new-model': {}}}
    result = apply('yaml', original, ['custom_provider', 'new'], provider)
    doc = native.parse_document('yaml', result)
    assert doc['defaultModel'] == 'custom_provider:chosen/model'
    assert doc['lightModel'] == 'custom_provider:chosen/light'
    assert doc['custom_provider']['chosen']['models'] == {'model': {}, 'light': {}}
    assert doc['custom_provider']['new'] == provider
    assert doc['unknown'] == ['a', 'b'] and '# active native choice' in result and '# user extension' in result


def test_yaml_alias_update_does_not_change_foreign_reference():
    original = '''defaults: &defaults
  timeout: 10 # keep anchored value
mcp_servers:
  foreign: *defaults
  owned: *defaults
'''
    result = apply('yaml', original, ['mcp_servers', 'owned', 'timeout'], 30)
    doc = native.parse_document('yaml', result)
    assert doc['defaults']['timeout'] == 10 and doc['mcp_servers']['foreign']['timeout'] == 10
    assert doc['mcp_servers']['owned']['timeout'] == 30
    assert '# keep anchored value' in result


def test_yaml_unselected_anchors_and_quotes_roundtrip():
    original = '''# first
defaults: &defaults {timeout: 10}
foreign: *defaults
quoted: "literal: # string"
mcp_servers: {}
'''
    result = apply('yaml', original, ['mcp_servers', 'new'], {'command': 'node', 'args': ['server.js'], 'enabled': True})
    assert '&defaults' in result and '*defaults' in result and '"literal: # string"' in result
    assert '# first' in result


def test_json5_openclaw_provider_preserves_comment_tokens_and_foreign_fields():
    original = '''// OpenClaw native header
{
  models: {
    mode: 'merge', // retain mode
    providers: {
      foreign: { baseUrl: 'https://existing.example.com', api: 'openai-responses' }, // old provider
    },
  },
  gateway: { port: 0x4e20, /* user network */ bind: 'loopback', },
  enabled: true, // final comment
}
'''
    provider = {'baseUrl': 'https://example.com/v1', 'api': 'anthropic-messages', 'models': [{'id': 'fixture-model'}]}
    result = apply('json5', original, ['models', 'providers', 'senrelay'], provider)
    doc = native.parse_document('json5', result)
    assert doc['models']['mode'] == 'merge' and doc['models']['providers']['senrelay'] == provider
    assert doc['models']['providers']['foreign']['baseUrl'] == 'https://existing.example.com'
    assert doc['gateway']['port'] == 0x4e20 and 'port: 0x4e20' in result
    for comment in ['// OpenClaw native header', '// retain mode', '// old provider', '/* user network */', '// final comment']:
        assert comment in result
    assert "mode: 'merge'" in result


def test_json5_replace_preserves_target_outer_comment_and_safe_strings():
    original = "{ providers: { owned: 'before' /* target policy */ }, trailing: 'keep' }"
    value = 'x"\n}, injected: true, //\\ dangerous string'
    result = apply('json5', original, ['providers', 'owned'], value)
    doc = native.parse_document('json5', result)
    assert doc == {'providers': {'owned': value}, 'trailing': 'keep'}
    assert '/* target policy */' in result


def test_json5_remove_retains_foreign_inline_comments_and_syntax():
    original = '''{
  foreign: 1, // foreign comment
  owned: 2, // removed entry trailing comment
  last: 3, /* last foreign comment */
}
'''
    result = apply('json5', original, ['owned'], operation='remove')
    assert native.parse_document('json5', result) == {'foreign': 1, 'last': 3}
    assert '// foreign comment' in result and '/* last foreign comment */' in result
    last = apply('json5', "{ owned: 2, // retain surrounding note\n}", ['owned'], operation='remove')
    assert native.parse_document('json5', last) == {} and '// retain surrounding note' in last


def test_json5_array_pointer_hermes_style_named_list_preserves_other_elements():
    original = "{ providers: [ {name:'foreign', value:1}, /* between */ {name:'owned', value:2}, ], other: true }"
    result = apply('json5', original, ['providers', 1, 'value'], 3)
    assert native.parse_document('json5', result)['providers'] == [{'name': 'foreign', 'value': 1}, {'name': 'owned', 'value': 3}]
    assert '/* between */' in result


def test_yaml_hermes_custom_provider_list_changes_only_selected_field():
    original = '''custom_providers:
- name: foreign
  api_key: original-host-owned
  base_url: https://foreign.example.com # foreign endpoint
- name: selected
  api_key: selected-host-owned
  base_url: https://old.example.com
'''
    result = apply('yaml', original, ['custom_providers', 1, 'base_url'], 'https://new.example.com')
    doc = native.parse_document('yaml', result)
    assert doc['custom_providers'][0]['api_key'] == 'original-host-owned'
    assert doc['custom_providers'][1]['api_key'] == 'selected-host-owned'
    assert '# foreign endpoint' in result


@pytest.mark.parametrize('format,text', [('yaml', '# header\nx: 1\n'), ('json5', "// header\n{x:'one',}\n")])
def test_noop_and_idempotent_ops_do_not_reformat(format, text):
    assert native.render_document(format, text, []) == text
    assert apply(format, text, ['missing'], operation='remove') == text
    value = native.parse_document(format, text)['x']
    assert apply(format, text, ['x'], value) == text


@pytest.mark.parametrize('format,text', [('yaml', 'x: 1\nx: 2\n'), ('json5', '{x:1, x:2}'), ('yaml', '!!python/object/apply:os.system [touch injected]')])
def test_ambiguous_or_tagged_native_source_is_not_rewritten(format, text):
    with pytest.raises(ConfigurationError): apply(format, text, ['new'], 3)


def test_missing_optional_dependency_is_runtime_unavailable(monkeypatch):
    original = native.import_module
    def missing(name):
        if name.startswith('ruamel'): raise ModuleNotFoundError(name)
        return original(name)
    monkeypatch.setattr(native, 'import_module', missing)
    assert native.runtime_status('yaml')['status'] == 'runtime_unavailable'
    with pytest.raises(native.StructuredFormatUnavailable) as exc:
        apply('yaml', 'a: 1\n', ['a'], 2)
    assert exc.value.status_code == 503 and exc.value.code == 'format_runtime_unavailable'


@pytest.mark.parametrize('format', ['yaml', 'json5'])
def test_scalar_ancestor_and_sparse_array_are_refused(format):
    text = 'a: scalar\nitems: []\n' if format == 'yaml' else '{a:"scalar",items:[]}'
    with pytest.raises(ConfigurationError): apply(format, text, ['a', 'nested'], 1)
    with pytest.raises(ConfigurationError): apply(format, text, ['items', 3], 1)


def test_recursive_yaml_alias_is_refused_before_ruamel_normalizes_it_to_null():
    original = 'foreign: &cycle {self: *cycle}\nowned: {}\n'
    with pytest.raises(ConfigurationError):
        apply('yaml', original, ['owned', 'entry'], {'enabled': True})


def test_yaml_merged_alias_nested_update_does_not_mutate_foreign_base():
    original = 'base: &base\n  nested: {value: 1}\nowned:\n  <<: *base\nforeign: *base\n'
    result = apply('yaml', original, ['owned', 'nested', 'value'], 3)
    doc = native.parse_document('yaml', result)
    assert doc['base']['nested']['value'] == 1 and doc['foreign']['nested']['value'] == 1
    assert doc['owned']['nested']['value'] == 3 and '<<: *base' in result


@pytest.mark.parametrize('format,text', [('yaml','owned: {command: node, timeout: 30} # native policy\n'), ('json5',"{owned:{command:'node', timeout:30 /* native policy */}}")])
def test_explicit_child_removal_then_merge_preserves_native_options(format,text):
    result = native.render_document(format,text,[{'pointer':['owned','command'],'operation':'remove'},
        {'pointer':['owned'],'operation':'merge','value':{'url':'https://example.com/mcp'}}])
    assert native.parse_document(format,result)['owned'] == {'timeout':30,'url':'https://example.com/mcp'}
    assert 'native policy' in result
