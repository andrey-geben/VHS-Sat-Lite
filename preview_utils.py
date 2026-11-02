"""Preview (announcement) utilities for folder channels.

Given a running folder channel and its current position, tells whether
the channel is close to the end of the current file, and which file
is next. Used by supervisor to write data/preview/<cid>.txt, which
FFmpeg picks up via drawtext=textfile=...:reload=1.

Reuses epg_utils._parse_playlist and epg_utils.probe_duration.
"""
from __future__ import annotations

from pathlib import Path

from epg_utils import _parse_playlist, probe_duration
from frozen_utils import resolve_path


def build_timeline(channel: dict) -> list[dict] | None:
    """Return timeline for folder channel:
    [{"start": 0, "end": 120, "name": "file1.mp4"}, ...]

    Bumpers are folded into the offset of the following file
    (same logic as epg_utils.channel_schedule). File name = Path(path).name.
    Returns None for non-folder channels or if playlist missing.
    """
    if (channel.get("source_type") or "folder") != "folder":
        return None
    playlist = resolve_path("data/playlists") / f"channel_{channel['id']}.txt"
    items = _parse_playlist(playlist)
    if not items:
        return None

    bumper_dur = int(channel.get("bumper_duration_sec") or 5)
    timeline: list[dict] = []
    elapsed = 0
    pending_bumper = 0

    for path, is_bumper in items:
        if is_bumper:
            pending_bumper += bumper_dur
            continue
        dur = probe_duration(path)
        if dur <= 0:
            # unknown duration — cannot schedule reliably; skip
            pending_bumper = 0
            continue
        start = elapsed + pending_bumper
        end = start + dur
        timeline.append({
            "start": start,
            "end": end,
            "name": Path(path).name,
        })
        elapsed = end
        pending_bumper = 0

    return timeline or None


def next_name_near_end(
    timeline: list[dict],
    position_sec: int,
    lead_sec: int,
) -> str | None:
    """Return name of next file if position is within [end-lead, end) of current.

    Takes -stream_loop into account: position wraps around by cycle length.
    Returns None if not near end or if there is no next (last in cycle).
    """
    if not timeline:
        return None
    cycle = timeline[-1]["end"]
    if cycle <= 0:
        return None
    pos = int(position_sec) % cycle

    for i, item in enumerate(timeline):
        if item["start"] <= pos < item["end"]:
            remaining = item["end"] - pos
            if remaining <= lead_sec and i + 1 < len(timeline):
                return timeline[i + 1]["name"]
            return None
    return None