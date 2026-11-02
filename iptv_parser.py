"""M3U/M3U8 playlist parser for IPTV import."""
from __future__ import annotations

import re
import urllib.request
from urllib.parse import urlparse


_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
_URL_SCHEMES = ("http://", "https://", "rtsp://", "rtmp://", "udp://", "rtp://")


def parse_m3u(text: str) -> list[dict]:
    """Return [{name, url, tvg_id, tvg_logo, group}, ...]."""
    result: list[dict] = []
    current: dict | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            meta_part, _, name_part = line.partition(",")
            attrs = dict(_ATTR_RE.findall(meta_part))
            current = {
                "name": (name_part.strip()
                         or attrs.get("tvg-name")
                         or "Unnamed"),
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_logo": attrs.get("tvg-logo", ""),
                "group": attrs.get("group-title", ""),
                "url": None,
            }
        elif line.startswith("#"):
            continue
        elif current is not None:
            current["url"] = line
            result.append(current)
            current = None
        elif line.startswith(_URL_SCHEMES):
            tail = urlparse(line).path.rsplit("/", 1)[-1]
            result.append({
                "name": tail or line,
                "url": line,
                "tvg_id": "", "tvg_logo": "", "group": "",
            })

    return [item for item in result if item.get("url")]


def fetch_playlist(url: str, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "VHS-Sat/0.3"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")