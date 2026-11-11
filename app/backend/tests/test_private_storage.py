import os
from pathlib import Path

import pytest

from app.database import db


def mode(path):
    return path.stat().st_mode & 0o777


def test_database_migrates_only_product_leaf_and_sidecars(tmp_path, monkeypatch):
    shared = tmp_path / 'shared'
    shared.mkdir(mode=0o755)
    data = shared / 'product'
    data.mkdir(mode=0o755)
    target = data / 'test.db'
    target.touch(mode=0o644)
    monkeypatch.setenv('CONTEXTCORE_DATA_DIR', str(data))
    monkeypatch.setenv('CONTEXTCORE_DB', str(target))
    with db() as conn:
        conn.execute('CREATE TABLE sample (value TEXT)')
        conn.execute("INSERT INTO sample VALUES ('private')")
        assert mode(target) == 0o600
        for suffix in ('-wal', '-shm'):
            assert mode(Path(str(target) + suffix)) == 0o600
    assert mode(data) == 0o700
    assert mode(shared) == 0o755
    with db() as conn:
        assert conn.execute('SELECT value FROM sample').fetchone()[0] == 'private'


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_database_rejects_link_without_changing_target(tmp_path, suffix, monkeypatch):
    target = tmp_path / 'outside'
    target.write_text('keep')
    target.chmod(0o644)
    database = tmp_path / 'test.db'
    Path(str(database) + suffix).symlink_to(target)
    monkeypatch.setenv('CONTEXTCORE_DB', str(database))
    with pytest.raises(OSError):
        with db():
            pass
    assert target.read_text() == 'keep' and mode(target) == 0o644


def test_explicit_db_does_not_chmod_shared_parent(tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir(mode=0o755)
    with db(shared / 'standalone.db') as conn:
        conn.execute('CREATE TABLE sample (value TEXT)')
    assert mode(shared) == 0o755
    assert mode(shared / 'standalone.db') == 0o600


def test_database_rejects_hardlink_and_directory_link(tmp_path, monkeypatch):
    original = tmp_path / 'original'
    original.touch(mode=0o644)
    alias = tmp_path / 'test.db'
    os.link(original, alias)
    with pytest.raises(OSError):
        with db(alias):
            pass
    assert mode(original) == 0o644
    linked = tmp_path / 'linked'
    linked.symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setenv('CONTEXTCORE_DB', str(linked / 'another.db'))
    with pytest.raises(OSError):
        with db():
            pass
    assert not (tmp_path / 'another.db').exists()


def test_nested_link_rejected_before_creating_leaf(tmp_path):
    linked = tmp_path / 'linked'
    linked.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        with db(linked / 'product' / 'new.db'):
            pass
    assert not (tmp_path / 'product').exists()


def test_foreign_owned_file_rejected_without_permission_change(tmp_path, monkeypatch):
    from app.private_storage import private_file
    target = tmp_path / 'data.db'
    target.touch(mode=0o644)
    uid = os.getuid()
    monkeypatch.setattr(os, 'getuid', lambda: uid + 1)
    with pytest.raises(OSError):
        private_file(target)
    assert mode(target) == 0o644


def test_foreign_owned_directory_rejected_without_permission_change(tmp_path, monkeypatch):
    from app.private_storage import private_directory
    target = tmp_path / 'foreign-product'
    target.mkdir(mode=0o755)
    uid = os.getuid()
    monkeypatch.setattr(os, 'getuid', lambda: uid + 1)
    with pytest.raises(OSError):
        private_directory(target)
    assert mode(target) == 0o755


def test_fifo_is_rejected_without_waiting_for_a_writer(tmp_path):
    import subprocess
    import sys
    target = tmp_path / 'not-a-database'
    os.mkfifo(target, 0o600)
    result = subprocess.run(
        [sys.executable, '-c',
         'from pathlib import Path; from app.private_storage import private_file; '
         'import sys; private_file(Path(sys.argv[1]))', str(target)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert 'owned regular file' in result.stderr


@pytest.mark.skipif(__import__('sys').platform != 'darwin', reason='macOS extended ACL behavior')
def test_private_storage_removes_extended_grants_without_changing_parent(tmp_path):
    import subprocess
    from app.private_storage import prepare_database
    parent = tmp_path / 'shared'
    parent.mkdir(mode=0o755)
    data = parent / 'product'
    data.mkdir(mode=0o755)
    target = data / 'test.db'
    target.write_bytes(b'synthetic preserved bytes')
    sidecars = [Path(str(target) + suffix) for suffix in ('-wal', '-shm', '-journal')]
    for p in sidecars:
        p.write_bytes(b'synthetic sidecar')
    def acl(path):
        return [line.strip() for line in subprocess.check_output(['ls', '-lde', str(path)], text=True).splitlines()[1:]]
    subprocess.run(['chmod', '+a', 'everyone allow read,search', str(parent)], check=True)
    subprocess.run(['chmod', '+a', 'everyone allow read,search', str(data)], check=True)
    for p in [target, *sidecars]:
        subprocess.run(['chmod', '+a', 'everyone allow read', str(p)], check=True)
        p.chmod(0o600)
        assert any('everyone allow read' in row for row in acl(p))
    parent_acl = acl(parent)
    prepare_database(target, data_directory=data)
    assert acl(data) == [] and mode(data) == 0o700
    for p in [target, *sidecars]:
        assert acl(p) == [] and mode(p) == 0o600
    assert target.read_bytes() == b'synthetic preserved bytes'
    assert acl(parent) == parent_acl and mode(parent) == 0o755
