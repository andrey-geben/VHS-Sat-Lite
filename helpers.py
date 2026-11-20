"""Общие помощники маршрутов."""
from __future__ import annotations

from pathlib import Path

from channel_utils import enrich_channel
from db import (
    find_channel_by_number, find_channels_by_folder,
    find_channels_by_stream_url,
)
from runtime import supervisor


def to_int(value, default=None):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def to_bool(value) -> bool:
    return str(value).lower() in ("1", "true", "on", "yes")

def channel_view(ch: dict) -> dict:
    st = supervisor.get_state(ch["id"])
    base = {**ch, **st, "running": bool(st.get("running"))}
    return enrich_channel(base)

def validate_channel_payload(data: dict, exclude_id: int | None = None) -> str | None:
    name = (data.get("name") or "").strip()
    if not name:
        return "название канала обязательно"

    source_type = (data.get("source_type") or "folder").lower()

    if source_type == "folder":
        folder = (data.get("folder") or "").strip()
        if not folder:
            return "путь к папке обязателен"
        p = Path(folder)
        if not p.is_dir():
            return f"папка не существует: {folder}"
        dup = find_channels_by_folder(folder, exclude_id=exclude_id)
        if dup:
            return f"эта папка уже используется каналом «{dup[0]['name']}» (id={dup[0]['id']})"
        exts = (data.get("file_extensions") or "").lower()
        if not any(p.suffix.lower().lstrip(".") in exts for p in p.iterdir() if p.is_file()):
            return "в папке нет видеофайлов с указанными расширениями"

    elif source_type in ("iptv", "camera"):
        url = (data.get("stream_url") or "").strip()
        if not url:
            return "URL потока обязателен"
        if not url.startswith(("http://", "https://", "rtsp://", "rtmp://", "udp://", "rtp://")):
            return "URL должен начинаться с http://, https://, rtsp://, rtmp://, udp:// или rtp://"
        dup = find_channels_by_stream_url(url)
        dup = [d for d in dup if d["id"] != exclude_id]
        if dup:
            return f"этот URL уже используется каналом «{dup[0]['name']}» (id={dup[0]['id']})"

    elif source_type == "mosaic":
        pass

    else:
        return f"неизвестный source_type: {source_type}"

    if not (data.get("hls_enabled") or data.get("multicast_enabled")):
        return "включите хотя бы один вывод (HLS или multicast)"

    num = data.get("channel_number")
    if num is not None:
        try:
            num_int = int(num)
        except (TypeError, ValueError):
            return "номер канала должен быть целым числом"
        if num_int < 1:
            return "номер канала должен быть ≥ 1"
        dup = find_channel_by_number(num_int, exclude_id=exclude_id)
        if dup:
            return f"номер {num_int} уже занят каналом «{dup['name']}»"
    return None

def parse_schedule_time(value: str) -> str | None:
    v = (value or "").strip()
    try:
        h, m = v.split(":")
        hi, mi = int(h), int(m)
        if 0 <= hi <= 23 and 0 <= mi <= 59:
            return f"{hi:02d}:{mi:02d}"
    except (ValueError, AttributeError):
        pass
    return None

def parse_schedule_days(form) -> str:
    days = [d for d in form.getlist("days") if d in ("1", "2", "3", "4", "5", "6", "7")]
    if not days:
        days = ["1", "2", "3", "4", "5", "6", "7"]
    return ",".join(sorted(set(days)))
