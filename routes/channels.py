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

bp = Blueprint("channels", __name__)


@bp.route("/channels")
@login_required
def channels_page():
    from epg_utils import now_next

    all_view = []
    for ch in list_channels():
        cv = channel_view(ch)
        if ch.get("source_type") == "folder" and cv.get("running"):
            try:
                cv["epg_now"], cv["epg_next"] = now_next(
                    ch, supervisor.get_state(ch["id"]),
                )
            except Exception:  # noqa: BLE001
                cv["epg_now"] = cv["epg_next"] = None
        all_view.append(cv)

    counts = {
        "total": len(all_view),
        "folder": sum(1 for c in all_view if c.get("source_type") == "folder"),
        "iptv": sum(1 for c in all_view if c.get("source_type") == "iptv"),
        "camera": sum(1 for c in all_view if c.get("source_type") == "camera"),
        "mosaic": sum(1 for c in all_view if c.get("source_type") == "mosaic"),
    }

    type_filter = (request.args.get("type") or "").strip().lower()
    if type_filter in ("folder", "iptv", "camera", "mosaic"):
        view = [c for c in all_view if c.get("source_type") == type_filter]
    else:
        type_filter = ""
        view = all_view

    view.sort(key=lambda c: (
        0 if c.get("running") else 1,
        c.get("channel_number") or 9999,
    ))

    live = [c for c in view if c.get("running")]
    offline = [c for c in view if not c.get("running")]

    return render_template(
        "channels.html",
        live=live, offline=offline,
        counts=counts, current_type=type_filter,
        tools=detect_lite_tools(),
    )


@bp.route("/channels/new")
@login_required
def channels_new_choice():
    return render_template("channels_new.html")


@bp.route("/channels/new/<source_type>")
@login_required
def channel_new_form(source_type):
    if source_type not in ("folder", "iptv", "camera", "mosaic"):
        abort(404)
    return render_template("channel_new.html", form_type=source_type)


@bp.route("/channels/create", methods=["POST"])
@login_required
def channels_create():
    source_type = request.form.get("source_type", "folder").lower()
    if source_type not in ("folder", "mosaic"):
        source_type = "folder"
    data = {
        "name": request.form.get("name", "").strip(),
        "source_type": source_type,
        "folder": request.form.get("folder", "").strip() or None,
        "rotation": request.form.get("rotation", "random"),
        "reshuffle_on_loop": 1 if to_bool(request.form.get("reshuffle_on_loop")) else 0,
        "file_extensions": request.form.get("file_extensions", "mp4,mkv,avi,ts,mov,m4v,webm"),
        "codec_strategy": request.form.get("codec_strategy", "auto"),
        "bumper_enabled": 1 if to_bool(request.form.get("bumper_enabled")) else 0,
        "bumper_duration_sec": to_int(request.form.get("bumper_duration_sec"), 5),
        "stream_url": None,
        "stream_user": None,
        "stream_pass": None,
        "channel_number": to_int(request.form.get("channel_number"), None),
        "hls_enabled": 1 if to_bool(request.form.get("hls_enabled", "on")) else 0,
        "multicast_enabled": 1 if to_bool(request.form.get("multicast_enabled")) else 0,
        "multicast_addr": request.form.get("multicast_addr", "239.1.1.1").strip(),
        "multicast_port": to_int(request.form.get("multicast_port"), 5000),
        "preview_enabled": 1 if to_bool(request.form.get("preview_enabled")) else 0,
        "preview_lead_sec": to_int(request.form.get("preview_lead_sec"), 60),
        "ticker_enabled": 1 if to_bool(request.form.get("ticker_enabled")) else 0,
        "ticker_text": request.form.get("ticker_text", "").strip(),
        "ticker_speed": to_int(request.form.get("ticker_speed"), 100),
        "ticker_fontsize": to_int(request.form.get("ticker_fontsize"), 24),
        "ticker_color": request.form.get("ticker_color", "white").strip() or "white",
        "ticker_bgcolor": request.form.get("ticker_bgcolor", "black@0.5").strip() or "black@0.5",
        "pip_enabled": 1 if to_bool(request.form.get("pip_enabled")) else 0,
        "pip_mode": request.form.get("pip_mode", "none").strip() or "none",
        "pip_source": request.form.get("pip_source", "").strip(),
        "pip_position": request.form.get("pip_position", "top-right").strip() or "top-right",
        "pip_width": to_int(request.form.get("pip_width"), 320),
        "pip_margin": to_int(request.form.get("pip_margin"), 16),
        "enabled": 1,
    }
    err = validate_channel_payload(data)
    if err:
        flash(err, "error")
        return redirect(url_for("channels.channels_page"))
    if not data.get("channel_number"):
        data["channel_number"] = next_free_number()
    try:
        cid = create_channel(data)
    except Exception as exc:  # noqa: BLE001
        flash(f"Ошибка создания канала: {exc}", "error")
        return redirect(url_for("channels.channels_page"))
    events.info("channels", f"created {source_type} channel {cid} '{data['name']}'")
    flash(f"Канал «{data['name']}» создан", "info")
    if source_type == "mosaic":
        flash("Добавьте камеры на странице редактирования канала", "info")
        return redirect(url_for("channels.channel_edit_form", cid=cid))
    return redirect(url_for("channels.channels_page"))


@bp.route("/channels/create-stream", methods=["POST"])
@login_required
def channels_create_stream():
    source_type = request.form.get("source_type", "camera").lower()
    if source_type not in ("iptv", "camera"):
        source_type = "camera"
    data = {
        "name": request.form.get("name", "").strip(),
        "source_type": source_type,
        "folder": None,
        "rotation": "random",
        "reshuffle_on_loop": 0,
        "file_extensions": "mp4,mkv,avi,ts,mov,m4v,webm",
        "codec_strategy": request.form.get("codec_strategy", "auto"),
        "bumper_enabled": 0,
        "bumper_duration_sec": 5,
        "stream_url": request.form.get("stream_url", "").strip(),
        "stream_user": request.form.get("stream_user", "").strip() or None,
        "stream_pass": request.form.get("stream_pass", "").strip() or None,
        "channel_number": to_int(request.form.get("channel_number"), None),
        "hls_enabled": 1 if to_bool(request.form.get("hls_enabled", "on")) else 0,
        "multicast_enabled": 1 if to_bool(request.form.get("multicast_enabled")) else 0,
        "multicast_addr": request.form.get("multicast_addr", "239.1.1.1").strip(),
        "multicast_port": to_int(request.form.get("multicast_port"), 5000),
        "preview_enabled": 0,
        "preview_lead_sec": 60,
        "ticker_enabled": 1 if to_bool(request.form.get("ticker_enabled")) else 0,
        "ticker_text": request.form.get("ticker_text", "").strip(),
        "ticker_speed": to_int(request.form.get("ticker_speed"), 100),
        "ticker_fontsize": to_int(request.form.get("ticker_fontsize"), 24),
        "ticker_color": request.form.get("ticker_color", "white").strip() or "white",
        "ticker_bgcolor": request.form.get("ticker_bgcolor", "black@0.5").strip() or "black@0.5",
        "pip_enabled": 1 if to_bool(request.form.get("pip_enabled")) else 0,
        "pip_mode": request.form.get("pip_mode", "none").strip() or "none",
        "pip_source": request.form.get("pip_source", "").strip(),
        "pip_position": request.form.get("pip_position", "top-right").strip() or "top-right",
        "pip_width": to_int(request.form.get("pip_width"), 320),
        "pip_margin": to_int(request.form.get("pip_margin"), 16),
        "enabled": 1,
    }
    err = validate_channel_payload(data)
    if err:
        flash(err, "error")
        return redirect(url_for("channels.channels_page"))
    if not data.get("channel_number"):
        data["channel_number"] = next_free_number()
    cid = create_channel(data)
    events.info("channels",
                f"created {source_type} channel {cid} '{data['name']}'")
    flash(f"Канал «{data['name']}» создан", "info")
    return redirect(url_for("channels.channels_page"))


@bp.route("/channels/<int:cid>/edit", methods=["GET"])
@login_required
def channel_edit_form(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    cams = []
    if ch.get("source_type") == "mosaic":
        cams = list_mosaic_sources(cid)
    return render_template("channel_edit.html",
                           channel=channel_view(ch),
                           mosaic_cameras=cams)


@bp.route("/channels/<int:cid>/pip/upload", methods=["POST"])
@login_required
def channels_pip_upload(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    f = request.files.get("pip_file")
    if not f or not f.filename:
        flash("Файл не выбран", "error")
        return redirect(url_for("channels.channel_edit_form", cid=cid))
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg"):
        flash("Только PNG или JPEG", "error")
        return redirect(url_for("channels.channel_edit_form", cid=cid))
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 5 * 1024 * 1024:
        flash("Файл больше 5 МБ", "error")
        return redirect(url_for("channels.channel_edit_form", cid=cid))
    pip_dir = DATA_DIR / "pip"
    pip_dir.mkdir(parents=True, exist_ok=True)
    target = pip_dir / f"{cid}.png"
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    f.save(str(target))
    events.info("pip", f"channel {cid}: pip image uploaded ({size} bytes)")
    flash("Картинка PiP загружена", "info")
    return redirect(url_for("channels.channel_edit_form", cid=cid))


@bp.route("/channels/<int:cid>/edit", methods=["POST"])
@login_required
def channels_edit(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    st = ch["source_type"]
    data = {
        "name": request.form.get("name", "").strip() or ch["name"],
        "rotation": request.form.get("rotation", ch["rotation"]),
        "reshuffle_on_loop": 1 if to_bool(request.form.get("reshuffle_on_loop")) else 0,
        "file_extensions": request.form.get("file_extensions", ch["file_extensions"]),
        "codec_strategy": request.form.get("codec_strategy", ch["codec_strategy"]),
        "bumper_enabled": 1 if to_bool(request.form.get("bumper_enabled")) else 0,
        "bumper_duration_sec": to_int(request.form.get("bumper_duration_sec"), ch["bumper_duration_sec"]),
        "channel_number": to_int(request.form.get("channel_number"),
                                  ch.get("channel_number")),
        "hls_enabled": 1 if to_bool(request.form.get("hls_enabled")) else 0,
        "multicast_enabled": 1 if to_bool(request.form.get("multicast_enabled")) else 0,
        "multicast_addr": request.form.get("multicast_addr", ch["multicast_addr"]).strip(),
        "multicast_port": to_int(request.form.get("multicast_port"), ch["multicast_port"]),
        "preview_enabled": 1 if to_bool(request.form.get("preview_enabled")) else 0,
        "preview_lead_sec": to_int(request.form.get("preview_lead_sec"), ch.get("preview_lead_sec", 60)),
        "ticker_enabled": 1 if to_bool(request.form.get("ticker_enabled")) else 0,
        "ticker_text": request.form.get("ticker_text", ch.get("ticker_text") or "").strip(),
        "ticker_speed": to_int(request.form.get("ticker_speed"), ch.get("ticker_speed", 100)),
        "ticker_fontsize": to_int(request.form.get("ticker_fontsize"), ch.get("ticker_fontsize", 24)),
        "ticker_color": request.form.get("ticker_color", ch.get("ticker_color") or "white").strip() or "white",
        "ticker_bgcolor": request.form.get("ticker_bgcolor",
                                           ch.get("ticker_bgcolor") or "black@0.5").strip() or "black@0.5",
        "pip_enabled": 1 if to_bool(request.form.get("pip_enabled")) else 0,
        "pip_mode": request.form.get("pip_mode", ch.get("pip_mode") or "none").strip() or "none",
        "pip_source": request.form.get("pip_source", ch.get("pip_source") or "").strip(),
        "pip_position": request.form.get("pip_position",
                                         ch.get("pip_position") or "top-right").strip() or "top-right",
        "pip_width": to_int(request.form.get("pip_width"), ch.get("pip_width", 320)),
        "pip_margin": to_int(request.form.get("pip_margin"), ch.get("pip_margin", 16)),
    }
    if st == "folder":
        data["folder"] = request.form.get("folder", "").strip() or ch["folder"]
    elif st in ("iptv", "camera"):
        data["stream_url"] = (request.form.get("stream_url", "").strip()
                              or ch["stream_url"])
        if request.form.get("stream_user") is not None:
            data["stream_user"] = request.form.get("stream_user", "").strip() or None
        if request.form.get("stream_pass"):
            data["stream_pass"] = request.form.get("stream_pass", "").strip() or None

    err = validate_channel_payload({**ch, **data}, exclude_id=cid)
    if err:
        flash(err, "error")
        return redirect(url_for("channels.channels_page"))
    was_running = supervisor.get_state(cid).get("running")
    if was_running:
        supervisor.stop(cid)
    update_channel(cid, data)
    if was_running:
        supervisor.start(get_channel(cid))
    flash(f"Канал «{data['name']}» обновлён", "info")
    return redirect(url_for("channels.channels_page"))


@bp.route("/channels/<int:cid>/delete", methods=["POST"])
@login_required
def channels_delete(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    supervisor.stop(cid)
    delete_channel(cid)
    # Убираем файлы канала, иначе HLS-сегменты копятся на диске.
    for sub, name in (("hls", str(cid)), ("ticker", f"{cid}.txt")):
        target = DATA_DIR / sub / name
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except OSError:
            pass
    events.info("channels", f"deleted channel {cid}")
    flash(f"Канал «{ch['name']}» удалён", "info")
    return redirect(url_for("channels.channels_page"))


@bp.route("/channels/<int:cid>/start", methods=["POST"])
@login_required
def channels_start(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    if supervisor.start(ch):
        events.info("channels", f"channel {cid} started")
        flash(f"Канал «{ch['name']}» запущен", "info")
    else:
        flash(f"Канал «{ch['name']}» уже работает", "warn")
    return redirect(url_for("channels.channels_page"))


@bp.route("/channels/<int:cid>/stop", methods=["POST"])
@login_required
def channels_stop(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    supervisor.stop(cid)
    events.info("channels", f"channel {cid} stopped")
    flash(f"Канал «{ch['name']}» остановлен", "info")
    return redirect(url_for("channels.channels_page"))


@bp.route("/channels/clear", methods=["POST"])
@login_required
def channels_clear():
    type_filter = (request.form.get("type") or "").strip().lower()

    all_channels = list_channels()
    if type_filter in ("folder", "iptv", "camera", "mosaic"):
        to_delete = [c for c in all_channels
                     if c.get("source_type") == type_filter]
    else:
        to_delete = all_channels

    if not to_delete:
        flash("Нечего удалять", "warn")
        return redirect(url_for("channels.channels_page"))

    removed_ids = []
    for ch in to_delete:
        cid = ch["id"]
        try:
            supervisor.stop(cid)
        except Exception:  # noqa: BLE001
            pass
        try:
            delete_channel(cid)
            removed_ids.append(cid)
        except Exception as exc:  # noqa: BLE001
            events.warn("channels",
                        f"clear: failed to delete channel {cid}: {exc}")
            continue

        hls_dir = DATA_DIR / "hls" / str(cid)
        if hls_dir.is_dir():
            shutil.rmtree(hls_dir, ignore_errors=True)

        pl = DATA_DIR / "playlists" / f"channel_{cid}.txt"
        if pl.exists():
            try:
                pl.unlink()
            except OSError:
                pass

    events.info("channels",
                f"cleared {len(removed_ids)} channels "
                f"(filter={type_filter or 'all'})")
    flash(f"Удалено {len(removed_ids)} канал(ов)", "info")
    return redirect(url_for("channels.channels_page"))
