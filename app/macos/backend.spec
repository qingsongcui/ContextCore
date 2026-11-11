from PyInstaller.utils.hooks import collect_submodules, copy_metadata
from pathlib import Path
root = Path(SPECPATH).parent
stage = root / 'macos' / 'build'
import json
inputs = json.loads((stage / 'build-inputs.json').read_text())
backend = Path(inputs['backend'])
frontend = Path(inputs['frontend'])
import importlib.util
resource_spec = importlib.util.spec_from_file_location('release_resource_policy', backend / 'app/release_resources.py')
resource_policy = importlib.util.module_from_spec(resource_spec)
resource_spec.loader.exec_module(resource_policy)
a = Analysis([str(root / 'macos' / 'backend_entry.py')], pathex=[str(backend)],
    binaries=[], datas=resource_policy.native_data_files(backend) + copy_metadata('contextcore-backend') + [
        (str(frontend), 'frontend'),
        (str(stage / 'desktop-release.json'), '.')],
    hiddenimports=collect_submodules('uvicorn') + ['.'.join(p.relative_to(backend).with_suffix('').parts) for p in (backend / 'app').rglob('*.py') if p.name != '__init__.py'],
    excludes=['pytest', 'tkinter'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='senrelay-service', console=True)
coll = COLLECT(exe, a.binaries, a.datas, name='senrelay-service')
