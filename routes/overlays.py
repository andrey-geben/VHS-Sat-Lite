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

bp = Blueprint("overlays", __name__)


@bp.route("/channels/<int:cid>/overlays", methods=["GET"])
@login_required
def channel_overlays_form(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    return render_template(
        "channel_overlays.html",
        channel=channel_view(ch),
        schedules=list_schedules(cid),
        tickers=list_tickers(cid),
        default_ticker=get_default_ticker(cid),
    )


@bp.route("/channels/<int:cid>/overlays", methods=["POST"])
@login_required
def channel_overlays_save(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    data = {
        "pip_enabled": 1 if to_bool(request.form.get("pip_enabled")) else 0,
        "pip_mode": request.form.get("pip_mode", ch.get("pip_mode") or "none").strip() or "none",
        "pip_position": request.form.get("pip_position", ch.get("pip_position") or "top-right").strip() or "top-right",
        "pip_width": to_int(request.form.get("pip_width"), ch.get("pip_width", 320)),
        "pip_margin": to_int(request.form.get("pip_margin"), ch.get("pip_margin", 16)),
    }
    was_running = supervisor.get_state(cid).get("running")
    if was_running:
        supervisor.stop(cid)
    update_channel(cid, data)
    if was_running:
        supervisor.start(get_channel(cid))
    flash("Настройки оверлеев сохранены", "info")
    return redirect(url_for("overlays.channel_overlays_form", cid=cid))


@bp.route("/channels/<int:cid>/overlays/schedules/create", methods=["POST"])
@login_required
def channel_overlays_schedule_create(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    kind = (request.form.get("kind") or "").strip().lower()
    if kind not in ("ticker", "pip_image"):
        flash("Неизвестный тип расписания", "error")
        return redirect(url_for("overlays.channel_overlays_form", cid=cid))

    start = parse_schedule_time(request.form.get("start_time"))
    end = parse_schedule_time(request.form.get("end_time"))
    if not start or not end:
        flash("Время должно быть в формате HH:MM", "error")
        return redirect(url_for("overlays.channel_overlays_form", cid=cid))

    days = parse_schedule_days(request.form)
    priority = to_int(request.form.get("priority"), 0)

    ticker_id: int | None = None
    payload: dict = {}
    if kind == "ticker":
        ticker_id = to_int(request.form.get("ticker_id"), None)
        if not ticker_id:
            flash("Выберите бегущую строку для расписания", "error")
            return redirect(url_for("overlays.channel_overlays_form", cid=cid))
        t = get_ticker(ticker_id)
        if not t or t["channel_id"] != cid:
            flash("Выбранная бегущая строка не принадлежит этому каналу", "error")
            return redirect(url_for("overlays.channel_overlays_form", cid=cid))
        conflict = check_ticker_overlap(cid, start, end, days)
        if conflict:
            flash(
                f"Пересечение с расписанием #{conflict['id']} "
                f"({conflict['start_time']}–{conflict['end_time']}, days={conflict['days']})",
                "error",
            )
            return redirect(url_for("overlays.channel_overlays_form", cid=cid))

    sid = create_schedule(cid, {
        "kind": kind,
        "start_time": start,
        "end_time": end,
        "days": days,
        "payload": json.dumps(payload, ensure_ascii=False),
        "priority": priority,
        "enabled": 1,
        "ticker_id": ticker_id,
    })

    events.info("overlays", f"channel {cid}: schedule {sid} ({kind}) created")
    flash("Расписание добавлено", "info")
    return redirect(url_for("overlays.channel_overlays_form", cid=cid))


@bp.route("/channels/<int:cid>/overlays/schedules/<int:sid>/toggle", methods=["POST"])
@login_required
def channel_overlays_schedule_toggle(cid, sid):
    ch = get_channel(cid)
    s = get_schedule(sid)
    if not ch or not s or s["channel_id"] != cid:
        abort(404)
    new_val = 0 if s.get("enabled") else 1
    update_schedule(sid, {"enabled": new_val})
    flash("Расписание " + ("включено" if new_val else "выключено"), "info")
    return redirect(url_for("overlays.channel_overlays_form", cid=cid))


@bp.route("/channels/<int:cid>/overlays/schedules/<int:sid>/delete", methods=["POST"])
@login_required
def channel_overlays_schedule_delete(cid, sid):
    ch = get_channel(cid)
    s = get_schedule(sid)
    if not ch or not s or s["channel_id"] != cid:
        abort(404)
    # delete associated pip image if any
    if s["kind"] == "pip_image":
        try:
            payload = json.loads(s.get("payload") or "{}")
        except (ValueError, TypeError):
            payload = {}
        fname = payload.get("file")
        if fname:
            target = DATA_DIR / "pip" / fname
            try:
                if target.is_file():
                    target.unlink()
            except OSError:
                pass
    delete_schedule(sid)
    events.info("overlays", f"channel {cid}: schedule {sid} deleted")
    flash("Расписание удалено", "info")
    return redirect(url_for("overlays.channel_overlays_form", cid=cid))


@bp.route("/channels/<int:cid>/overlays/schedules/<int:sid>/upload", methods=["POST"])
@login_required
def channel_overlays_schedule_upload(cid, sid):
    ch = get_channel(cid)
    s = get_schedule(sid)
    if not ch or not s or s["channel_id"] != cid:
        abort(404)
    if s["kind"] != "pip_image":
        flash("Загрузка картинки доступна только для PiP-расписаний", "error")
        return redirect(url_for("overlays.channel_overlays_form", cid=cid))
    f = request.files.get("pip_file")
    if not f or not f.filename:
        flash("Файл не выбран", "error")
        return redirect(url_for("overlays.channel_overlays_form", cid=cid))
    ext = f.filename.rsplit(".", 1)[-1].lower()
    if ext not in ("png", "jpg", "jpeg"):
        flash("Только PNG или JPEG", "error")
        return redirect(url_for("overlays.channel_overlays_form", cid=cid))
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    if size > 5 * 1024 * 1024:
        flash("Файл больше 5 МБ", "error")
        return redirect(url_for("overlays.channel_overlays_form", cid=cid))
    pip_dir = DATA_DIR / "pip"
    pip_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{cid}_{sid}.png"
    target = pip_dir / fname
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    f.save(str(target))
    try:
        old_payload = json.loads(s.get("payload") or "{}")
    except (ValueError, TypeError):
        old_payload = {}
    old_payload["file"] = fname
    update_schedule(sid, {"payload": json.dumps(old_payload, ensure_ascii=False)})
    events.info("overlays",
                f"channel {cid}: schedule {sid} pip image uploaded ({size} bytes)")
    flash("Картинка PiP для расписания загружена", "info")
    return redirect(url_for("overlays.channel_overlays_form", cid=cid))


@bp.route("/channels/<int:cid>/overlays/tickers/create", methods=["POST"])
@login_required
def channel_ticker_create(cid):
    ch = get_channel(cid)
    if not ch:
        abort(404)
    name = (request.form.get("name") or "").strip() or "Ticker"
    text = (request.form.get("text") or "").strip()
    if not text:
        flash("Текст бегущей строки не может быть пустым", "error")
        return redirect(url_for("overlays.channel_overlays_form", cid=cid))
    is_default = 1 if to_bool(request.form.get("is_default")) else 0
    if is_default and get_default_ticker(cid):
        flash("У канала уже есть ticker по умолчанию", "error")
        return redirect(url_for("overlays.channel_overlays_form", cid=cid))
    tid = create_ticker(cid, {
        "name": name,
        "text": text,
        "speed": to_int(request.form.get("speed"), 100),
        "fontsize": to_int(request.form.get("fontsize"), 24),
        "color": (request.form.get("color") or "white").strip() or "white",
        "bgcolor": (request.form.get("bgcolor") or "black@0.5").strip() or "black@0.5",
        "is_default": is_default,
    })
    events.info("overlays", f"channel {cid}: ticker {tid} '{name}' created")
    flash("Бегущая строка добавлена", "info")
    return redirect(url_for("overlays.channel_overlays_form", cid=cid))


@bp.route("/channels/<int:cid>/overlays/tickers/<int:tid>/update", methods=["POST"])
@login_required
def channel_ticker_update(cid, tid):
    ch = get_channel(cid)
    t = get_ticker(tid)
    if not ch or not t or t["channel_id"] != cid:
        abort(404)
    data: dict = {}
    if request.form.get("name") is not None:
        data["name"] = (request.form.get("name") or "").strip() or "Ticker"
    if request.form.get("text") is not None:
        new_text = (request.form.get("text") or "").strip()
        if not new_text:
            flash("Текст не может быть пустым", "error")
            return redirect(url_for("overlays.channel_overlays_form", cid=cid))
        data["text"] = new_text
    for field, default in (("speed", 100), ("fontsize", 24)):
        if request.form.get(field) is not None:
            data[field] = to_int(request.form.get(field), default)
    for field, default in (("color", "white"), ("bgcolor", "black@0.5")):
        if request.form.get(field) is not None:
            data[field] = (request.form.get(field) or default).strip() or default
    update_ticker(tid, data)
    flash("Бегущая строка обновлена", "info")
    return redirect(url_for("overlays.channel_overlays_form", cid=cid))


@bp.route("/channels/<int:cid>/overlays/tickers/<int:tid>/delete", methods=["POST"])
@login_required
def channel_ticker_delete(cid, tid):
    ch = get_channel(cid)
    t = get_ticker(tid)
    if not ch or not t or t["channel_id"] != cid:
        abort(404)
    delete_ticker(tid)
    events.info("overlays", f"channel {cid}: ticker {tid} deleted (with schedules)")
    flash("Бегущая строка удалена (вместе с её расписаниями)", "info")
    return redirect(url_for("overlays.channel_overlays_form", cid=cid))
