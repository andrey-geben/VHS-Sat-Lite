"""EPG for local (folder) channels.

Reads the same playlist that ffmpeg is currently using, probes each file's
duration (cached in SQLite by path + mtime), and projects a timeline forward
from the channel's current playback position.

For IPTV / cameras — returns [] (direct broadcast, no schedule).
For stopped channels — returns [] (playlist may be stale).

Bumpers are folded into the offset of the following video — the user sees
only real files in the schedule, not "анонс 5 сек" between each.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from db import get_db
from frozen_utils import resolve_path


def _parse_playlist(playlist_path: Path) -> list[tuple[str, bool]]:
    """Return [(path, is_bumper), ...] parsed from ffmpeg concat list."""
    if not playlist_path.exists():
        return []
    items: list[tuple[str, bool]] = []
    try:
        text = playlist_path.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("file '"):
            continue
        inner = line[len("file '"):-1]  # strip "file '" and trailing "'"
        # undo ffmpeg concat escaping: ' → '\''
        path = "'".join(inner.split("'\\''"))
        norm = path.replace("\\", "/")
        is_bumper = "/bumpers/" in norm
        items.append((path, is_bumper))
    return items


def probe_duration(path: str) -> int:
    """Return duration in seconds; 0 on any error. Cached by (path, mtime)."""
    p = Path(path)
    if not p.is_file():
        return 0
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return 0

    with get_db() as conn:
        row = conn.execute(
            "SELECT duration_sec FROM file_durations WHERE path = ? AND mtime = ?",
            (str(p), mtime),
        ).fetchone()
    if row is not None:
        return int(row[0])

    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(p)],
            capture_output=True, text=True, timeout=10,
        )
        dur = int(float(r.stdout.strip())) if r.returncode == 0 and r.stdout.strip() else 0
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError, OSError):
        dur = 0

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO file_durations (path, mtime, duration_sec) "
            "VALUES (?, ?, ?)",
            (str(p), mtime, dur),
        )
    return dur


def channel_schedule(
    channel: dict,
    state: dict,
    horizon_min: int = 180,
) -> list[dict]:
    """Return [{offset_sec, duration_sec, name, is_current}, ...].

    Only for folder channels that are currently running. Empty list otherwise.
    """
    if (channel.get("source_type") or "folder") != "folder":
        return []
    if not state.get("running"):
        return []

    playlist = resolve_path("data/playlists") / f"channel_{channel['id']}.txt"
    items = _parse_playlist(playlist)
    if not items:
        return []

    bumper_dur = int(channel.get("bumper_duration_sec") or 5)
    durations: list[int] = []
    for path, is_bumper in items:
        if is_bumper:
            durations.append(bumper_dur)
        else:
            durations.append(probe_duration(path))

    cycle = sum(durations)
    if cycle <= 0:
        return []

    position = int(state.get("position_sec") or 0) % cycle

    # locate current item
    current_idx = 0
    offset_in_current = 0
    cumulative = 0
    for i, dur in enumerate(durations):
        if position < cumulative + dur:
            current_idx = i
            offset_in_current = position - cumulative
            break
        cumulative += dur

    # build forward schedule
    schedule: list[dict] = []
    horizon_sec = horizon_min * 60
    elapsed = 0
    pending_bumper = 0

    for i in range(current_idx, len(items)):
        path, is_bumper = items[i]
        dur = durations[i]
        remaining = dur - offset_in_current if i == current_idx else dur

        if is_bumper:
            if i == current_idx:
                # we're inside the bumper right now — show as current
                schedule.append({
                    "offset_sec": 0,
                    "duration_sec": max(0, remaining),
                    "name": "📢 анонс",
                    "is_current": True,
                })
                elapsed += max(0, remaining)
            else:
                pending_bumper += dur
                elapsed += dur
                continue
        else:
            schedule.append({
                "offset_sec": elapsed + pending_bumper,
                "duration_sec": dur,
                "name": Path(path).name,
                "is_current": i == current_idx,
            })
            elapsed += pending_bumper + remaining
            pending_bumper = 0

        if elapsed >= horizon_sec:
            break

    return schedule


def now_next(channel: dict, state: dict) -> tuple[str | None, str | None]:
    """Return (current_name, next_name) for the channel card. Cheap thanks to cache."""
    sched = channel_schedule(channel, state, horizon_min=120)
    if not sched:
        return None, None
    current = sched[0]["name"] if sched[0].get("is_current") else None
    following = sched[1]["name"] if len(sched) > 1 else None
    return current, following