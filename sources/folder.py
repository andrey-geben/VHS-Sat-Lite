"""Folder-based channel source: enumerate videos, build concat playlist,
build ffmpeg command for each output."""
from __future__ import annotations

import random
from pathlib import Path

from event_writer import events
from .bumper import ensure_bumper
from frozen_utils import ffmpeg_path


def _quote_path(p: Path | str) -> str:
    s = str(p).replace("\\", "/")
    s = s.replace("'", "'\\''")
    return f"'{s}'"


class FolderSource:
    def __init__(self, channel: dict, base_dir: Path | None = None):
        self.channel = channel
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        self.data_dir = self.base_dir / "data"
        self.playlist_dir = self.data_dir / "playlists"
        self.bumper_dir = self.data_dir / "bumpers"

    def enumerate_files(self) -> list[Path]:
        folder = Path(self.channel["folder"])
        if not folder.is_dir():
            raise FileNotFoundError(f"folder not found: {folder}")
        exts = {f".{e.strip().lower().lstrip('.')}"
                for e in (self.channel.get("file_extensions") or "").split(",") if e.strip()}
        files = [f for f in folder.iterdir()
                 if f.is_file() and f.suffix.lower() in exts]
        rotation = self.channel.get("rotation") or "random"
        if rotation == "random":
            random.shuffle(files)
        elif rotation == "mtime_asc":
            files.sort(key=lambda f: f.stat().st_mtime)
        elif rotation == "mtime_desc":
            files.sort(key=lambda f: -f.stat().st_mtime)
        elif rotation == "name":
            files.sort(key=lambda f: f.name.lower())
        return files

    def build_playlist(self) -> Path:
        files = self.enumerate_files()
        if not files:
            raise RuntimeError("no video files found in folder")

        lines: list[str] = []
        bumper_on = bool(self.channel.get("bumper_enabled"))
        bumper_dur = int(self.channel.get("bumper_duration_sec") or 5)

        for f in files:
            if bumper_on:
                bumper = ensure_bumper(f, self.bumper_dir,
                                       duration=bumper_dur, label=f.stem)
                if bumper:
                    lines.append(f"file {_quote_path(bumper)}")
            lines.append(f"file {_quote_path(f)}")

        self.playlist_dir.mkdir(parents=True, exist_ok=True)
        out = self.playlist_dir / f"channel_{self.channel['id']}.txt"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        events.info("folder", f"playlist built for channel {self.channel['id']}",
                    files=len(files), bumpers=bumper_on)
        return out

    def build_ffmpeg_cmd(self, output: str) -> list[str]:
        playlist = self.build_playlist()
        c = self.channel
        cmd = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
            "-progress", "pipe:1", "-nostats",
        ]
        if not c.get("reshuffle_on_loop"):
            cmd += ["-stream_loop", "-1"]
        cmd += ["-re", "-f", "concat", "-safe", "0", "-i", str(playlist)]

        from overlays import overlay_extra_inputs
        extra = overlay_extra_inputs(c)
        if extra:
            cmd += extra

        strategy = (c.get("codec_strategy") or "auto").lower()
        if strategy == "copy":
            cmd += ["-c", "copy"]
        elif strategy == "encode":
            cmd += [
                "-vf", "scale='min(1280,iw)':-2",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-profile:v", "main",
                "-pix_fmt", "yuv420p", "-b:v", "2000k", "-maxrate", "2500k",
                "-bufsize", "5000k", "-g", "50",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
            ]
        elif strategy == "copy_video":
            cmd += [
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            ]
        else:  # auto
            cmd += ["-c", "copy"]

        from overlays import (build_overlay_filter, overlay_output_args,
                              overlay_forces_encode)
        if overlay_forces_encode(c):
            if "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy":
                i = cmd.index("-c")
                cmd[i:i + 2] = [
                    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                    "-profile:v", "main",
                    "-pix_fmt", "yuv420p", "-b:v", "2000k", "-maxrate", "2500k",
                    "-bufsize", "5000k", "-g", "50",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                ]
            overlay = build_overlay_filter(c)
            if overlay:
                cmd += ["-filter_complex", overlay]
                cmd += overlay_output_args()

        if output == "multicast":
            addr = c.get("multicast_addr") or "239.1.1.1"
            port = c.get("multicast_port") or 5000
            cmd += ["-muxdelay", "0.1", "-f", "mpegts",
                    f"udp://{addr}:{port}?pkt_size=1316"]
        elif output == "hls":
            hls_dir = self.data_dir / "hls" / str(c["id"])
            hls_dir.mkdir(parents=True, exist_ok=True)
            cmd += [
                "-sn",
                "-map", "0:v:0", "-map", "0:a:0?",
                "-f", "hls",
                "-hls_time", "6",
                "-hls_list_size", "6",
                "-start_number", "0",
                "-hls_flags", "delete_segments+omit_endlist+temp_file",
                "-hls_segment_filename", str(hls_dir / "seg_%05d.ts"),
                str(hls_dir / "index.m3u8"),
            ]
        else:
            raise ValueError(f"unknown output: {output}")

        return cmd

    def build_source_cmd(self, port: int) -> list[str]:
        """FFmpeg #1: read concat playlist, copy codecs, output MPEG-TS to TCP.

        Reserved for future two-process mode; folder channels currently run
        as a single process (see supervisor._needs_two_process).
        """
        playlist = self.build_playlist()
        c = self.channel
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
               "-nostats"]
        if not c.get("reshuffle_on_loop"):
            cmd += ["-stream_loop", "-1"]
        cmd += ["-re", "-f", "concat", "-safe", "0", "-i", str(playlist),
                "-c", "copy", "-f", "mpegts",
                f"tcp://127.0.0.1:{port}?listen=1"]
        return cmd