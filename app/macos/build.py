#!/usr/bin/env python3
"""Build a self-contained macOS application for the current machine architecture."""
import argparse
import os
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import re
import tomllib
import importlib.util
import importlib.metadata

here = Path(__file__).resolve().parent
app = here.parent
repo = app.parent
# Target macOS 14.0+ (Sonoma and later) on Apple Silicon (arm64).
minimum_macos = '14.0'
python = Path(sys.executable)
output = here / 'dist'
build = here / 'build'
build.mkdir(exist_ok=True)
output.mkdir(exist_ok=True)

def run(*args, env=None):
    subprocess.run([str(a) for a in args], cwd=repo, env=env, check=True)

sys.path.insert(0, str(app / 'scripts'))
from release_versions import normalize_ci_build_number, reject_audit_build_number, validate_versions

parser = argparse.ArgumentParser()
mode = parser.add_mutually_exclusive_group()
mode.add_argument('--release-manifest', type=Path)
mode.add_argument('--from-source', action='store_true', help='Build a fresh matched candidate from this checkout; never activate the daily service')
parser.add_argument('--output-dir', type=Path, help='Candidate output directory (defaults to app/macos/dist)')
parser.add_argument('--build-number', default=normalize_ci_build_number(os.environ.get('GITHUB_RUN_NUMBER', '1')))
variants = parser.add_mutually_exclusive_group()
variants.add_argument('--local-workbench-test', action='store_true', help='Package a side-by-side test workspace with independent storage and port')
variants.add_argument('--workspace-design-test', action='store_true', help='Package the redesigned workspace in its own local sandbox')
args = parser.parse_args()
if (args.local_workbench_test or args.workspace_design_test) and not (args.from_source and args.output_dir):
    parser.error('Side-by-side test packages require --from-source and --output-dir')
if args.output_dir:
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
if __import__('platform').machine() != 'arm64':
    raise SystemExit('This Beta packages Apple Silicon (arm64) only')
if not re.fullmatch(r'[0-9]{1,7}(?:\.[0-9]{1,2}){0,2}', args.build_number):
    raise SystemExit('Build number must contain up to three numeric components')
try:
    reject_audit_build_number(args.build_number)
except ValueError as exc:
    raise SystemExit(str(exc)) from exc
version = json.loads((app / 'frontend/package.json').read_text())['version']
version_identity = validate_versions(frontend=version, backend=tomllib.loads((app / 'backend/pyproject.toml').read_text())['project']['version'])
if importlib.metadata.version('contextcore-backend') != version_identity['python_version']:
    raise SystemExit('Installed backend metadata is stale; build with the locked environment for this source version')
selection = json.loads((here / 'selected-release.json').read_text())
# Only verified immutable snapshots are package inputs, including a prepared UI repair.
if args.from_source:
    # This explicit build mode makes clean CI possible without a developer's
    # installed service or private current-release manifest. The approved shell
    # selection remains provenance; this artifact is a candidate, not deployment.
    run('npm', '--prefix', app / 'frontend', 'run', 'build')
    spec = importlib.util.spec_from_file_location('senrelay_prepare', app / 'scripts/prepare_release.py')
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    requested = prepare.prepare(app / 'backend', app / 'frontend/dist-staging', build / 'source-candidate')
else:
    manifest_path = args.release_manifest or Path.home() / 'Library/Application Support/ContextCore/current-release.json'
    requested = json.loads(manifest_path.read_text())
data_dir = Path(requested['backend_dir']).parent.parent.parent
record = json.loads(subprocess.check_output([str(python), '-c',
    'import json,sys; from pathlib import Path; from app.runtime import _verified_release_for_autostart; r=_verified_release_for_autostart(Path(sys.argv[1]),Path(sys.argv[2])); assert r; print(json.dumps(r))',
    str(data_dir), requested['backend_dir']],
    cwd=repo, env=os.environ | {'PYTHONPATH': str(app / 'backend'), 'PYTHONDONTWRITEBYTECODE': '1'}, text=True))
if not args.from_source:
    for key in ('release_id', 'frontend_sha256', 'backend_sha256', 'api_contract'):
        if record.get(key) != selection[key]:
            raise SystemExit(f'Build refused: snapshot does not match the approved UI repair ({key})')
os.environ['PYINSTALLER_CONFIG_DIR'] = str(build / 'pyinstaller-cache')
os.environ['MACOSX_DEPLOYMENT_TARGET'] = minimum_macos
backend = Path(record['backend_dir'])
frontend = Path(record['frontend_dir'])
manifest = {key: record[key] for key in ('release_id', 'api_contract', 'frontend_sha256', 'backend_sha256')}
(build / 'desktop-release.json').write_text(json.dumps(manifest, indent=2))
(build / 'build-inputs.json').write_text(json.dumps({'backend': str(backend), 'frontend': str(frontend)}))
run(python, '-m', 'PyInstaller', '--noconfirm', '--clean', '--distpath', build / 'frozen', '--workpath', build / 'pyinstaller', here / 'backend.spec', env=os.environ | {'MACOSX_DEPLOYMENT_TARGET': minimum_macos})
import subprocess
frozen_service = build / 'frozen/senrelay-service'
runtime_sha = subprocess.check_output(
    [str(frozen_service / 'senrelay-service'), '-m', 'app.cli', 'print-backend-runtime-sha256'],
    text=True,
).strip()
if len(runtime_sha) != 64:
    raise RuntimeError(f'Unexpected backend_runtime_sha256 output: {runtime_sha!r}')
manifest['backend_runtime_sha256'] = runtime_sha
manifest_json = json.dumps(manifest, indent=2)
(build / 'desktop-release.json').write_text(manifest_json)
for target_manifest in (frozen_service / '_internal/desktop-release.json', frozen_service / 'desktop-release.json'):
    if target_manifest.parent.is_dir() and (target_manifest.exists() or target_manifest.parent.name == '_internal'):
        target_manifest.write_text(manifest_json)
bundle = output / 'Senrelay.app'
if bundle.exists(): shutil.rmtree(bundle)
contents = bundle / 'Contents'
resources = contents / 'Resources'
(contents / 'MacOS').mkdir(parents=True)
resources.mkdir()
shutil.copytree(build / 'frozen/senrelay-service', resources / 'service')
run('xcrun', 'swiftc', '-O', '-module-cache-path', build / 'swift-cache', '-target', f'{__import__("platform").machine()}-apple-macosx{minimum_macos}', '-framework', 'Cocoa', '-framework', 'WebKit', here / 'Senrelay.swift', here / 'WorkbenchShortcuts.swift', here / 'VerifyWorkspaceExports.swift', '-o', contents / 'MacOS/Senrelay')
icons = build / 'AppIcon.iconset'; icons.mkdir(exist_ok=True)
icon_source = frontend / 'app-icon.png'
if not icon_source.exists(): icon_source = frontend / 'senrelay-logo-original-v1.png'
for size in [16,32,128,256,512]:
    for scale in [1,2]:
        name = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
        run('sips', '-z', size*scale, size*scale, icon_source, '--out', icons / name)
run('iconutil', '-c', 'icns', icons, '-o', resources / 'AppIcon.icns')
info = {'CFBundleName': 'Senrelay', 'CFBundleDisplayName': 'Senrelay · 续境', 'CFBundleIdentifier': 'com.senrelay.desktop',
    'CFBundleExecutable': 'Senrelay', 'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': version_identity['native_version'],
    'CFBundleVersion': args.build_number, 'CFBundleIconFile': 'AppIcon', 'LSMinimumSystemVersion': minimum_macos,
    'SenrelayReleaseVersion': version,
    'NSHighResolutionCapable': True, 'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
    'LSApplicationCategoryType': 'public.app-category.productivity'}
if args.local_workbench_test:
    info.update(CFBundleIdentifier='com.senrelay.local-workbench-test', CFBundleDisplayName='Senrelay · 本地测试',
                SenrelayLocalWorkbenchTest=True, SenrelayTestPort=19426, SenrelayTestStorage='SenrelayLocalTest')
elif args.workspace_design_test:
    info.update(CFBundleIdentifier='com.senrelay.workspace-design-test', CFBundleDisplayName='Senrelay · Agent 工作台测试',
                SenrelayLocalWorkbenchTest=True, SenrelayTestPort=19436, SenrelayTestStorage='SenrelayWorkspaceDesignTest')
if args.workspace_design_test:
    launcher = here / 'open-host-workspace.command'
    shutil.copy2(launcher, resources / '使用本机Agent配置.command')
    shutil.copy2(launcher, output / '使用本机Agent配置.command')
with (contents / 'Info.plist').open('wb') as stream: plistlib.dump(info, stream)
shutil.copy(backend / 'THIRD_PARTY_NOTICES.md', resources)
shutil.copy(backend / 'LICENSE', resources)
shutil.copy(build / 'desktop-release.json', resources)
if args.from_source:
    (resources / 'selected-release.json').write_text(json.dumps({
        'release_id': manifest['release_id'],
        'frontend_sha256': manifest['frontend_sha256'],
        'backend_sha256': manifest['backend_sha256'],
        'api_contract': manifest['api_contract'],
    }, indent=2) + '\n')
else:
    shutil.copy(here / 'selected-release.json', resources)
subprocess.run(['xattr', '-cr', str(bundle)], cwd=repo, check=False)
run('codesign', '--force', '--deep', '--sign', '-', bundle)
subprocess.run(['xattr', '-cr', str(bundle)], cwd=repo, check=False)
run('codesign', '--verify', '--deep', '--strict', bundle)
run('ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', bundle, output / 'Senrelay-macOS.zip')
receipt = {**manifest, **version_identity, 'build_number': args.build_number,
    'minimum_macos': minimum_macos,
    'architecture': __import__('platform').machine(), 'build_mode': 'source_candidate' if args.from_source else 'selected_installed_release',
    'parent_selection': selection['release_id'], 'signature': 'ad-hoc', 'notarized': False,
    'sha256': hashlib.sha256((output / 'Senrelay-macOS.zip').read_bytes()).hexdigest()}
(output / 'BUILD.json').write_text(json.dumps(receipt, indent=2) + '\n')
(output / 'SHA256SUMS').write_text(receipt['sha256'] + '  Senrelay-macOS.zip\n')
print(bundle)
