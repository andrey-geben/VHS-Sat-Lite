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

bp = Blueprint("mosaic", __name__)


@bp.route("/channels/<int:cid>/mosaic/add", methods=["POST"])
@login_required
def mosaic_add_camera(cid):
    ch = get_channel(cid)
    if not ch or ch.get("source_type") != "mosaic":
        abort(404)
    url = (request.form.get("stream_url") or "").strip()
    if not url.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        flash("URL камеры должен начинаться с http://, https://, rtsp:// или rtmp://",
              "error")
        return redirect(url_for("channels.channel_edit_form", cid=cid))
    if len(list_mosaic_sources(cid)) >= 9:
        flash("Максимум 9 камер в мозаике", "error")
        return redirect(url_for("channels.channel_edit_form", cid=cid))
    add_mosaic_source(cid, {
        "stream_url": url,
        "stream_user": request.form.get("stream_user", "").strip() or None,
        "stream_pass": request.form.get("stream_pass", "").strip() or None,
        "label": request.form.get("label", "").strip() or None,
    })
    was_running = supervisor.get_state(cid).get("running")
    if was_running:
        supervisor.stop(cid)
        supervisor.start(get_channel(cid))
    events.info("mosaic", f"camera added to channel {cid}")
    flash("Камера добавлена", "info")
    return redirect(url_for("channels.channel_edit_form", cid=cid))


@bp.route("/channels/<int:cid>/mosaic/<int:sid>/update", methods=["POST"])
@login_required
def mosaic_update_camera(cid, sid):
    ch = get_channel(cid)
    src = get_mosaic_source(sid)
    if not ch or not src or src["channel_id"] != cid:
        abort(404)
    data = {
        "label": request.form.get("label", "").strip() or None,
        "stream_url": request.form.get("stream_url", "").strip() or src["stream_url"],
        "stream_user": request.form.get("stream_user", "").strip() or None,
    }
    if request.form.get("stream_pass"):
        data["stream_pass"] = request.form.get("stream_pass").strip() or None
    update_mosaic_source(sid, data)
    was_running = supervisor.get_state(cid).get("running")
    if was_running:
        supervisor.stop(cid)
        supervisor.start(get_channel(cid))
    flash("Камера обновлена", "info")
    return redirect(url_for("channels.channel_edit_form", cid=cid))


@bp.route("/channels/<int:cid>/mosaic/<int:sid>/remove", methods=["POST"])
@login_required
def mosaic_remove_camera(cid, sid):
    ch = get_channel(cid)
    src = get_mosaic_source(sid)
    if not ch or not src or src["channel_id"] != cid:
        abort(404)
    delete_mosaic_source(sid)
    was_running = supervisor.get_state(cid).get("running")
    if was_running:
        supervisor.stop(cid)
        supervisor.start(get_channel(cid))
    events.info("mosaic", f"camera {sid} removed from channel {cid}")
    flash("Камера удалена", "info")
    return redirect(url_for("channels.channel_edit_form", cid=cid))


@bp.route("/channels/<int:cid>/mosaic/<int:sid>/move/<direction>", methods=["POST"])
@login_required
def mosaic_move_camera(cid, sid, direction):
    ch = get_channel(cid)
    src = get_mosaic_source(sid)
    if not ch or not src or src["channel_id"] != cid:
        abort(404)
    if direction in ("up", "down"):
        move_mosaic_source(sid, direction)
        was_running = supervisor.get_state(cid).get("running")
        if was_running:
            supervisor.stop(cid)
            supervisor.start(get_channel(cid))
    return redirect(url_for("channels.channel_edit_form", cid=cid))
