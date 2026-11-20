from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

from flask import (
    Blueprint, abort, flash, jsonify, redirect, render_template,
    request, url_for,
)

from auth import login_required
from config import config
from db import (
    add_mosaic_source, check_ticker_overlap, create_channel, create_schedule,
    create_ticker, delete_channel, delete_mosaic_source, delete_schedule,
    delete_ticker, find_channel_by_number, find_channels_by_folder,
    find_channels_by_stream_url, get_channel, get_default_ticker,
    get_mosaic_source, get_schedule, get_ticker, list_channels,
    list_mosaic_sources, list_schedules, list_tickers, move_mosaic_source,
    next_free_number, update_channel, update_mosaic_source, update_schedule,
    update_ticker,
)
from event_writer import events
from frozen_utils import APP_ROOT, detect_lite_tools
from helpers import (
    to_int, to_bool, channel_view, validate_channel_payload,
    parse_schedule_time, parse_schedule_days,
)
from iptv_parser import fetch_playlist, parse_m3u
from runtime import DATA_DIR, supervisor

bp = Blueprint("iptv", __name__)


@bp.route("/channels/iptv/import", methods=["GET", "POST"])
@login_required
def channels_iptv_import():
    url = (request.values.get("url") or "").strip()
    uploaded = request.files.get("file")

    playlist_text = ""
    if uploaded and uploaded.filename:
        playlist_text = uploaded.read().decode("utf-8", errors="replace")
    elif url:
        try:
            playlist_text = fetch_playlist(url)
        except Exception as exc:  # noqa: BLE001
            flash(f"Не удалось загрузить плейлист: {exc}", "error")
            return redirect(url_for("channels.channel_new_form", source_type="iptv"))

    if not playlist_text:
        flash("Укажите URL плейлиста или загрузите файл", "error")
        return redirect(url_for("channels.channel_new_form", source_type="iptv"))

    entries = parse_m3u(playlist_text)
    if not entries:
        flash("В плейлисте не найдено ни одного канала", "warn")
        return redirect(url_for("channels.channel_new_form", source_type="iptv"))

    groups = sorted({e["group"] for e in entries if e.get("group")})
    return render_template("iptv_import.html",
                           entries=entries, groups=groups, source_url=url)


@bp.route("/channels/iptv/bulk-import", methods=["POST"])
@login_required
def channels_iptv_bulk_import():
    names = request.form.getlist("name")
    urls = request.form.getlist("url")
    strategy = request.form.get("codec_strategy", "auto")
    hls = 1 if to_bool(request.form.get("hls_enabled", "on")) else 0
    mcast = 1 if to_bool(request.form.get("multicast_enabled")) else 0
    mcast_addr = request.form.get("multicast_addr", "239.1.1.1").strip()
    mcast_port = to_int(request.form.get("multicast_port"), 5000)

    created, skipped = 0, 0
    next_port = mcast_port
    for i, u in enumerate(urls):
        u = u.strip()
        if not u:
            continue
        nm = (names[i].strip() if i < len(names) else "") or f"IPTV {i+1}"
        data = {
            "name": nm,
            "source_type": "iptv",
            "folder": None,
            "rotation": "random",
            "reshuffle_on_loop": 0,
            "file_extensions": "mp4,mkv,avi,ts,mov,m4v,webm",
            "codec_strategy": strategy,
            "bumper_enabled": 0,
            "bumper_duration_sec": 5,
            "stream_url": u,
            "stream_user": None,
            "stream_pass": None,
            "channel_number": None,
            "hls_enabled": hls,
            "multicast_enabled": mcast,
            "multicast_addr": mcast_addr,
            "multicast_port": next_port,
            "preview_enabled": 0,
            "preview_lead_sec": 60,
            "ticker_enabled": 0,
            "ticker_text": "",
            "ticker_speed": 100,
            "ticker_fontsize": 24,
            "ticker_color": "white",
            "ticker_bgcolor": "black@0.5",
            "pip_enabled": 0,
            "pip_mode": "none",
            "pip_source": "",
            "pip_position": "top-right",
            "pip_width": 320,
            "pip_margin": 16,
            "enabled": 1,
        }
        err = validate_channel_payload(data)
        if err:
            skipped += 1
            continue
        data["channel_number"] = next_free_number()
        try:
            create_channel(data)
            created += 1
            if mcast:
                next_port += 1
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            events.warn("channels", f"IPTV import: skip {nm}: {exc}")

    events.info("channels",
                f"IPTV import: created={created}, skipped={skipped}")
    flash(f"Импортировано {created} канал(ов)"
          + (f", пропущено {skipped}" if skipped else ""),
          "info" if created else "warn")
    return redirect(url_for("channels.channels_page"))
