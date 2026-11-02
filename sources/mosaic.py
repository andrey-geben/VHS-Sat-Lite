"""Mosaic source: N camera streams composed into a grid via xstack.

Grid layout auto-selected by camera count:
  1 → 1×1 (fullscreen), 2 → 1×2, 3-4 → 2×2, 5-6 → 3×2, 7-9 → 3×3.
Empty cells are filled with a dark background. Output is fixed 1280×720.
Audio is DISABLED — mixing mic audio across cameras creates chaos.
"""
from __future__ import annotations

import platform
from pathlib import Path
from urllib.parse import quote, urlparse


def _merge_auth(url: str, user: str, password: str) -> str:
    if not user and not password:
        return url
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        return url
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    netloc = f"{auth}{parsed.hostname or ''}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


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


def _plan_grid(n: int) -> tuple[int, int, int, int, int]:
    """Return (cols, rows, cell_w, cell_h, total_cells)."""
    if n <= 1:
        return 1, 1, 1280, 720, 1
    if n == 2:
        return 1, 2, 1280, 360, 2
    if n <= 4:
        return 2, 2, 640, 360, 4
    if n <= 6:
        return 3, 2, 426, 360, 6
    if n <= 9:
        return 3, 3, 426, 240, 9
    raise ValueError("too many cameras for mosaic (max 9)")


def _build_xstack_layout(cols: int, rows: int) -> str:
    parts: list[str] = []
    for r in range(rows):
        for c in range(cols):
            x = "+".join(f"w{i}" for i in range(c)) if c > 0 else "0"
            y = "+".join(f"h{i * cols}" for i in range(r)) if r > 0 else "0"
            parts.append(f"{x}_{y}")
    return "|".join(parts)


class MosaicSource:
    def __init__(self, channel: dict, cameras: list[dict], base_dir: Path | None = None):
        self.channel = channel
        self.cameras = cameras
        self.data_dir = (base_dir or Path(__file__).resolve().parent.parent) / "data"

    def build_ffmpeg_cmd(self, output: str) -> list[str]:
        c = self.channel
        n = len(self.cameras)
        if n == 0:
            raise RuntimeError("mosaic has no cameras — add at least one")
        if n > 9:
            raise RuntimeError("mosaic supports at most 9 cameras")

        cols, rows, cell_w, cell_h, total = _plan_grid(n)
        layout = _build_xstack_layout(cols, rows)

        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-progress", "pipe:1", "-nostats"]

        # --- video inputs ---
        for cam in self.cameras:
            url = _merge_auth(
                cam.get("stream_url") or "",
                cam.get("stream_user") or "",
                cam.get("stream_pass") or "",
            )
            if url.startswith("rtsp://"):
                cmd += ["-rtsp_transport", "tcp"]
            if url.startswith(("http://", "https://")):
                cmd += ["-reconnect", "1",
                        "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "5"]
            # faster startup, avoid waiting on silent cameras
            cmd += ["-analyzeduration", "3M", "-probesize", "3M",
                    "-i", url]

        # --- filler inputs for empty cells ---
        empties = total - n
        for _ in range(empties):
            cmd += ["-f", "lavfi", "-i",
                    f"color=c=0x1a1a1a:s={cell_w}x{cell_h}:r=15:d=999999"]

        # --- filter graph ---
        filters: list[str] = []
        for i, cam in enumerate(self.cameras):
            parts = [
                f"scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease",
                f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2:color=black",
                "fps=15",
                "setpts=PTS-STARTPTS",
            ]
            label = (cam.get("label") or "").strip()
            if label:
                safe = _escape_drawtext(label[:30])
                parts.append(
                    f"drawtext=fontfile='{_font_path()}':text='{safe}':"
                    f"fontcolor=white:fontsize=18:"
                    f"x=10:y=10:box=1:boxcolor=black@0.5:boxborderw=4"
                )
            filters.append(f"[{i}:v]" + ",".join(parts) + f"[v{i}]")

        for j in range(n, total):
            filters.append(f"[{j}:v]setpts=PTS-STARTPTS[v{j}]")

        stack_in = "".join(f"[v{i}]" for i in range(total))
        filters.append(
            f"{stack_in}xstack=inputs={total}:layout={layout}:fill=black[out]"
        )

        filter_complex = ";".join(filters)

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "6000k",
            "-g", "30",
            "-vsync", "cfr", "-r", "15",
            "-an",  # no audio in mosaics
        ]

        # --- output ---
        if output == "multicast":
            addr = c.get("multicast_addr") or "239.1.1.1"
            port = c.get("multicast_port") or 5000
            cmd += ["-muxdelay", "0.1", "-f", "mpegts",
                    f"udp://{addr}:{port}?pkt_size=1316"]
        elif output == "hls":
            hls_dir = self.data_dir / "hls" / str(c["id"])
            hls_dir.mkdir(parents=True, exist_ok=True)
            cmd += [
                "-f", "hls", "-hls_time", "4", "-hls_list_size", "6",
                "-hls_flags", "delete_segments+append_list+omit_endlist",
                "-hls_segment_filename", str(hls_dir / "seg_%05d.ts"),
                str(hls_dir / "index.m3u8"),
            ]
        else:
            raise ValueError(f"unknown output: {output}")

        return cmd