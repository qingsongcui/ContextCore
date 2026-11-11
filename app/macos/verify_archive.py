"""Read back the exact ZIP, runtime identity, licenses and bounded secret scan."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile


def verify(directory: Path):
    record = json.loads((directory/'BUILD.json').read_text())
    archive = directory/'Senrelay-macOS.zip'
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == record['sha256']
    spec = importlib.util.spec_from_file_location('security', Path(__file__).resolve().parents[2]/'scripts/verify_repository_security.py')
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    count = 0
    with zipfile.ZipFile(archive) as package:
        assert package.testzip() is None, 'Archive CRC failure'
        names = package.namelist()
        assert len(names) == len(set(names)), 'Duplicate archive member'
        for info in package.infolist():
            name = PurePosixPath(info.filename)
            assert not name.is_absolute() and '..' not in name.parts, 'Unsafe archive member'
            assert name.parts[0] in {'Senrelay.app', '__MACOSX'}, 'Unexpected archive root'
            assert not any(part in {'.git', '.venv', 'node_modules', 'credentials', 'uploads'} for part in name.parts), 'Private/runtime directory in archive'
            assert name.suffix not in {'.db', '.sqlite', '.sqlite3', '.token', '.pid', '.p12', '.p8'}, 'Private/runtime file in archive'
            assert name.name not in {'auth.json', 'credentials.json', 'current-release.json', 'SOURCE-COMMIT'}, 'Private state in archive'
            if info.is_dir():
                continue
            count += 1
            # ZIP payloads are scanned after decompression. Nested PYZ/stdlib
            # bytecode is covered by source scanning, not claimed decoded here.
            with package.open(info) as stream:
                tail = b''
                while chunk := stream.read(1024 * 1024):
                    assert not scanner.content_findings(tail + chunk, info.filename), 'Potential credential in ' + scanner.safe_relative_location(info.filename) + '; content withheld'
                    tail = chunk[-512:]
    with tempfile.TemporaryDirectory(prefix='senrelay-archive-check-') as temporary:
        subprocess.run(['ditto', '-x', '-k', str(archive.resolve()), temporary], check=True, capture_output=True)
        bundle = Path(temporary)/'Senrelay.app'
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(bundle)], check=True, capture_output=True)
        info = plistlib.loads((bundle/'Contents/Info.plist').read_bytes())
        assert info['CFBundleVersion'] == record['build_number']
        assert info['CFBundleShortVersionString'] == record['native_version']
        assert info['SenrelayReleaseVersion'] == record['version']
        assert info['LSMinimumSystemVersion'] == record['minimum_macos']
        resources = bundle/'Contents/Resources'
        release = json.loads((resources/'desktop-release.json').read_text())
        assert all(release[key] == record[key] for key in release)
        for path in [bundle/'Contents/MacOS/Senrelay', resources/'service/senrelay-service']:
            assert subprocess.check_output(['lipo', '-archs', str(path)], text=True).strip() == record['architecture']
        vtool = shutil.which('vtool')
        if vtool:
            build_info = subprocess.check_output([vtool, '-show-build', str(bundle/'Contents/MacOS/Senrelay')], text=True)
            minos_values = re.findall(r'^\s*minos\s+([0-9]+(?:\.[0-9]+)*)\s*$', build_info, flags=re.MULTILINE)
            assert minos_values, 'Missing LC_BUILD_VERSION minos on Senrelay binary'
            def _version_tuple(raw: str) -> tuple[int, ...]:
                parts = [int(p) for p in raw.split('.')]
                while len(parts) < 3:
                    parts.append(0)
                return tuple(parts)
            for minos in minos_values:
                assert _version_tuple(minos) <= (14, 0, 0), f'Senrelay binary minos {minos} exceeds 14.0'
        assert (resources/'LICENSE').is_file()
        notices = list(resources.rglob('*LICENSE*')) + list(resources.rglob('*THIRD_PARTY*'))
        assert len(notices) > 2, 'Third-party notices missing'
        original = directory/'Senrelay.app'
        for path in original.rglob('*'):
            restored = bundle/path.relative_to(original)
            if path.is_symlink():
                assert restored.is_symlink() and path.readlink() == restored.readlink()
            elif path.is_file():
                assert restored.is_file() and hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(restored.read_bytes()).digest()
    return {'archive_files': count, 'sha256': record['sha256'], 'signature': 'verified ad-hoc',
            'architecture': record['architecture'], 'identity_and_readback': 'verified',
            'privacy_scope': 'ZIP members and decompressed bytes; nested bytecode not independently decoded; source scan remains required'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory), indent=2))
