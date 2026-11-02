"""Stream source for IPTV and camera URLs (HLS / MPEG-TS / RTSP / RTMP)."""
from __future__ import annotations
from frozen_utils import ffmpeg_path

from pathlib import Path
from urllib.parse import quote, urlparse


def _merge_auth(url: str, user: str, password: str) -> str:
    if not user and not password:
        return url
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        return url  # already embedded
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    netloc = f"{auth}{parsed.hostname or ''}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


class StreamSource:
    def __init__(self, channel: dict, base_dir: Path | None = None):
        self.channel = channel
        self.data_dir = (base_dir or Path(__file__).resolve().parent.parent) / "data"

    def build_ffmpeg_cmd(self, output: str) -> list[str]:
        c = self.channel
        url = _merge_auth(
            c.get("stream_url") or "",
            c.get("stream_user") or "",
            c.get("stream_pass") or "",
        )
        if not url:
            raise RuntimeError("stream_url is empty")

        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
               "-progress", "pipe:1", "-nostats"]

        if url.startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp"]

        if url.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"]

        cmd += ["-i", url]

        from overlays import overlay_extra_inputs
        extra = overlay_extra_inputs(c)
        if extra:
            cmd += extra

        # --- codec strategy ---
        strategy = (c.get("codec_strategy") or "auto").lower()
        if strategy == "auto":
            # IPTV: providers push ready-to-use TS/HLS → copy.
            # Cameras: often H.265/G.711 → must encode for browser HLS.
            #strategy = "copy" if c.get("source_type") == "iptv" else "encode"
            strategy = "encode"

        cmd += ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]
        if strategy == "copy":
            cmd += ["-c", "copy"]
        elif strategy == "encode":
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                "-pix_fmt", "yuv420p", "-b:v", "2500k", "-maxrate", "3000k",
                "-bufsize", "6000k", "-g", "50",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            ]
        elif strategy == "copy_video":
            cmd += [
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
            ]
        else:
            cmd += ["-c", "copy"]
        # --- overlays (ticker, later PiP) ---
        from overlays import build_overlay_filter, overlay_output_args, overlay_forces_encode
        if overlay_forces_encode(c):
            if "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy":
                i = cmd.index("-c")
                cmd[i:i + 2] = [
                    "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                    "-pix_fmt", "yuv420p", "-b:v", "2500k", "-maxrate", "3000k",
                    "-bufsize", "6000k", "-g", "50",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                ]
            overlay = build_overlay_filter(c)
            if overlay:
                cmd += ["-filter_complex", overlay]
                cmd += overlay_output_args()

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


    def build_source_cmd(self, port: int) -> list[str]:
        """FFmpeg #1: read input, copy codecs, output MPEG-TS to TCP.

        No filters, no re-encode. Encode + overlay happen in FFmpeg #2
        which connects to tcp://127.0.0.1:<port>.
        """
        c = self.channel
        url = _merge_auth(
            c.get("stream_url") or "",
            c.get("stream_user") or "",
            c.get("stream_pass") or "",
        )
        if not url:
            raise RuntimeError("stream_url is empty")

        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
               "-nostats"]

        if url.startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp"]

        if url.startswith(("http://", "https://")):
            cmd += ["-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "10"]

        cmd += ["-i", url, "-c", "copy", "-f", "mpegts",
                f"tcp://127.0.0.1:{port}?listen=1"]
        return cmd


    def build_overlay_cmd(self, port: int, output: str = "hls") -> list[str]:
        """FFmpeg #2: read TCP from FFmpeg #1, apply overlays, encode, write output.

        Everything that was in build_ffmpeg_cmd, except:
        - input is tcp://127.0.0.1:<port> instead of the provider URL
        - no -reconnect flags (not needed for local TCP)
        - always encodes (copy is meaningless when overlays are applied;
          for "no overlays" case we still encode to keep it simple —
          can be optimized later)
        """
        c = self.channel
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "warning",
               "-progress", "pipe:1", "-nostats",
               "-i", f"tcp://127.0.0.1:{port}"]

        # overlays: add PiP image as extra input BEFORE codec options
        from overlays import overlay_extra_inputs
        extra = overlay_extra_inputs(c)
        if extra:
            cmd += extra

        cmd += ["-fflags", "+genpts", "-avoid_negative_ts", "make_zero"]

        # always encode in FFmpeg #2 (overlays force re-encode anyway)
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-b:v", "2500k", "-maxrate", "3000k",
            "-bufsize", "6000k", "-g", "50",
            "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
        ]

        # overlays: filter_complex + maps
        from overlays import build_overlay_filter, overlay_output_args
        overlay = build_overlay_filter(c)
        if overlay:
            cmd += ["-filter_complex", overlay]
            cmd += overlay_output_args()

        # output
        if output == "multicast":
            addr = c.get("multicast_addr") or "239.1.1.1"
            port_out = c.get("multicast_port") or 5000
            cmd += ["-muxdelay", "0.1", "-f", "mpegts",
                    f"udp://{addr}:{port_out}?pkt_size=1316"]
        elif output == "hls":
            hls_dir = self.data_dir / "hls" / str(c["id"])
            hls_dir.mkdir(parents=True, exist_ok=True)
            cmd += [
                "-f", "hls", "-hls_time", "4", "-hls_list_size", "6",
                "-hls_flags", "delete_segments+omit_endlist",
                "-hls_segment_filename", str(hls_dir / "seg_%05d.ts"),
                str(hls_dir / "index.m3u8"),
            ]
        else:
            raise ValueError(f"unknown output: {output}")

        return cmd

