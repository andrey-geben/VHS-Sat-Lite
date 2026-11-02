"""Render a short 'Next: <name>' bumper clip for a video file.

Bumpers are MPEG-TS (H.264 + AAC 1920x1080) so `-c copy` in the concat
demuxer works when the user's library already matches these params.
If not — the channel's `codec_strategy` falls back to `encode`.
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
from pathlib import Path

from event_writer import events


def _font_path() -> str:
    system = platform.system()
    if system == "Windows":
        return "C:/Windows/Fonts/arial.ttf"
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            return candidate
    return "Sans"


def _escape_drawtext(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "\\'").replace("%", "\\%"))


def _extract_thumbnail(video: Path, out: Path) -> bool:
    for ss in ("10", "1", "0"):
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-i", str(video),
             "-vframes", "1", "-vf", "scale=800:-1", str(out)],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return True
    return False


def ensure_bumper(
    video: Path,
    bumper_dir: Path,
    duration: int = 5,
    label: str | None = None,
) -> Path | None:
    """Return path to a cached (or freshly rendered) bumper .ts for `video`."""
    bumper_dir.mkdir(parents=True, exist_ok=True)
    try:
        mtime = video.stat().st_mtime
    except OSError:
        return None

    label = label or video.stem
    key = hashlib.sha256(
        f"{video}|{mtime}|{duration}|{label}".encode()
    ).hexdigest()[:16]
    cached = bumper_dir / f"{key}.ts"
    if cached.exists() and cached.stat().st_size > 0:
        return cached

    thumb = bumper_dir / f"{key}.jpg"
    has_thumb = _extract_thumbnail(video, thumb)

    font = _font_path()
    label_safe = _escape_drawtext(label[:60])
    overlay_title = _escape_drawtext("Следующий файл")

    inputs = [
        "-f", "lavfi", "-i", f"color=c=0x0e1418:s=1920x1080:d={duration}",
    ]
    if has_thumb:
        inputs += ["-i", str(thumb)]
    else:
        inputs += ["-f", "lavfi", "-i",
                   f"color=c=0x1a2230:s=800x450:d={duration}"]
    inputs += ["-f", "lavfi", "-i",
               f"anullsrc=r=48000:cl=stereo:d={duration}"]

    filter_complex = (
        f"[1:v]scale=800:-1[thumb];"
        f"[0:v][thumb]overlay=(W-w)/2:(H-h)/2-80[bg];"
        f"[bg]drawtext=fontfile='{font}':text='{overlay_title}':"
        f"fontcolor=0x88aacc:fontsize=36:x=(w-text_w)/2:y=140[v1];"
        f"[v1]drawtext=fontfile='{font}':text='{label_safe}':"
        f"fontcolor=white:fontsize=56:x=(w-text_w)/2:y=h-140[outv]"
    )

    cmd = (
        ["ffmpeg", "-y", "-loglevel", "error"] + inputs +
        ["-filter_complex", filter_complex,
         "-map", "[outv]", "-map", "2:a",
         "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
         "-pix_fmt", "yuv420p", "-b:v", "2000k",
         "-c:a", "aac", "-b:a", "96k", "-ar", "48000",
         "-t", str(duration), "-f", "mpegts", str(cached)]
    )

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        events.warn("bumper", f"timeout rendering bumper for {video.name}")
        return None
    finally:
        if thumb.exists():
            thumb.unlink(missing_ok=True)

    if r.returncode != 0 or not cached.exists():
        events.warn("bumper", f"failed to render bumper for {video.name}",
                    stderr=(r.stderr or "")[-500:])
        return None
    return cached