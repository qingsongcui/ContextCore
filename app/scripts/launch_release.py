"""Trusted source-only launcher. Run with Python -I -B, outside the release tree."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def checked_path(raw, expected: Path, base: Path, *, directory: bool) -> Path:
    if not isinstance(raw, str) or not Path(raw).is_absolute() or Path(raw) != expected:
        raise ValueError('Release path does not match the selected data directory')
    node = base
    for part in expected.relative_to(base).parts:
        node /= part
        if node.is_symlink():
            raise ValueError('Release paths must not contain symbolic links')
    if not (expected.is_dir() if directory else expected.is_file()):
        raise ValueError('Release path is unavailable')
    return expected


def verified_record(manifest: Path, *, require_active: bool = True,
                    require_launcher_identity: bool = True) -> dict:
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Invalid published manifest')
    record = json.loads(manifest.read_text())
    if record.get('api_contract') != 2:
        raise ValueError('Unsupported published API contract')
    backend_hash, frontend_hash = record['backend_sha256'], record['frontend_sha256']
    if not all(isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value)
               for value in (backend_hash, frontend_hash)):
        raise ValueError('Invalid release hashes')
    release_id = f'{backend_hash[:16]}-{frontend_hash[:16]}'
    data = manifest.parent.parent.parent
    if record.get('release_id') != release_id or manifest != data/'releases'/release_id/'manifest.json':
        raise ValueError('Published release identity mismatch')
    checked_path(str(manifest), data/'releases'/release_id/'manifest.json', data, directory=False)
    active = data/'current-release.json'
    if require_active and (active.exists() or active.is_symlink()):
        if active.is_symlink() or json.loads(active.read_text()) != record:
            raise ValueError('Active record differs from the selected release')
    backend = checked_path(record['backend_dir'], manifest.parent/'backend', data, directory=True)
    frontend = checked_path(record['frontend_dir'], data/'frontend-releases'/frontend_hash, data, directory=True)
    files = record['files']
    if not isinstance(files, dict) or 'app/main.py' not in files:
        raise ValueError('Incomplete published file manifest')
    if hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest() != backend_hash:
        raise ValueError('Published backend manifest hash mismatch')
    actual = set()
    for path in backend.rglob('*'):
        if path.is_symlink():
            raise ValueError('Published backend contains a symbolic link')
        if path.is_file() and '__pycache__' not in path.parts:
            actual.add(str(path.relative_to(backend)))
    if actual != set(files):
        raise ValueError('Published backend file set mismatch')
    for name, expected in files.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or not isinstance(expected, str):
            raise ValueError('Invalid published file entry')
        if digest_file(backend/path) != expected:
            raise ValueError('Published backend content verification failed')
    ui_digest = hashlib.sha256()
    for path in sorted(frontend.rglob('*')):
        if path.is_symlink():
            raise ValueError('Published frontend contains a symbolic link')
        if path.is_file():
            ui_digest.update(str(path.relative_to(frontend)).encode())
            ui_digest.update(b'\0')
            ui_digest.update(bytes.fromhex(digest_file(path)))
    if not (frontend/'index.html').is_file() or ui_digest.hexdigest() != frontend_hash:
        raise ValueError('Published frontend content verification failed')
    launcher_hash = files.get('release_bootstrap.py')
    launcher = data/'launchers'/str(launcher_hash)/'launch_release.py'
    checked_path(record.get('launcher_path'), launcher, data, directory=False)
    if record.get('launcher_sha256') != launcher_hash or digest_file(launcher) != launcher_hash:
        raise ValueError('Published launcher verification failed')
    if require_launcher_identity and Path(__file__).absolute() != launcher:
        raise ValueError('Launcher must run from its verified control directory')
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    args, remainder = parser.parse_known_args(argv)
    if remainder[:1] == ['--']:
        remainder = remainder[1:]
    if remainder[:3] != ['-m', 'app.cli', 'serve']:
        parser.error('Expected explicit -m app.cli serve arguments')
    try:
        record = verified_record(args.manifest.absolute())
        command = remainder[2:]
        # Ensure the selected data/UI cannot be overridden after verification.
        for key, expected in (('--data-dir', str(args.manifest.parent.parent.parent)),
                              ('--frontend-dir', record['frontend_dir'])):
            values = []
            for index, value in enumerate(command):
                if value == key:
                    values.append(command[index+1] if index+1 < len(command) else None)
                elif value.startswith(key+'='):
                    values.append(value.split('=', 1)[1])
            if len(values) != 1 or not values[0] or Path(values[0]).expanduser().resolve() != Path(expected).resolve():
                raise ValueError('Serve arguments differ from the verified release')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Error: Published service verification failed: {exc}', file=sys.stderr)
        return 1
    # An empty, private prefix prevents CPython reading old or forged .pyc from
    # the release. -B also prevents writing fresh caches while the server runs.
    with tempfile.TemporaryDirectory(prefix='senrelay-source-import-') as prefix:
        sys.pycache_prefix = prefix
        sys.dont_write_bytecode = True
        sys.path.insert(0, record['backend_dir'])
        os.environ['CONTEXTCORE_RELEASE_ID'] = record['release_id']
        from app import cli
        return cli.main(command)


if __name__ == '__main__':
    raise SystemExit(main())
