"""Helpers that enrich a channel dict with computed fields:

- uptime + human-friendly duration
- staleness for running HLS channels (based on latest segment mtime)
- folder stats (file count, total size) — cached for 30 s
- multicast URL (udp://@...)

Cache is in-process and time-limited so /channels doesn't
re-scan folders on every refresh.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from frozen_utils import resolve_path


_cache: dict = {}
_CACHE_TTL = 30  # seconds


def _cached(key, factory):
    now = time.time()
    entry = _cache.get(key)
    if entry and (now - entry[0]) < _CACHE_TTL:
        return entry[1]
    value = factory()
    _cache[key] = (now, value)
    return value


def humanize_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m} мин" + (f" {s} с" if s else "")
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h} ч" + (f" {m} мин" if m else "")
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d} д" + (f" {h} ч" if h else "")


def humanize_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def _latest_segment_mtime(channel_id: int) -> float | None:
    hls_dir = resolve_path("data/hls") / str(channel_id)
    if not hls_dir.is_dir():
        return None
    latest = 0.0
    for seg in hls_dir.glob("seg_*.ts"):
        try:
            m = seg.stat().st_mtime
        except OSError:
            continue
        if m > latest:
            latest = m
    return latest or None


def _folder_stats(folder: str, extensions: str) -> tuple[int, int]:
    p = Path(folder)
    if not p.is_dir():
        return 0, 0
    exts = {"." + e.strip().lower().lstrip(".") for e in extensions.split(",") if e.strip()}
    count = total = 0
    try:
        for f in p.iterdir():
            if f.is_file() and f.suffix.lower() in exts:
                try:
                    total += f.stat().st_size
                    count += 1
                except OSError:
                    pass
    except OSError:
        return 0, 0
    return count, total


def enrich_channel(ch: dict) -> dict:
    """Return ch with computed fields (non-destructive)."""
    extra: dict = {}

    # ----- uptime -----
    started = ch.get("started_at")
    if ch.get("running") and started:
        try:
            t = datetime.fromisoformat(started)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            delta = int((datetime.now(timezone.utc) - t).total_seconds())
            extra["uptime_sec"] = max(0, delta)
            extra["uptime_human"] = humanize_duration(max(0, delta))
            extra["started_at_local"] = t.astimezone().strftime("%H:%M")
        except (ValueError, TypeError):
            pass

    # ----- staleness (running + HLS only) -----
    if ch.get("running") and ch.get("hls_enabled"):
        m = _cached(("seg_mtime", ch["id"]),
                    lambda cid=ch["id"]: _latest_segment_mtime(cid))
        if m:
            age = int(time.time() - m)
            extra["hls_age_sec"] = age
            if age > 30:
                extra["is_stale"] = True
                extra["stale_seconds"] = age
                extra["stale_human"] = humanize_duration(age)

    # ----- folder stats (cached) -----
    if ch.get("source_type") == "folder" and ch.get("folder"):
        stats = _cached(
            ("folder", ch["id"]),
            lambda c=ch: _folder_stats(c["folder"], c.get("file_extensions") or ""),
        )
        extra["folder_files"] = stats[0]
        extra["folder_size"] = humanize_size(stats[1])

    # ----- output URLs -----
    extra["has_hls"] = bool(ch.get("hls_enabled"))
    if ch.get("multicast_enabled") and ch.get("multicast_addr"):
        extra["multicast_url"] = (
            f"udp://@{ch['multicast_addr']}:{ch['multicast_port']}"
        )

    return {**ch, **extra}