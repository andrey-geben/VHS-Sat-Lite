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

bp = Blueprint("main", __name__)


@bp.route("/")
@login_required
def dashboard():
    channels = [channel_view(c) for c in list_channels()]
    return render_template("dashboard_lite.html",
                           channels=channels, tools=detect_lite_tools())
