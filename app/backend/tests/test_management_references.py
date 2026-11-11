"""Management deletion checks relationships, not unrelated resource bodies."""
import json
import sqlite3

import pytest

from app.integrations.config_transactions import ConfigurationError
from app.services import management_service
from tests.test_management import manager


def definition(manager, kind):
    if kind == 'provider':
        return manager.save(kind, {'name': 'Unused service', 'protocol': 'openai', 'base_url': 'https://fixture.example/v1'})
    if kind in ('binding', 'model'):
        provider = definition(manager, 'provider')
        return manager.save(kind, {'name': 'Unused model', 'provider_id': provider['id'], **({'host_id': 'codex'} if kind == 'binding' else {'model_id': 'unused-native'})})
    if kind == 'asset':
        return manager.save(kind, {'name': 'Unused rule', 'kind': 'prompt', 'content': 'Synthetic rule'})
    if kind == 'account':
        return manager.save(kind, {'name': 'Unused account', 'kind': 'api_key'})
    return manager.save('bundle', {'name': 'Unused combination', 'asset_ids': [], 'host_ids': ['codex']})


@pytest.mark.parametrize('kind', ['provider', 'account', 'model', 'binding', 'asset', 'bundle'])
def test_deleting_unused_definition_never_decodes_unrelated_skill_text(manager, monkeypatch, kind):
    victim = definition(manager, kind)
    marker = 'UNRELATED_SKILL_BODY_MUST_NOT_BE_DECODED'
    asset = manager.save('asset', {'name': 'Large unrelated Skill', 'kind': 'skill', 'files': {'SKILL.md': marker + '\n' + 'Synthetic reference text.\n' * 1000}})
    original = json.loads
    def bounded_decode(value, *args, **kwargs):
        if isinstance(value, str) and marker in value:
            raise AssertionError('Deletion decoded an unrelated Skill body')
        return original(value, *args, **kwargs)
    monkeypatch.setattr(management_service.json, 'loads', bounded_decode)
    result = manager.delete(kind, victim['id'], victim['version'])
    assert result['deleted'] is True
    with manager.connect() as conn:
        assert conn.execute('SELECT 1 FROM management_entities WHERE id=?', (victim['id'],)).fetchone() is None
        assert conn.execute('SELECT 1 FROM management_entities WHERE id=?', (asset['id'],)).fetchone() is not None


@pytest.mark.parametrize('kind', ['provider', 'account', 'model', 'asset'])
def test_typed_relationships_still_block_deletion(manager, kind):
    victim = definition(manager, kind)
    if kind == 'provider':
        manager.save('model', {'name': 'Dependent model', 'provider_id': victim['id'], 'model_id': 'dependent-native'})
    elif kind == 'account':
        provider = definition(manager, 'provider')
        manager.save('provider', {'account_id': victim['id']}, provider['id'], provider['version'])
    elif kind == 'model':
        manager.save('binding', {'host_id': 'codex', 'provider_id': victim['provider_id'], 'model_id': victim['id']})
    else:
        manager.save('bundle', {'name': 'Dependent combination', 'host_ids': ['codex'], 'asset_ids': [victim['id']]})
    with pytest.raises(ConfigurationError, match='referenced'):
        manager.delete(kind, victim['id'], victim['version'])
    assert manager.get(kind, victim['id'])['id'] == victim['id']


@pytest.mark.parametrize('membership', ['object', 'text'])
def test_legacy_bundle_membership_shapes_do_not_allow_referenced_asset_deletion(manager, membership):
    victim = definition(manager, 'asset')
    bundle = manager.save('bundle', {'name': 'Legacy combination', 'host_ids': ['codex'], 'asset_ids': [victim['id']]})
    old = {victim['id']: 'old annotation'} if membership == 'object' else victim['id']
    with manager.connect() as conn:
        conn.execute("UPDATE management_entities SET data=json_set(data,'$.asset_ids',json(?)) WHERE id=?", (json.dumps(old), bundle['id']))
    with pytest.raises(ConfigurationError, match='referenced'):
        manager.delete('asset', victim['id'], victim['version'])


def test_deletion_does_not_decode_the_selected_skills_large_files_either(manager, monkeypatch):
    marker = 'SELECTED_SKILL_FILES_NOT_NEEDED_FOR_DELETION'
    victim = manager.save('asset', {'name': 'Large selected Skill', 'kind': 'skill', 'files': {'SKILL.md': marker + '\n' + 'Synthetic skill text.\n' * 1000}})
    original = json.loads
    def metadata_only(value, *args, **kwargs):
        if isinstance(value, str) and marker in value:
            raise AssertionError('Deletion decoded the selected Skill files')
        return original(value, *args, **kwargs)
    monkeypatch.setattr(management_service.json, 'loads', metadata_only)
    assert manager.delete('asset', victim['id'], victim['version'])['deleted']


@pytest.mark.parametrize('kind', ['provider', 'account', 'model', 'binding', 'asset', 'bundle'])
def test_deleting_selected_definition_never_decodes_its_own_body(manager, monkeypatch, kind):
    marker = f'SELECTED_{kind.upper()}_BODY_MUST_NOT_BE_DECODED'
    victim = definition(manager, kind)
    with manager.connect() as conn:
        conn.execute(
            "UPDATE management_entities SET data=json_set(data,'$.large_note',?) WHERE id=?",
            (marker + ':' + ('x' * 32768), victim['id']),
        )
    original = json.loads
    def metadata_only(value, *args, **kwargs):
        if isinstance(value, str) and marker in value:
            raise AssertionError(f'Deletion decoded the selected {kind} body')
        return original(value, *args, **kwargs)
    monkeypatch.setattr(management_service.json, 'loads', metadata_only)
    result = manager.delete(kind, victim['id'], victim['version'])
    assert result['deleted'] is True
    with manager.connect() as conn:
        assert conn.execute('SELECT 1 FROM management_entities WHERE id=?', (victim['id'],)).fetchone() is None
        if kind == 'asset':
            assert conn.execute('SELECT 1 FROM management_asset_versions WHERE asset_id=?', (victim['id'],)).fetchone() is None


def test_metadata_only_delete_preserves_version_conflict_credential_cleanup_and_oauth_cancel(manager, monkeypatch):
    with pytest.raises(ConfigurationError, match='not found') as missing_exc:
        manager.delete('provider', 'provider_missing', 1)
    assert missing_exc.value.status_code == 404

    provider = definition(manager, 'provider')
    withSecret = manager.credential('provider', provider['id'], 'sk-test-secret-for-delete-123456')
    marker = 'SELECTED_PROVIDER_BODY_WITH_SECRET_NOT_DECODED'
    with manager.connect() as conn:
        conn.execute("UPDATE management_entities SET data=json_set(data,'$.notes',?) WHERE id=?", (marker, provider['id']))
    original = json.loads
    def metadata_only(value, *args, **kwargs):
        if isinstance(value, str) and marker in value:
            raise AssertionError('Deletion decoded the selected provider body')
        return original(value, *args, **kwargs)
    monkeypatch.setattr(management_service.json, 'loads', metadata_only)

    with pytest.raises(ConfigurationError, match='version changed'):
        manager.delete('provider', provider['id'], provider['version'])
    assert len(manager.vault.values) == 1

    deleted_provider = manager.delete('provider', provider['id'], withSecret['version'])
    assert deleted_provider['deleted'] is True
    assert deleted_provider['credential_cleanup'] == [{'id': deleted_provider['credential_cleanup'][0]['id'], 'status': 'completed'}]
    assert manager.vault.values == {}

    oauth_provider = manager.save(
        'provider',
        {
            'name': 'OAuth Provider',
            'protocol': 'openai',
            'base_url': 'https://fixture.example/v1',
            'oauth': {
                'enabled': True,
                'client_id': 'client-1',
                'authorization_url': 'https://fixture.example/oauth/authorize',
                'token_url': 'https://fixture.example/oauth/token',
                'redirect_uri': 'http://127.0.0.1:8420/api/manage/oauth/callback',
            },
        },
    )
    account = manager.save('account', {'name': 'OAuth Account', 'kind': 'oauth', 'provider_id': oauth_provider['id']})
    flow_info = manager.account_action(account['id'], 'authorize')
    assert flow_info['status'] == 'authorizing'
    account_now = manager.get('account', account['id'])
    account_marker = 'SELECTED_ACCOUNT_BODY_NOT_DECODED'
    with manager.connect() as conn:
        conn.execute("UPDATE management_entities SET data=json_set(data,'$.notes',?) WHERE id=?", (account_marker, account['id']))
    def account_metadata_only(value, *args, **kwargs):
        if isinstance(value, str) and account_marker in value:
            raise AssertionError('Deletion decoded the selected account body')
        return original(value, *args, **kwargs)
    monkeypatch.setattr(management_service.json, 'loads', account_metadata_only)
    deleted_account = manager.delete('account', account['id'], account_now['version'])
    assert deleted_account['deleted'] is True
    assert len(deleted_account['credential_cleanup']) == 1
    assert deleted_account['credential_cleanup'][0]['status'] == 'completed'
    with manager.connect() as conn:
        flow_row = conn.execute('SELECT status FROM management_oauth_flows WHERE account_id=?', (account['id'],)).fetchone()
        assert flow_row['status'] == 'cancelled'
    assert manager.vault.values == {}


def test_independent_routing_rejects_saving_nonexistent_providers(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app.database import create_schema
    from app.services import routing_service
    from tests.test_management import FakeVault

    db_path = tmp_path / 'service' / 'test.db'
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CONTEXTCORE_DB', str(db_path))
    create_schema(db_path)
    mgr = management_service.ManagementService(db_path, home, FakeVault())
    valid = definition(mgr, 'provider')

    for bad_patch in (
        {'provider_id': 'provider_does_not_exist'},
        {'provider_id': valid['id'], 'fallback_provider_ids': ['provider_does_not_exist']},
        {'provider_id': valid['id'], 'model_routes': {'gpt-4.1': [valid['id'], 'provider_does_not_exist']}},
    ):
        with pytest.raises(HTTPException) as exc:
            routing_service.configure(bad_patch)
        assert exc.value.status_code in (404, 409, 422)
    current = routing_service.settings()
    assert current['provider_id'] is None
    assert current['fallback_provider_ids'] == []
    assert current['model_routes'] == {}


def test_deleting_provider_referenced_by_independent_routing_is_blocked(tmp_path, monkeypatch):
    from app.database import create_schema
    from app.services import routing_service
    from tests.test_management import FakeVault

    db_path = tmp_path / 'service' / 'test.db'
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CONTEXTCORE_DB', str(db_path))
    create_schema(db_path)
    mgr = management_service.ManagementService(db_path, home, FakeVault())
    primary = mgr.save('provider', {'name': 'Primary Route Provider', 'protocol': 'openai', 'base_url': 'https://primary.example/v1'})
    fallback = mgr.save('provider', {'name': 'Fallback Route Provider', 'protocol': 'openai', 'base_url': 'https://fallback.example/v1'})
    routed = mgr.save('provider', {'name': 'Model Route Provider', 'protocol': 'openai', 'base_url': 'https://routed.example/v1'})

    routing_service.configure({
        'enabled': True,
        'provider_id': primary['id'],
        'fallback_provider_ids': [fallback['id']],
        'model_routes': {'special-model': [routed['id']]},
    })

    for target in (primary, fallback, routed):
        with pytest.raises(ConfigurationError, match='routing|route') as exc:
            mgr.delete('provider', target['id'], target['version'])
        assert exc.value.status_code == 409
        assert mgr.get('provider', target['id'])['id'] == target['id']

    routing_service.configure({
        'enabled': False,
        'provider_id': None,
        'fallback_provider_ids': [],
        'model_routes': {},
    })
    for target in (primary, fallback, routed):
        assert mgr.delete('provider', target['id'], target['version'])['deleted'] is True


def test_concurrent_independent_routing_save_and_provider_delete_never_leaves_dangling_provider(tmp_path, monkeypatch):
    import threading
    from fastapi import HTTPException
    from app.database import create_schema
    from app.services import routing_service
    from tests.test_management import FakeVault

    db_path = tmp_path / 'service' / 'test.db'
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('CONTEXTCORE_DB', str(db_path))
    create_schema(db_path)
    mgr = management_service.ManagementService(db_path, home, FakeVault())

    for iteration in range(6):
        victim = mgr.save('provider', {'name': f'Race Provider {iteration}', 'protocol': 'openai', 'base_url': 'https://race.example/v1'})
        routing_service.configure({'enabled': False, 'provider_id': None, 'fallback_provider_ids': [], 'model_routes': {}})
        barrier = threading.Barrier(2)
        outcomes = {}

        def configure_worker():
            barrier.wait()
            try:
                routing_service.configure({'provider_id': victim['id'], 'fallback_provider_ids': [victim['id']], 'model_routes': {'m': [victim['id']]}})
                outcomes['configure'] = 'ok'
            except HTTPException as exc:
                outcomes['configure'] = f'err:{exc.status_code}'

        def delete_worker():
            barrier.wait()
            try:
                mgr.delete('provider', victim['id'], victim['version'])
                outcomes['delete'] = 'ok'
            except ConfigurationError as exc:
                outcomes['delete'] = f'err:{exc.status_code}'

        t1 = threading.Thread(target=configure_worker)
        t2 = threading.Thread(target=delete_worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        state = routing_service.settings()
        referenced = {
            *([state['provider_id']] if state['provider_id'] else []),
            *state['fallback_provider_ids'],
            *(pid for route in state['model_routes'].values() for pid in route),
        }
        with mgr.connect() as conn:
            exists = conn.execute("SELECT 1 FROM management_entities WHERE kind='provider' AND id=?", (victim['id'],)).fetchone() is not None
        assert not (victim['id'] in referenced and not exists), f'Dangling provider in routing settings on iteration {iteration}: {outcomes}'
        if victim['id'] in referenced:
            routing_service.configure({'enabled': False, 'provider_id': None, 'fallback_provider_ids': [], 'model_routes': {}})
            mgr.delete('provider', victim['id'], victim['version'])


def test_host_state_plan_budget_uses_covering_index_and_skips_out_of_budget_plan_bodies(manager, monkeypatch):
    from app.services import management_experience

    asset_recent = manager.save('asset', {'name': 'Recent Rule', 'kind': 'prompt', 'content': 'Recent rule'})
    asset_old = manager.save('asset', {'name': 'Old Rule', 'kind': 'prompt', 'content': 'Old rule'})
    forbidden_marker = 'OUT_OF_BUDGET_PLAN_METADATA_MUST_NOT_BE_DECODED'
    pad = 'p' * 250_000

    with manager.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        old_payload = {
            'metadata': {
                'host_id': 'codex',
                'operation': 'install',
                'entity_versions': {asset_old['id']: 1},
                'provider_dependencies': {},
                'audit_note': forbidden_marker + ':' + pad,
            },
            'files': [],
        }
        conn.execute(
            "INSERT INTO management_plans VALUES('plan_old',1,'applied',?,?,?)",
            (json.dumps(old_payload, separators=(',', ':')), '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z'),
        )
        for idx in range(1, 20):
            recent_payload = {
                'metadata': {
                    'host_id': 'codex',
                    'operation': 'install',
                    'entity_versions': {asset_recent['id']: 1},
                    'provider_dependencies': {},
                    'audit_note': pad,
                },
                'files': [],
            }
            stamp = f'2026-10-26T00:01:{idx:02d}Z'
            conn.execute(
                "INSERT INTO management_plans VALUES(?,1,'applied',?,?,?)",
                (f'plan_{idx:03d}', json.dumps(recent_payload, separators=(',', ':')), stamp, stamp),
            )
        plan_steps = [
            row['detail']
            for row in conn.execute(
                """EXPLAIN QUERY PLAN WITH recent AS (
                    SELECT id,status,updated_at,
                        length(coalesce(json_extract(payload,'$.metadata'),'')) item_bytes
                    FROM management_plans WHERE json_extract(payload,'$.metadata.host_id')=?
                    AND status IN ('applied','applying','recovery_required') ORDER BY updated_at DESC,id DESC LIMIT 301
                ), budgeted AS (
                    SELECT id,status,updated_at,
                        sum(item_bytes) OVER (ORDER BY updated_at DESC,id DESC) metadata_bytes FROM recent
                ) SELECT b.id,b.status,b.updated_at,b.metadata_bytes,
                    CASE WHEN b.metadata_bytes<=4194304 THEN json_extract(p.payload,'$.metadata') ELSE NULL END metadata
                    FROM budgeted b
                    LEFT JOIN management_plans p ON b.metadata_bytes<=4194304 AND p.id=b.id""",
                ('codex',),
            ).fetchall()
        ]
    if sqlite3.sqlite_version_info >= (3, 50, 0):
        assert any('COVERING INDEX idx_management_plan_host_updated' in step for step in plan_steps), plan_steps
        assert not any('USE TEMP B-TREE' in step for step in plan_steps), plan_steps
    else:
        assert any('INDEX idx_management_plan_host_updated' in step for step in plan_steps), plan_steps
        assert not any('SCAN management_plans' in step for step in plan_steps), plan_steps

    original = json.loads
    def bounded_loads(value, *args, **kwargs):
        if isinstance(value, str) and forbidden_marker in value:
            raise AssertionError('host_state decoded out-of-budget plan metadata')
        return original(value, *args, **kwargs)
    monkeypatch.setattr(management_experience.json, 'loads', bounded_loads)
    state = management_experience.host_state(manager, 'codex')
    assert state['coverage'] == 'partial'
    assert state['resources'][asset_recent['id']]['plan_id'] == 'plan_019'
    assert asset_old['id'] not in state['resources']
