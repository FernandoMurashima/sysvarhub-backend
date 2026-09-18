# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from pathlib import Path
import sys

block_cipher = None
spec_file = Path(globals().get("__file__", Path(SPECPATH) / "SysvarHubService.spec")).resolve()
spec_root = spec_file.parent
backend_root = spec_root.parent
for candidate in (str(backend_root), str(spec_root)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

hiddenimports = [
    "django",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
    "django_filters",
    "django_extensions",
    "drf_yasg",
    "waitress",
    "whitenoise",
    "MySQLdb",
    "win32timezone",
]
hiddenimports += collect_submodules("core", filter=lambda name: ".tests" not in name)
hiddenimports += collect_submodules("integracao", filter=lambda name: ".tests" not in name)
hiddenimports += collect_submodules("sysvarhub")
hiddenimports += collect_submodules("rest_framework", filter=lambda name: ".tests" not in name)
hiddenimports += collect_submodules("django_filters", filter=lambda name: ".tests" not in name)
hiddenimports += collect_submodules("drf_yasg", filter=lambda name: ".tests" not in name)
hiddenimports += collect_submodules("corsheaders", filter=lambda name: ".tests" not in name)
hiddenimports += collect_submodules("whitenoise", filter=lambda name: ".tests" not in name)
datas = collect_data_files("coreschema")

a = Analysis(
    ["windows_service.py"],
    pathex=[str(spec_root), str(backend_root)],
    binaries=[],
    datas=datas,
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
