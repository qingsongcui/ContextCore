# -*- mode: python ; coding: utf-8 -*-
#
# ContextCore Backend Standalone Binary Specification (Legacy Desktop Spec)
# NOTE: Canonical release distribution is via wheel/source CLI running as a native
# macOS LaunchAgent background service with the web interface. PyInstaller packaging
# is maintained as an optional build extra (see requirements-build.txt), not runtime.
#
import os
from pathlib import Path

backend_dir = Path(SPECPATH).resolve()

a = Analysis(
    ['desktop_backend.py'],
    pathex=[str(backend_dir)],
    binaries=[],
    datas=[
        (str(backend_dir / 'app' / 'integrations'), 'app/integrations'),
        (str(backend_dir / 'app' / 'services'), 'app/services'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='contextcore-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='contextcore-backend',
)
