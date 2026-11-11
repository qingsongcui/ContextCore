import importlib.util
import json
from pathlib import Path

import pytest


def test_matched_backend_and_frontend_survive_development_changes(tmp_path):
    script = Path(__file__).parents[2] / "scripts" / "prepare_release.py"
    spec = importlib.util.spec_from_file_location("prepare_release", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend, frontend = tmp_path / "backend", tmp_path / "frontend"
    (backend / "app").mkdir(parents=True)
    frontend.mkdir()
    (backend / "app" / "main.py").write_text("first backend")
    (frontend / "index.html").write_text("first frontend")
    first = module.prepare(backend, frontend, tmp_path / "data")
    (backend / "app" / "main.py").write_text("next backend")
    (frontend / "index.html").write_text("next frontend")
    assert (Path(first["backend_dir"]) / "app" / "main.py").read_text() == "first backend"
    assert (Path(first["frontend_dir"]) / "index.html").read_text() == "first frontend"
    assert module.prepare(backend, frontend, tmp_path / "data")["release_id"] != first["release_id"]


def test_reusing_a_release_rejects_unlisted_modules_and_modified_manifest(tmp_path):
    script = Path(__file__).parents[2] / 'scripts' / 'prepare_release.py'
    spec = importlib.util.spec_from_file_location('prepare_release_reuse', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend, frontend, data = tmp_path/'backend', tmp_path/'frontend', tmp_path/'data'
    (backend/'app').mkdir(parents=True)
    frontend.mkdir()
    (backend/'app'/'main.py').write_text('backend')
    (frontend/'index.html').write_text('frontend')
    record = module.prepare(backend, frontend, data)
    extra = Path(record['backend_dir'])/'app'/'unlisted.py'
    extra.write_text('unlisted module')
    with pytest.raises(ValueError, match='file set'):
        module.prepare(backend, frontend, data)
    extra.unlink()
    manifest = Path(record['backend_dir']).parent/'manifest.json'
    manifest.write_text(json.dumps({**record, 'release_id': 'wrong'}))
    with pytest.raises(ValueError, match='manifest verification'):
        module.prepare(backend, frontend, data)


def test_release_excludes_private_neighbors_but_preserves_all_declared_resources(tmp_path):
    import hashlib
    import shutil
    from app.release_resources import RESOURCE_FILES, native_data_files
    script = Path(__file__).parents[2] / 'scripts/prepare_release.py'
    spec = importlib.util.spec_from_file_location('prepare_private_neighbors', script)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    backend, frontend, data = tmp_path/'backend', tmp_path/'frontend', tmp_path/'data'
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source/'app', backend/'app', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    frontend.mkdir(); (frontend/'index.html').write_text('<html>synthetic</html>')
    canary = b'SYNTHETIC_RELEASE_PRIVATE_CANARY'
    private = ['app/.env', 'app/private.db', 'app/private.db-wal', 'app/private.log',
               'app/private-account.json', 'app/private-history.txt', 'app/private-notes.md',
               'app/services/private-account.json', 'app/services/private-history.txt',
               'app/integrations/private-account.json', 'app/integrations/skills/private-history.md',
               'app/configuration-snapshots/secret.py', 'requirements-private.txt']
    for relative in private:
        path = backend/relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(canary)
    result = module.prepare(backend, frontend, data)
    prepared = Path(result['backend_dir'])
    assert not set(private) & result['files'].keys()
    assert all(canary not in path.read_bytes() for path in prepared.rglob('*') if path.is_file())
    for relative in RESOURCE_FILES:
        assert (prepared/relative).read_bytes() == (source/relative).read_bytes()
        assert result['files'][relative] == hashlib.sha256((source/relative).read_bytes()).hexdigest()
    native_paths = {str(Path(path).relative_to(prepared)) for path, _ in native_data_files(prepared)}
    assert set(RESOURCE_FILES) <= native_paths
    assert not set(private) & native_paths
    # Private neighbor edits do not create a new release or perturb source identity.
    for relative in private: (backend/relative).write_bytes(b'SYNTHETIC_CHANGED_SECRET')
    assert module.prepare(backend, frontend, data)['release_id'] == result['release_id']


def test_native_packaging_rejects_missing_or_linked_required_resources(tmp_path):
    from app.release_resources import RESOURCE_FILES, RAW_CODE_RESOURCES, native_data_files
    for relative in (*RESOURCE_FILES, *RAW_CODE_RESOURCES):
        path=tmp_path/relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('synthetic')
    first=tmp_path/RESOURCE_FILES[0]; first.unlink()
    with pytest.raises(ValueError, match='missing or unsafe'):
        native_data_files(tmp_path)
    target=tmp_path/'outside.txt'; target.write_text('private')
    first.symlink_to(target)
    with pytest.raises(ValueError, match='missing or unsafe'):
        native_data_files(tmp_path)


@pytest.mark.parametrize('relative', ['private.py', 'semantic-runtime-wheels.json'])
def test_preparer_refuses_selected_inputs_through_linked_package_directory(tmp_path, relative):
    script=Path(__file__).parents[2]/'scripts/prepare_release.py'
    spec=importlib.util.spec_from_file_location('prepare_parent_link', script)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    backend,frontend=tmp_path/'backend',tmp_path/'frontend'
    (backend/'app').mkdir(parents=True); (backend/'app/main.py').write_text('# source')
    frontend.mkdir(); (frontend/'index.html').write_text('<html>synthetic</html>')
    outside=tmp_path/'outside'; outside.mkdir(); (outside/relative).write_text('SYNTHETIC_PRIVATE')
    (backend/'app/services').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symbolic links'):
        module.prepare(backend,frontend,tmp_path/'data')
    assert not (tmp_path/'data/releases').exists()
