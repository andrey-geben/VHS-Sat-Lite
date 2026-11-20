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

bp = Blueprint("events", __name__)


@bp.route("/events")
@login_required
def events_page():
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 2000))
    return render_template("events.html", events=events.read_tail(limit))


@bp.route("/events/clear", methods=["POST"])
@login_required
def events_clear():
    by = (current_user() or {}).get("username")
    removed = events.clear(by=by)
    flash(f"Журнал очищен (удалено {removed} записей)", "info")
    return redirect(url_for("events.events_page"))
