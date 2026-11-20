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

bp = Blueprint("api", __name__)


@bp.route("/api/status")
def api_status():
    return jsonify({
        "title": config.get("server", "title"),
        "backend": "lokalchanel (windows-lite)",
        "platform": platform.system(),
        "edition": "lite",
        "auth_enabled": config.get("security", "auth_enabled"),
        "users_configured": config.has_users(),
    })


@bp.route("/api/tools")
@login_required
def api_tools():
    return jsonify(detect_lite_tools())


@bp.route("/api/events")
@login_required
def api_events():
    limit = int(request.args.get("limit", 100))
    return jsonify(events.read_tail(limit))


@bp.route("/api/channels", methods=["GET"])
@login_required
def api_channels():
    return jsonify([channel_view(c) for c in list_channels()])


@bp.route("/api/channels", methods=["POST"])
@login_required
def api_channels_create():
    data = request.get_json(silent=True) or {}
    data.setdefault("source_type", "folder")
    data.setdefault("rotation", "random")
    data.setdefault("file_extensions", "mp4,mkv,avi,ts,mov,m4v,webm")
    data.setdefault("codec_strategy", "auto")
    data.setdefault("bumper_duration_sec", 5)
    data.setdefault("stream_url", None)
    data.setdefault("stream_user", None)
    data.setdefault("stream_pass", None)
    data.setdefault("channel_number", None)
    data.setdefault("multicast_addr", "239.1.1.1")
    data.setdefault("multicast_port", 5000)
    data.setdefault("enabled", 1)
    for k in ("reshuffle_on_loop", "bumper_enabled", "hls_enabled", "multicast_enabled"):
        if k in data:
            data[k] = 1 if data[k] else 0
    err = validate_channel_payload(data)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    if not data.get("channel_number"):
        data["channel_number"] = next_free_number()
    cid = create_channel(data)
    return jsonify({"ok": True, "id": cid}), 201


@bp.route("/api/channels/<int:cid>", methods=["GET"])
@login_required
def api_channel_get(cid):
    ch = get_channel(cid)
    if not ch:
        return jsonify({"error": "not found"}), 404
    return jsonify(channel_view(ch))


@bp.route("/api/channels/<int:cid>", methods=["PATCH"])
@login_required
def api_channel_patch(cid):
    ch = get_channel(cid)
    if not ch:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    err = validate_channel_payload({**ch, **data}, exclude_id=cid)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    was_running = supervisor.get_state(cid).get("running")
    if was_running:
        supervisor.stop(cid)
    update_channel(cid, data)
    if was_running:
        supervisor.start(get_channel(cid))
    return jsonify({"ok": True, "channel": channel_view(get_channel(cid))})


@bp.route("/api/channels/<int:cid>", methods=["DELETE"])
@login_required
def api_channel_delete(cid):
    if not get_channel(cid):
        return jsonify({"error": "not found"}), 404
    supervisor.stop(cid)
    delete_channel(cid)
    return jsonify({"ok": True})


@bp.route("/api/channels/<int:cid>/start", methods=["POST"])
@login_required
def api_channel_start(cid):
    ch = get_channel(cid)
    if not ch:
        return jsonify({"error": "not found"}), 404
    started = supervisor.start(ch)
    return jsonify({"ok": started, "state": supervisor.get_state(cid)})


@bp.route("/api/channels/<int:cid>/stop", methods=["POST"])
@login_required
def api_channel_stop(cid):
    if not get_channel(cid):
        return jsonify({"error": "not found"}), 404
    stopped = supervisor.stop(cid)
    return jsonify({"ok": stopped, "state": supervisor.get_state(cid)})


@bp.route("/api/channels/<int:cid>/state")
@login_required
def api_channel_state(cid):
    if not get_channel(cid):
        return jsonify({"error": "not found"}), 404
    return jsonify(supervisor.get_state(cid))
