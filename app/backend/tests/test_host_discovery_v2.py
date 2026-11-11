from pathlib import Path
from app.database import create_schema, seed_data, db
from app.services.host_registry import discover_hosts, record_observation, get_observations
from app.services import connection_service


def test_metadata_does_not_execute_or_read_secrets(tmp_path, monkeypatch):
    home = tmp_path / 'home'; sessions = home / '.codex/sessions'; sessions.mkdir(parents=True)
    (home / '.codex/auth.json').write_text('SECRET MUST NOT BE OPENED')
    binaries = tmp_path / 'bin'; binaries.mkdir(); command = binaries / 'codex'
    command.write_text('#!/bin/sh\ntouch ' + str(tmp_path / 'EXECUTED')); command.chmod(0o755)
    apps = tmp_path / 'apps'; (apps / 'Codex.app').mkdir(parents=True)
    original = Path.open
    def deny_open(self, *args, **kwargs):
        if '.codex' in str(self): raise AssertionError('metadata discovery attempted content read')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', deny_open)
    found = {item['id']: item for item in discover_hosts(home=home, env={'PATH': str(binaries)}, application_dirs=[apps])}
    codex = found['codex']
    assert not (tmp_path / 'EXECUTED').exists()
    assert codex['paths'] == [str(sessions)]
    assert {s['installation'] for s in codex['discovery']['surfaces']} == {'detected'}
    assert codex['discovery']['credentials_read'] is False
    assert all(s['authentication'] == 'unknown' and s['version'] is None and not s['verified'] for s in codex['discovery']['surfaces'])


def test_leftover_config_is_not_installed_desktop_or_health(tmp_path):
    (tmp_path / '.cursor').mkdir()
    item = next(i for i in discover_hosts(home=tmp_path) if i['id'] == 'cursor')
    assert item['discovered']
    assert item['discovery']['health'] == 'not_probed'
    assert all(s['installation'] == 'not_detected' for s in item['discovery']['surfaces'])
    cli = next(s for s in item['discovery']['surfaces'] if s['id'] == 'cli')
    assert cli['capabilities']['history']['status'] == 'unsupported'


def test_environment_override_and_symlink_are_metadata_only(tmp_path):
    override = tmp_path / 'alternate'; (override / 'sessions').mkdir(parents=True)
    outside = tmp_path / 'external'; outside.mkdir(); home = tmp_path / 'home'; home.mkdir()
    (home / '.claude').symlink_to(outside, target_is_directory=True)
    rows = {r['id']: r for r in discover_hosts(home=home, env={'CODEX_HOME': str(override)})}
    assert rows['codex']['paths'] == [str(override / 'sessions')]
    assert any(e['status'] == 'symlink_requires_authorization' for e in rows['claude-code']['discovery']['evidence'])
    assert rows['claude-code']['paths'] == []


def test_discovery_preserves_auth_and_existing_grants(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'app.db')); monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    create_schema(); seed_data(); home = tmp_path / 'home'
    for path in ['.codex/sessions', '.claude/projects', '.omp/agent/sessions']: (home / path).mkdir(parents=True)
    connection_service.patch_connection('codex', paths=['/custom/keep'], confirmed=True)
    with db() as conn:
        conn.execute("UPDATE connections SET token_hash='preserve-hash', config_preview='preserve-config' WHERE id='codex'")
        conn.execute("UPDATE connections SET enabled=1 WHERE id='omp'")
    rows = {r['id']: r for r in connection_service.discover_connections(home=home, env={})}
    assert rows['codex']['paths'] == ['/custom/keep']
    assert rows['claude-code']['paths'] == [str(home / '.claude/projects')]
    assert rows['omp']['paths'] == []
    with db() as conn:
        assert tuple(conn.execute("SELECT token_hash,config_preview FROM connections WHERE id='codex'").fetchone()) == ('preserve-hash', 'preserve-config')


def test_server_observation_drops_bodies_and_is_not_ui_consumption(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'app.db'))
    record_observation('codex', 'capture', 'unknown', {'event_name': 'Stop', 'prompt': 'SECRET', 'token': 'SECRET'})
    record_observation('codex', 'capture', 'unknown', {'event_name': 'Stop'})
    row = get_observations('codex')[0]
    assert row['count'] == 2 and row['metadata'] == {'event_name': 'Stop'}
    assert row['evidence_kind'] == 'server_observed' and row['user_interface_consumption'] == 'unknown'

def test_symlink_ancestor_does_not_prefill_history(tmp_path):
    home = tmp_path / 'home'; home.mkdir(); outside = tmp_path / 'outside'; (outside / 'projects').mkdir(parents=True)
    (home / '.claude').symlink_to(outside, target_is_directory=True)
    row = next(r for r in discover_hosts(home=home) if r['id'] == 'claude-code')
    assert row['paths'] == []
    assert all(e['status'] == 'symlink_requires_authorization' for e in row['discovery']['evidence'] if e['kind'] == 'history_root')


def test_bundle_version_is_bounded_metadata_and_never_a_cli_or_runtime_proof(tmp_path):
    import plistlib
    apps = tmp_path / 'apps'
    info = apps / 'Codex.app/Contents/Info.plist'
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '1.2.3', 'CFBundleVersion': '42'}))
    row = next(r for r in discover_hosts(home=tmp_path/'home', application_dirs=[apps]) if r['id']=='codex')
    desktop = next(s for s in row['discovery']['surfaces'] if s['id']=='desktop')
    cli = next(s for s in row['discovery']['surfaces'] if s['id']=='cli')
    assert desktop['version']=='1.2.3' and desktop['version_source']=='bundle_metadata'
    assert not desktop['verified'] and desktop['runtime']=='unknown'
    assert cli['version'] is None
    for raw in (b'not a plist', b'<plist><broken', b'x'*65537, plistlib.dumps({'CFBundleShortVersionString':'Bearer synthetic-secret'})):
        info.write_bytes(raw)
        row=next(r for r in discover_hosts(home=tmp_path/'home', application_dirs=[apps]) if r['id']=='codex')
        assert all(s['version'] is None for s in row['discovery']['surfaces'])
    target=tmp_path/'external.plist'
    target.write_bytes(plistlib.dumps({'CFBundleShortVersionString':'9.9.9'}))
    info.unlink(); info.symlink_to(target)
    row=next(r for r in discover_hosts(home=tmp_path/'home', application_dirs=[apps]) if r['id']=='codex')
    assert all(s['version'] is None for s in row['discovery']['surfaces'])


def test_codex_home_does_not_hide_the_desktop_sessions_tree(tmp_path):
    home = tmp_path / 'home'
    (home / '.codex/sessions').mkdir(parents=True)
    override = tmp_path / 'cli-home'
    (override / 'sessions').mkdir(parents=True)
    row = next(item for item in discover_hosts(home=home, env={'CODEX_HOME': str(override)}) if item['id'] == 'codex')
    assert str(home / '.codex/sessions') in row['paths']
    assert str(override / 'sessions') in row['paths']


def test_present_antigravity_ide_and_codex_archive_are_suggested(tmp_path):
    home = tmp_path / 'home'
    for rel in (
        '.gemini/antigravity/brain',
        '.gemini/antigravity-cli/brain',
        '.gemini/antigravity-ide/brain',
        '.codex/sessions',
        '.codex/archived_sessions',
    ):
        (home / rel).mkdir(parents=True)
    rows = {item['id']: item for item in discover_hosts(home=home, env={})}
    assert str(home / '.gemini/antigravity-ide/brain') in rows['antigravity']['paths']
    assert str(home / '.codex/archived_sessions') in rows['codex']['paths']
