# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
PROJECT_ROOT = str(Path(SPECPATH).parent)

hiddenimports = [
    "django",
    "rest_framework",
    "django_filters",
    "drf_yasg",
    "waitress",
    "whitenoise",
    "MySQLdb",
    "win32timezone",
]
hiddenimports += collect_submodules("core", filter=lambda name: ".tests" not in name)
hiddenimports += collect_submodules("integracao", filter=lambda name: ".tests" not in name)
hiddenimports += collect_submodules("sysvarhub")

a = Analysis(
    ["windows_service.py"],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SysvarHubService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SysvarHubService",
)
