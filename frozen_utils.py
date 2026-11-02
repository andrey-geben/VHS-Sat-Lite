"""Path resolution that works both as script and as frozen .exe.

- APP_ROOT:      writable — next to the .exe, or project root in dev.
                 Everything that must persist (config.yaml, data/, logs/)
                 lives here.
- RESOURCE_ROOT: read-only bundle — sys._MEIPASS in frozen, else project root.
                 templates/ and static/ live here.

find_tool(): looks for a bundled bin/<name>[.exe] next to the app first,
then falls back to PATH.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


APP_ROOT = _app_root()
RESOURCE_ROOT = _resource_root()


def resolve_path(p: str | Path) -> Path:
    """Resolve relative paths against APP_ROOT (so frozen .exe uses
    its own folder, not whatever CWD happens to be)."""
    path = Path(p)
    return path if path.is_absolute() else APP_ROOT / path


def find_tool(name: str) -> str | None:
    """Locate ffmpeg/ffprobe: bundled bin/ first, then PATH."""
    exe = ".exe" if sys.platform == "win32" else ""
    bundled = APP_ROOT / "bin" / f"{name}{exe}"
    if bundled.is_file():
        return str(bundled)
    return shutil.which(name)


def detect_lite_tools() -> dict[str, str | None]:
    """Tools used by the Windows Lite edition."""
    return {"ffmpeg": find_tool("ffmpeg"), "ffprobe": find_tool("ffprobe")}

def ffmpeg_path() -> str:
    return str(APP_ROOT / "bin" / "ffmpeg.exe")

def ffprobe_path() -> str:
    return str(APP_ROOT / "bin" / "ffprobe.exe")