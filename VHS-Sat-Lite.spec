# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for VHS-Sat-Lite (Windows, onedir).

Output: dist/VHS-Sat-Lite/
  ├── VHS-Sat-Lite.exe
  ├── _internal/         (Python runtime, templates/, static/, *.pyd)
  └── (bin/, data/, logs/, downloads/, config.yaml — создаются post-build
       или при первом запуске .exe)

FFmpeg (bin/ffmpeg.exe, bin/ffprobe.exe) НЕ bundle в _internal/ —
он кладётся рядом с .exe скриптом build.ps1, потому что frozen_utils
ищет APP_ROOT/bin/ffmpeg.exe.
"""
from PyInstaller.utils.hooks import collect_data_files
import os

project_root = os.path.abspath(".")

a = Analysis(
    ["server.py"],
    pathex=[project_root],
    binaries=[],
    datas=[
        ("templates", "templates"),
        ("static", "static"),
    ],
    hiddenimports=[
        "waitress",
        "qrcode",
        "qrcode.image.pil",
        "PIL",
        "PIL.Image",
        "yaml",
        "flask",
        "jinja2",
        "werkzeug",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VHS-Sat-Lite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="static/favicon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VHS-Sat-Lite",
)