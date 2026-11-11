"""Published service startup must validate the bytes it claims to serve."""
import importlib.util
import json
import marshal
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from app import cli, runtime


@pytest.fixture
def release(tmp_path):
    script = Path(__file__).parents[2] / 'scripts' / 'prepare_release.py'
    spec = importlib.util.spec_from_file_location('prepare_v3_release', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend, frontend, data = tmp_path/'backend', tmp_path/'frontend', tmp_path/'data'
    (backend/'app').mkdir(parents=True)
    frontend.mkdir()
    (backend/'app'/'main.py').write_text('original backend')
    (frontend/'index.html').write_text('original frontend')
    record = module.prepare(backend, frontend, data)
    (data/'current-release.json').write_text(json.dumps(record))
    return data, record


def test_verified_serve_accepts_only_matching_backend_ui_and_release_id(release):
    data, record = release
    verified = runtime.verified_serving_release(data, Path(record['backend_dir']),
        frontend_dir=Path(record['frontend_dir']), release_id=record['release_id'])
    assert verified == record
    with pytest.raises(ValueError):
        runtime.verified_serving_release(data, Path(record['backend_dir']), release_id='wrong')
    with pytest.raises(ValueError):
        runtime.verified_serving_release(data, Path(record['backend_dir']), frontend_dir=data/'other')


def test_existing_manifest_never_hides_drifted_or_extra_release_code(release):
    data, record = release
    backend = Path(record['backend_dir'])
    (backend/'app'/'main.py').write_text('modified backend')
    with pytest.raises(ValueError, match='content verification'):
        runtime.verified_serving_release(data, backend)
    (backend/'app'/'main.py').write_text('original backend')
    (backend/'app'/'injected.py').write_text('extra module')
    with pytest.raises(ValueError, match='file set'):
        runtime.verified_serving_release(data, backend)


def test_cli_rejects_corrupt_pinned_release_before_serving_or_creating_database(release, monkeypatch, capsys):
    data, record = release
    backend = Path(record['backend_dir'])
    (backend/'app'/'main.py').write_text('drift after deployment')
    monkeypatch.setattr(cli, '__file__', str(backend/'app'/'cli.py'))
    args = cli.create_parser().parse_args(['serve', '--data-dir', str(data), '--no-static'])
    assert cli.cmd_serve(args) == 1
    assert 'verification failed' in capsys.readouterr().err.lower()
    assert not (data/'contextcore.db').exists()


def test_development_service_without_a_release_claim_remains_supported(tmp_path):
    assert runtime.verified_serving_release(tmp_path/'data', tmp_path/'development') is None


def test_unlisted_directory_symlinks_cannot_inject_release_modules(release, tmp_path):
    data, record = release
    outside = tmp_path/'unmanifested'
    outside.mkdir()
    (outside/'injected.py').write_text('value = "not part of this release"')
    backend = Path(record['backend_dir'])
    (backend/'app'/'unmanifested').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symbolic link'):
        runtime.verified_serving_release(data, backend)


def test_unlisted_top_level_modules_are_not_part_of_a_verified_release(release):
    data, record = release
    backend = Path(record['backend_dir'])
    (backend/'late_injection.py').write_text('value = "not part of this release"')
    with pytest.raises(ValueError, match='file set'):
        runtime.verified_serving_release(data, backend)


def test_trusted_launcher_ignores_forged_timestamp_valid_release_bytecode(tmp_path):
    script = Path(__file__).parents[2] / 'scripts' / 'prepare_release.py'
    spec = importlib.util.spec_from_file_location('prepare_bootstrap_release', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend, frontend, data = tmp_path/'backend', tmp_path/'frontend', tmp_path/'data'
    (backend/'app').mkdir(parents=True)
    frontend.mkdir()
    (backend/'app'/'__init__.py').write_text('')
    (backend/'app'/'main.py').write_text('')
    (backend/'app'/'cli.py').write_text('def main(argv):\n    print("verified-source-cli")\n    return 0\n')
    (frontend/'index.html').write_text('frontend')
    record = module.prepare(backend, frontend, data)
    (data/'current-release.json').write_text(json.dumps(record))
    pinned = Path(record['backend_dir'])/'app'/'cli.py'
    cache = Path(py_compile.compile(str(pinned), doraise=True))
    sentinel = tmp_path/'unverified-bytecode-executed'
    injected = compile(f'from pathlib import Path\nPath({str(sentinel)!r}).write_text("bad")\ndef main(argv): return 0\n', str(pinned), 'exec')
    cache.write_bytes(cache.read_bytes()[:16] + marshal.dumps(injected))
    result = subprocess.run([sys.executable, '-I', '-B', record['launcher_path'],
        '--manifest', str(pinned.parents[1].parent/'manifest.json'), '--', '-m', 'app.cli', 'serve',
        '--data-dir', str(data), '--frontend-dir', record['frontend_dir']], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'verified-source-cli' in result.stdout
    assert not sentinel.exists()
    alias = tmp_path/'data-alias'
    alias.symlink_to(data, target_is_directory=True)
    alias_command = [sys.executable, '-I', '-B', record['launcher_path'],
        '--manifest', str(pinned.parents[1].parent/'manifest.json'), '--', '-m', 'app.cli', 'serve',
        '--data-dir', str(alias), '--frontend-dir', record['frontend_dir']]
    result = subprocess.run(alias_command, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([*alias_command, '--data-dir='+str(tmp_path/'other')], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert not sentinel.exists()
    pinned.write_text('raise RuntimeError("unverified source")')
    result = subprocess.run([sys.executable, '-I', '-B', record['launcher_path'],
        '--manifest', str(pinned.parents[1].parent/'manifest.json'), '--', '-m', 'app.cli', 'serve',
        '--data-dir', str(data), '--frontend-dir', record['frontend_dir']], capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert 'content verification failed' in result.stderr
    assert not sentinel.exists()
