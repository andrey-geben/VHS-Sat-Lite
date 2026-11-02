"""VHS-Sat Lite — Windows-only build. Local folder channels + IPTV + cameras + mosaic.

No DVB, no CAM, no hardware probing. Everything else identical to the
full server: two ports (admin 8300, viewer 8301), SQLite, events,
ffmpeg-based HLS + multicast streaming, EPG for folder channels.

Production mode uses waitress WSGI. This is the PyInstaller entry point
for VHS-Sat-Lite.exe.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import threading
import webbrowser

from pathlib import Path

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, url_for,
)

from auth import auth_bp, current_user, hash_password, login_required
from channel_utils import enrich_channel
from config import config
from db import (
    add_mosaic_source, check_ticker_overlap, create_channel, create_schedule,
    create_ticker, delete_channel, delete_mosaic_source, delete_schedule,
    delete_ticker, find_channel_by_number, find_channels_by_folder,
    find_channels_by_stream_url, get_channel, get_default_ticker,
    get_mosaic_source, get_schedule, get_ticker, init_db, list_channels,
    list_mosaic_sources, list_schedules, list_tickers, move_mosaic_source,
    next_free_number, update_channel, update_mosaic_source, update_schedule,
    update_ticker,
)
from event_writer import events
from frozen_utils import APP_ROOT, RESOURCE_ROOT, detect_lite_tools
from iptv_parser import fetch_playlist, parse_m3u
from settings_page import settings_bp
from supervisor import supervisor


DATA_DIR = APP_ROOT / "data"


def _to_int(value, default=None):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value) -> bool:
    return str(value).lower() in ("1", "true", "on", "yes")


def _channel_view(ch: dict) -> dict:
    st = supervisor.get_state(ch["id"])
    base = {**ch, **st, "running": bool(st.get("running"))}
    return enrich_channel(base)


def _validate_channel_payload(data: dict, exclude_id: int | None = None) -> str | None:
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


def create_lite_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(RESOURCE_ROOT / "templates"),
        static_folder=str(RESOURCE_ROOT / "static"),
    )
    app.config["SECRET_KEY"] = config.get("server", "secret_key")

    @app.template_filter("fromjson_safe")
    def _fromjson_safe(value):
        try:
            return json.loads(value or "{}")
        except (ValueError, TypeError):
            return {}
    init_db()

    for sub in ("hls", "bumpers", "playlists", "ticker"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

    app.register_blueprint(auth_bp)
    app.register_blueprint(settings_bp)

    @app.context_processor
    def inject_globals():
        admin_port = config.get("server", "port", default=8300)
        viewer_port = config.get("viewer", "port", default=8301)
        viewer_enabled = config.get("viewer", "enabled", default=True)
        host = request.host.split(":")[0]
        playlist_token = (config.get("viewer", "playlist_token", default="") or "").strip()
        playlist_base = f"http://{host}:{viewer_port}/playlist.m3u"
        playlist_url = (
            f"{playlist_base}?token={playlist_token}" if playlist_token else playlist_base
        )
        return {
            "title": config.get("server", "title", default="VHS-Sat"),
            "current_user": current_user(),
            "backend_name": "lokalchanel (windows-lite)",
            "platform": platform.system(),
            "viewer_enabled": viewer_enabled,
            "viewer_port": viewer_port,
            "viewer_url": f"http://{host}:{viewer_port}/",
            "admin_port": admin_port,
            "admin_url": f"http://{host}:{admin_port}/",
            "playlist_url": playlist_url,
            "about": config.get("about", default={}),
            "edition": "lite",
        }

    @app.before_request
    def _force_setup():
        allowed = {"setup", "static", "auth.login", "api_status"}
        if request.endpoint in allowed:
            return None
        if not config.has_users():
            return redirect(url_for("setup"))
        return None

    # ---------- setup ----------
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if config.has_users() and request.method == "GET":
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            password2 = request.form.get("password2", "")
            if not username or len(password) < 6:
                flash("Имя пользователя обязательно, пароль ≥ 6 символов", "error")
                return render_template("setup.html"), 400
            if password != password2:
                flash("Пароли не совпадают", "error")
                return render_template("setup.html"), 400
            config.set("security", "users", value=[{
                "username": username,
                "password": hash_password(password),
                "role": "admin",
            }])
            events.info("setup", f"admin user '{username}' created")
            flash("Администратор создан. Войдите.", "info")
            return redirect(url_for("auth.login"))
        return render_template("setup.html")

    # ---------- dashboard (lite) ----------
    @app.route("/")
    @login_required
    def dashboard():
        channels = [_channel_view(c) for c in list_channels()]
        return render_template("dashboard_lite.html",
                               channels=channels, tools=detect_lite_tools())

    # ---------- channels: list ----------
    @app.route("/channels")
    @login_required
    def channels_page():
        from epg_utils import now_next

        all_view = []
        for ch in list_channels():
            cv = _channel_view(ch)
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

    # ---------- channels: create (choice + forms) ----------
    @app.route("/channels/new")
    @login_required
    def channels_new_choice():
        return render_template("channels_new.html")

    @app.route("/channels/new/<source_type>")
    @login_required
    def channel_new_form(source_type):
        if source_type not in ("folder", "iptv", "camera", "mosaic"):
            abort(404)
        return render_template("channel_new.html", form_type=source_type)

    @app.route("/channels/create", methods=["POST"])
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
            "reshuffle_on_loop": 1 if _to_bool(request.form.get("reshuffle_on_loop")) else 0,
            "file_extensions": request.form.get("file_extensions", "mp4,mkv,avi,ts,mov,m4v,webm"),
            "codec_strategy": request.form.get("codec_strategy", "auto"),
            "bumper_enabled": 1 if _to_bool(request.form.get("bumper_enabled")) else 0,
            "bumper_duration_sec": _to_int(request.form.get("bumper_duration_sec"), 5),
            "stream_url": None,
            "stream_user": None,
            "stream_pass": None,
            "channel_number": _to_int(request.form.get("channel_number"), None),
            "hls_enabled": 1 if _to_bool(request.form.get("hls_enabled", "on")) else 0,
            "multicast_enabled": 1 if _to_bool(request.form.get("multicast_enabled")) else 0,
            "multicast_addr": request.form.get("multicast_addr", "239.1.1.1").strip(),
            "multicast_port": _to_int(request.form.get("multicast_port"), 5000),
            "preview_enabled": 1 if _to_bool(request.form.get("preview_enabled")) else 0,
            "preview_lead_sec": _to_int(request.form.get("preview_lead_sec"), 60),
            "ticker_enabled": 1 if _to_bool(request.form.get("ticker_enabled")) else 0,
            "ticker_text": request.form.get("ticker_text", "").strip(),
            "ticker_speed": _to_int(request.form.get("ticker_speed"), 100),
            "ticker_fontsize": _to_int(request.form.get("ticker_fontsize"), 24),
            "ticker_color": request.form.get("ticker_color", "white").strip() or "white",
            "ticker_bgcolor": request.form.get("ticker_bgcolor", "black@0.5").strip() or "black@0.5",
            "pip_enabled": 1 if _to_bool(request.form.get("pip_enabled")) else 0,
            "pip_mode": request.form.get("pip_mode", "none").strip() or "none",
            "pip_source": request.form.get("pip_source", "").strip(),
            "pip_position": request.form.get("pip_position", "top-right").strip() or "top-right",
            "pip_width": _to_int(request.form.get("pip_width"), 320),
            "pip_margin": _to_int(request.form.get("pip_margin"), 16),
            "enabled": 1,
        }
        err = _validate_channel_payload(data)
        if err:
            flash(err, "error")
            return redirect(url_for("channels_page"))
        if not data.get("channel_number"):
            data["channel_number"] = next_free_number()
        try:
            cid = create_channel(data)
        except Exception as exc:  # noqa: BLE001
            flash(f"Ошибка создания канала: {exc}", "error")
            return redirect(url_for("channels_page"))
        events.info("channels", f"created {source_type} channel {cid} '{data['name']}'")
        flash(f"Канал «{data['name']}» создан", "info")
        if source_type == "mosaic":
            flash("Добавьте камеры на странице редактирования канала", "info")
            return redirect(url_for("channel_edit_form", cid=cid))
        return redirect(url_for("channels_page"))

    @app.route("/channels/create-stream", methods=["POST"])
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
            "channel_number": _to_int(request.form.get("channel_number"), None),
            "hls_enabled": 1 if _to_bool(request.form.get("hls_enabled", "on")) else 0,
            "multicast_enabled": 1 if _to_bool(request.form.get("multicast_enabled")) else 0,
            "multicast_addr": request.form.get("multicast_addr", "239.1.1.1").strip(),
            "multicast_port": _to_int(request.form.get("multicast_port"), 5000),
            "preview_enabled": 0,
            "preview_lead_sec": 60,
            "ticker_enabled": 1 if _to_bool(request.form.get("ticker_enabled")) else 0,
            "ticker_text": request.form.get("ticker_text", "").strip(),
            "ticker_speed": _to_int(request.form.get("ticker_speed"), 100),
            "ticker_fontsize": _to_int(request.form.get("ticker_fontsize"), 24),
            "ticker_color": request.form.get("ticker_color", "white").strip() or "white",
            "ticker_bgcolor": request.form.get("ticker_bgcolor", "black@0.5").strip() or "black@0.5",
            "pip_enabled": 1 if _to_bool(request.form.get("pip_enabled")) else 0,
            "pip_mode": request.form.get("pip_mode", "none").strip() or "none",
            "pip_source": request.form.get("pip_source", "").strip(),
            "pip_position": request.form.get("pip_position", "top-right").strip() or "top-right",
            "pip_width": _to_int(request.form.get("pip_width"), 320),
            "pip_margin": _to_int(request.form.get("pip_margin"), 16),
            "enabled": 1,
        }
        err = _validate_channel_payload(data)
        if err:
            flash(err, "error")
            return redirect(url_for("channels_page"))
        if not data.get("channel_number"):
            data["channel_number"] = next_free_number()
        cid = create_channel(data)
        events.info("channels",
                    f"created {source_type} channel {cid} '{data['name']}'")
        flash(f"Канал «{data['name']}» создан", "info")
        return redirect(url_for("channels_page"))

    # ---------- channels: IPTV import ----------
    @app.route("/channels/iptv/import", methods=["GET", "POST"])
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
                return redirect(url_for("channel_new_form", source_type="iptv"))

        if not playlist_text:
            flash("Укажите URL плейлиста или загрузите файл", "error")
            return redirect(url_for("channel_new_form", source_type="iptv"))

        entries = parse_m3u(playlist_text)
        if not entries:
            flash("В плейлисте не найдено ни одного канала", "warn")
            return redirect(url_for("channel_new_form", source_type="iptv"))

        groups = sorted({e["group"] for e in entries if e.get("group")})
        return render_template("iptv_import.html",
                               entries=entries, groups=groups, source_url=url)

    @app.route("/channels/iptv/bulk-import", methods=["POST"])
    @login_required
    def channels_iptv_bulk_import():
        names = request.form.getlist("name")
        urls = request.form.getlist("url")
        strategy = request.form.get("codec_strategy", "auto")
        hls = 1 if _to_bool(request.form.get("hls_enabled", "on")) else 0
        mcast = 1 if _to_bool(request.form.get("multicast_enabled")) else 0
        mcast_addr = request.form.get("multicast_addr", "239.1.1.1").strip()
        mcast_port = _to_int(request.form.get("multicast_port"), 5000)

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
            err = _validate_channel_payload(data)
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
        return redirect(url_for("channels_page"))

    # ---------- channels: edit ----------
    @app.route("/channels/<int:cid>/edit", methods=["GET"])
    @login_required
    def channel_edit_form(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        cams = []
        if ch.get("source_type") == "mosaic":
            cams = list_mosaic_sources(cid)
        return render_template("channel_edit.html",
                               channel=_channel_view(ch),
                               mosaic_cameras=cams)

    @app.route("/channels/<int:cid>/pip/upload", methods=["POST"])
    @login_required
    def channels_pip_upload(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        f = request.files.get("pip_file")
        if not f or not f.filename:
            flash("Файл не выбран", "error")
            return redirect(url_for("channel_edit_form", cid=cid))
        ext = f.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("png", "jpg", "jpeg"):
            flash("Только PNG или JPEG", "error")
            return redirect(url_for("channel_edit_form", cid=cid))
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        if size > 5 * 1024 * 1024:
            flash("Файл больше 5 МБ", "error")
            return redirect(url_for("channel_edit_form", cid=cid))
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
        return redirect(url_for("channel_edit_form", cid=cid))

    @app.route("/channels/<int:cid>/overlays", methods=["GET"])
    @login_required
    def channel_overlays_form(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        return render_template(
            "channel_overlays.html",
            channel=_channel_view(ch),
            schedules=list_schedules(cid),
            tickers=list_tickers(cid),
            default_ticker=get_default_ticker(cid),
        )

    @app.route("/channels/<int:cid>/overlays", methods=["POST"])
    @login_required
    def channel_overlays_save(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        data = {
            "pip_enabled": 1 if _to_bool(request.form.get("pip_enabled")) else 0,
            "pip_mode": request.form.get("pip_mode", ch.get("pip_mode") or "none").strip() or "none",
            "pip_position": request.form.get("pip_position", ch.get("pip_position") or "top-right").strip() or "top-right",
            "pip_width": _to_int(request.form.get("pip_width"), ch.get("pip_width", 320)),
            "pip_margin": _to_int(request.form.get("pip_margin"), ch.get("pip_margin", 16)),
        }
        was_running = supervisor.get_state(cid).get("running")
        if was_running:
            supervisor.stop(cid)
        update_channel(cid, data)
        if was_running:
            supervisor.start(get_channel(cid))
        flash("Настройки оверлеев сохранены", "info")
        return redirect(url_for("channel_overlays_form", cid=cid))

    def _parse_schedule_time(value: str) -> str | None:
        v = (value or "").strip()
        try:
            h, m = v.split(":")
            hi, mi = int(h), int(m)
            if 0 <= hi <= 23 and 0 <= mi <= 59:
                return f"{hi:02d}:{mi:02d}"
        except (ValueError, AttributeError):
            pass
        return None

    def _parse_schedule_days(form) -> str:
        days = [d for d in form.getlist("days") if d in ("1", "2", "3", "4", "5", "6", "7")]
        if not days:
            days = ["1", "2", "3", "4", "5", "6", "7"]
        return ",".join(sorted(set(days)))

    @app.route("/channels/<int:cid>/overlays/schedules/create", methods=["POST"])
    @login_required
    def channel_overlays_schedule_create(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        kind = (request.form.get("kind") or "").strip().lower()
        if kind not in ("ticker", "pip_image"):
            flash("Неизвестный тип расписания", "error")
            return redirect(url_for("channel_overlays_form", cid=cid))

        start = _parse_schedule_time(request.form.get("start_time"))
        end = _parse_schedule_time(request.form.get("end_time"))
        if not start or not end:
            flash("Время должно быть в формате HH:MM", "error")
            return redirect(url_for("channel_overlays_form", cid=cid))

        days = _parse_schedule_days(request.form)
        priority = _to_int(request.form.get("priority"), 0)

        ticker_id: int | None = None
        payload: dict = {}
        if kind == "ticker":
            ticker_id = _to_int(request.form.get("ticker_id"), None)
            if not ticker_id:
                flash("Выберите бегущую строку для расписания", "error")
                return redirect(url_for("channel_overlays_form", cid=cid))
            t = get_ticker(ticker_id)
            if not t or t["channel_id"] != cid:
                flash("Выбранная бегущая строка не принадлежит этому каналу", "error")
                return redirect(url_for("channel_overlays_form", cid=cid))
            conflict = check_ticker_overlap(cid, start, end, days)
            if conflict:
                flash(
                    f"Пересечение с расписанием #{conflict['id']} "
                    f"({conflict['start_time']}–{conflict['end_time']}, days={conflict['days']})",
                    "error",
                )
                return redirect(url_for("channel_overlays_form", cid=cid))

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
        return redirect(url_for("channel_overlays_form", cid=cid))

    @app.route("/channels/<int:cid>/overlays/schedules/<int:sid>/toggle", methods=["POST"])
    @login_required
    def channel_overlays_schedule_toggle(cid, sid):
        ch = get_channel(cid)
        s = get_schedule(sid)
        if not ch or not s or s["channel_id"] != cid:
            abort(404)
        new_val = 0 if s.get("enabled") else 1
        update_schedule(sid, {"enabled": new_val})
        flash("Расписание " + ("включено" if new_val else "выключено"), "info")
        return redirect(url_for("channel_overlays_form", cid=cid))

    @app.route("/channels/<int:cid>/overlays/schedules/<int:sid>/delete", methods=["POST"])
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
        return redirect(url_for("channel_overlays_form", cid=cid))

    @app.route("/channels/<int:cid>/overlays/schedules/<int:sid>/upload", methods=["POST"])
    @login_required
    def channel_overlays_schedule_upload(cid, sid):
        ch = get_channel(cid)
        s = get_schedule(sid)
        if not ch or not s or s["channel_id"] != cid:
            abort(404)
        if s["kind"] != "pip_image":
            flash("Загрузка картинки доступна только для PiP-расписаний", "error")
            return redirect(url_for("channel_overlays_form", cid=cid))
        f = request.files.get("pip_file")
        if not f or not f.filename:
            flash("Файл не выбран", "error")
            return redirect(url_for("channel_overlays_form", cid=cid))
        ext = f.filename.rsplit(".", 1)[-1].lower()
        if ext not in ("png", "jpg", "jpeg"):
            flash("Только PNG или JPEG", "error")
            return redirect(url_for("channel_overlays_form", cid=cid))
        f.seek(0, 2)
        size = f.tell()
        f.seek(0)
        if size > 5 * 1024 * 1024:
            flash("Файл больше 5 МБ", "error")
            return redirect(url_for("channel_overlays_form", cid=cid))
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
        return redirect(url_for("channel_overlays_form", cid=cid))

    @app.route("/channels/<int:cid>/edit", methods=["POST"])
    @login_required
    def channels_edit(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        st = ch["source_type"]
        data = {
            "name": request.form.get("name", "").strip() or ch["name"],
            "rotation": request.form.get("rotation", ch["rotation"]),
            "reshuffle_on_loop": 1 if _to_bool(request.form.get("reshuffle_on_loop")) else 0,
            "file_extensions": request.form.get("file_extensions", ch["file_extensions"]),
            "codec_strategy": request.form.get("codec_strategy", ch["codec_strategy"]),
            "bumper_enabled": 1 if _to_bool(request.form.get("bumper_enabled")) else 0,
            "bumper_duration_sec": _to_int(request.form.get("bumper_duration_sec"), ch["bumper_duration_sec"]),
            "channel_number": _to_int(request.form.get("channel_number"),
                                      ch.get("channel_number")),
            "hls_enabled": 1 if _to_bool(request.form.get("hls_enabled")) else 0,
            "multicast_enabled": 1 if _to_bool(request.form.get("multicast_enabled")) else 0,
            "multicast_addr": request.form.get("multicast_addr", ch["multicast_addr"]).strip(),
            "multicast_port": _to_int(request.form.get("multicast_port"), ch["multicast_port"]),
            "preview_enabled": 1 if _to_bool(request.form.get("preview_enabled")) else 0,
            "preview_lead_sec": _to_int(request.form.get("preview_lead_sec"), ch.get("preview_lead_sec", 60)),
            "ticker_enabled": 1 if _to_bool(request.form.get("ticker_enabled")) else 0,
            "ticker_text": request.form.get("ticker_text", ch.get("ticker_text") or "").strip(),
            "ticker_speed": _to_int(request.form.get("ticker_speed"), ch.get("ticker_speed", 100)),
            "ticker_fontsize": _to_int(request.form.get("ticker_fontsize"), ch.get("ticker_fontsize", 24)),
            "ticker_color": request.form.get("ticker_color", ch.get("ticker_color") or "white").strip() or "white",
            "ticker_bgcolor": request.form.get("ticker_bgcolor",
                                               ch.get("ticker_bgcolor") or "black@0.5").strip() or "black@0.5",
            "pip_enabled": 1 if _to_bool(request.form.get("pip_enabled")) else 0,
            "pip_mode": request.form.get("pip_mode", ch.get("pip_mode") or "none").strip() or "none",
            "pip_source": request.form.get("pip_source", ch.get("pip_source") or "").strip(),
            "pip_position": request.form.get("pip_position",
                                             ch.get("pip_position") or "top-right").strip() or "top-right",
            "pip_width": _to_int(request.form.get("pip_width"), ch.get("pip_width", 320)),
            "pip_margin": _to_int(request.form.get("pip_margin"), ch.get("pip_margin", 16)),
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

        err = _validate_channel_payload({**ch, **data}, exclude_id=cid)
        if err:
            flash(err, "error")
            return redirect(url_for("channels_page"))
        was_running = supervisor.get_state(cid).get("running")
        if was_running:
            supervisor.stop(cid)
        update_channel(cid, data)
        if was_running:
            supervisor.start(get_channel(cid))
        flash(f"Канал «{data['name']}» обновлён", "info")
        return redirect(url_for("channels_page"))

    @app.route("/channels/<int:cid>/delete", methods=["POST"])
    @login_required
    def channels_delete(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        supervisor.stop(cid)
        delete_channel(cid)
        events.info("channels", f"deleted channel {cid}")
        flash(f"Канал «{ch['name']}» удалён", "info")
        return redirect(url_for("channels_page"))

    @app.route("/channels/<int:cid>/start", methods=["POST"])
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
        return redirect(url_for("channels_page"))

    @app.route("/channels/<int:cid>/stop", methods=["POST"])
    @login_required
    def channels_stop(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        supervisor.stop(cid)
        events.info("channels", f"channel {cid} stopped")
        flash(f"Канал «{ch['name']}» остановлен", "info")
        return redirect(url_for("channels_page"))

    @app.route("/channels/clear", methods=["POST"])
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
            return redirect(url_for("channels_page"))

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
        return redirect(url_for("channels_page"))

    # ---------- mosaic camera management ----------
    @app.route("/channels/<int:cid>/mosaic/add", methods=["POST"])
    @login_required
    def mosaic_add_camera(cid):
        ch = get_channel(cid)
        if not ch or ch.get("source_type") != "mosaic":
            abort(404)
        url = (request.form.get("stream_url") or "").strip()
        if not url.startswith(("http://", "https://", "rtsp://", "rtmp://")):
            flash("URL камеры должен начинаться с http://, https://, rtsp:// или rtmp://",
                  "error")
            return redirect(url_for("channel_edit_form", cid=cid))
        if len(list_mosaic_sources(cid)) >= 9:
            flash("Максимум 9 камер в мозаике", "error")
            return redirect(url_for("channel_edit_form", cid=cid))
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
        return redirect(url_for("channel_edit_form", cid=cid))

    @app.route("/channels/<int:cid>/mosaic/<int:sid>/update", methods=["POST"])
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
        return redirect(url_for("channel_edit_form", cid=cid))

    @app.route("/channels/<int:cid>/mosaic/<int:sid>/remove", methods=["POST"])
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
        return redirect(url_for("channel_edit_form", cid=cid))

    @app.route("/channels/<int:cid>/mosaic/<int:sid>/move/<direction>", methods=["POST"])
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
        return redirect(url_for("channel_edit_form", cid=cid))

    # ---------- events ----------
    @app.route("/events")
    @login_required
    def events_page():
        limit = int(request.args.get("limit", 200))
        return render_template("events.html", events=events.read_tail(limit))

    @app.route("/events/clear", methods=["POST"])
    @login_required
    def events_clear():
        by = (current_user() or {}).get("username")
        removed = events.clear(by=by)
        flash(f"Журнал очищен (удалено {removed} записей)", "info")
        return redirect(url_for("events_page"))

    # ---------- EPG ----------
    @app.route("/epg")
    @login_required
    def epg_page():
        from epg_utils import channel_schedule

        view = []
        for ch in list_channels():
            cv = _channel_view(ch)
            if ch.get("source_type") == "folder":
                sched = channel_schedule(
                    ch, supervisor.get_state(ch["id"]), horizon_min=180,
                )
            else:
                sched = []
            cv["schedule"] = sched
            view.append(cv)
        return render_template("epg.html", channels=view)

    # ---------- about ----------
    @app.route("/about")
    @login_required
    def about_page():
        return render_template("about_lite.html")

    # ---------- REST API ----------
    @app.route("/api/status")
    def api_status():
        return jsonify({
            "title": config.get("server", "title"),
            "backend": "lokalchanel (windows-lite)",
            "platform": platform.system(),
            "edition": "lite",
            "auth_enabled": config.get("security", "auth_enabled"),
            "users_configured": config.has_users(),
        })

    @app.route("/api/tools")
    @login_required
    def api_tools():
        return jsonify(detect_lite_tools())

    @app.route("/api/events")
    @login_required
    def api_events():
        limit = int(request.args.get("limit", 100))
        return jsonify(events.read_tail(limit))

    @app.route("/api/channels", methods=["GET"])
    @login_required
    def api_channels():
        return jsonify([_channel_view(c) for c in list_channels()])

    @app.route("/api/channels", methods=["POST"])
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
        err = _validate_channel_payload(data)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        if not data.get("channel_number"):
            data["channel_number"] = next_free_number()
        cid = create_channel(data)
        return jsonify({"ok": True, "id": cid}), 201

    @app.route("/api/channels/<int:cid>", methods=["GET"])
    @login_required
    def api_channel_get(cid):
        ch = get_channel(cid)
        if not ch:
            return jsonify({"error": "not found"}), 404
        return jsonify(_channel_view(ch))

    @app.route("/api/channels/<int:cid>", methods=["PATCH"])
    @login_required
    def api_channel_patch(cid):
        ch = get_channel(cid)
        if not ch:
            return jsonify({"error": "not found"}), 404
        data = request.get_json(silent=True) or {}
        err = _validate_channel_payload({**ch, **data}, exclude_id=cid)
        if err:
            return jsonify({"ok": False, "error": err}), 400
        was_running = supervisor.get_state(cid).get("running")
        if was_running:
            supervisor.stop(cid)
        update_channel(cid, data)
        if was_running:
            supervisor.start(get_channel(cid))
        return jsonify({"ok": True, "channel": _channel_view(get_channel(cid))})

    @app.route("/api/channels/<int:cid>", methods=["DELETE"])
    @login_required
    def api_channel_delete(cid):
        if not get_channel(cid):
            return jsonify({"error": "not found"}), 404
        supervisor.stop(cid)
        delete_channel(cid)
        return jsonify({"ok": True})

    @app.route("/api/channels/<int:cid>/start", methods=["POST"])
    @login_required
    def api_channel_start(cid):
        ch = get_channel(cid)
        if not ch:
            return jsonify({"error": "not found"}), 404
        started = supervisor.start(ch)
        return jsonify({"ok": started, "state": supervisor.get_state(cid)})

    @app.route("/api/channels/<int:cid>/stop", methods=["POST"])
    @login_required
    def api_channel_stop(cid):
        if not get_channel(cid):
            return jsonify({"error": "not found"}), 404
        stopped = supervisor.stop(cid)
        return jsonify({"ok": stopped, "state": supervisor.get_state(cid)})

    @app.route("/api/channels/<int:cid>/state")
    @login_required
    def api_channel_state(cid):
        if not get_channel(cid):
            return jsonify({"error": "not found"}), 404
        return jsonify(supervisor.get_state(cid))

    @app.route("/channels/<int:cid>/overlays/tickers/create", methods=["POST"])
    @login_required
    def channel_ticker_create(cid):
        ch = get_channel(cid)
        if not ch:
            abort(404)
        name = (request.form.get("name") or "").strip() or "Ticker"
        text = (request.form.get("text") or "").strip()
        if not text:
            flash("Текст бегущей строки не может быть пустым", "error")
            return redirect(url_for("channel_overlays_form", cid=cid))
        is_default = 1 if _to_bool(request.form.get("is_default")) else 0
        if is_default and get_default_ticker(cid):
            flash("У канала уже есть ticker по умолчанию", "error")
            return redirect(url_for("channel_overlays_form", cid=cid))
        tid = create_ticker(cid, {
            "name": name,
            "text": text,
            "speed": _to_int(request.form.get("speed"), 100),
            "fontsize": _to_int(request.form.get("fontsize"), 24),
            "color": (request.form.get("color") or "white").strip() or "white",
            "bgcolor": (request.form.get("bgcolor") or "black@0.5").strip() or "black@0.5",
            "is_default": is_default,
        })
        events.info("overlays", f"channel {cid}: ticker {tid} '{name}' created")
        flash("Бегущая строка добавлена", "info")
        return redirect(url_for("channel_overlays_form", cid=cid))

    @app.route("/channels/<int:cid>/overlays/tickers/<int:tid>/update", methods=["POST"])
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
                return redirect(url_for("channel_overlays_form", cid=cid))
            data["text"] = new_text
        for field, default in (("speed", 100), ("fontsize", 24)):
            if request.form.get(field) is not None:
                data[field] = _to_int(request.form.get(field), default)
        for field, default in (("color", "white"), ("bgcolor", "black@0.5")):
            if request.form.get(field) is not None:
                data[field] = (request.form.get(field) or default).strip() or default
        update_ticker(tid, data)
        flash("Бегущая строка обновлена", "info")
        return redirect(url_for("channel_overlays_form", cid=cid))

    @app.route("/channels/<int:cid>/overlays/tickers/<int:tid>/delete", methods=["POST"])
    @login_required
    def channel_ticker_delete(cid, tid):
        ch = get_channel(cid)
        t = get_ticker(tid)
        if not ch or not t or t["channel_id"] != cid:
            abort(404)
        delete_ticker(tid)
        events.info("overlays", f"channel {cid}: ticker {tid} deleted (with schedules)")
        flash("Бегущая строка удалена (вместе с её расписаниями)", "info")
        return redirect(url_for("channel_overlays_form", cid=cid))

    return app


def _run_viewer() -> None:
    try:
        from viewer import create_viewer_app

        port = int(config.get("viewer", "port", default=8301))
        host = config.get("server", "host", default="0.0.0.0")
        threads = int(config.get("server", "threads", default=8))
        viewer_app = create_viewer_app()

        try:
            from waitress import serve
            events.info("viewer", f"waitress viewer on {host}:{port} (threads={threads})")
            serve(viewer_app, host=host, port=port, threads=threads,
                  ident="VHS-Sat-viewer", clear_untrusted_proxy_headers=True)
        except ImportError:
            from werkzeug.serving import make_server
            events.warn("viewer", "waitress not installed — falling back to werkzeug")
            server = make_server(host, port, viewer_app, threaded=True)
            server.serve_forever()
    except Exception as exc:  # noqa: BLE001
        events.error("viewer", f"viewer failed to start: {exc}")
        print(f" * FATAL: viewer (port 8301) failed to start: {exc}")


app = create_lite_app()


def main() -> None:
    import atexit
    from scheduler import start_scheduler, shutdown_scheduler
    atexit.register(supervisor.shutdown_all)
    atexit.register(shutdown_scheduler)

    def _autostart():
        import time
        time.sleep(3)
        try:
            from db import get_running_channel_ids, get_channel
            ids = get_running_channel_ids()
            if not ids:
                return
            events.info("supervisor", f"auto-starting {len(ids)} channel(s) after restart")
            for cid in ids:
                ch = get_channel(cid)
                if ch:
                    supervisor.start(ch)
        except Exception as exc:  # noqa: BLE001
            events.error("supervisor", f"autostart failed: {exc}")

    threading.Thread(target=_autostart, daemon=True, name="autostart").start()
    start_scheduler(supervisor)

    events.info("server", f"VHS-Sat Lite starting (platform={platform.system()})")

    if config.get("viewer", "enabled", default=True):
        threading.Thread(
            target=_run_viewer, daemon=True, name="viewer-server",
        ).start()

    admin_port = int(config.get("server", "port", default=8300))
    host = config.get("server", "host", default="0.0.0.0")
    threads = int(config.get("server", "threads", default=8))

    if config.get("server", "open_browser_on_start", default=True):
        threading.Timer(
            1.5, lambda: webbrowser.open(f"http://localhost:{admin_port}/"),
        ).start()

    try:
        from waitress import serve
        events.info("server", f"waitress admin on {host}:{admin_port} (threads={threads})")
        print(f" * Production server (waitress) on http://{host}:{admin_port}")
        print(f" * Threads: {threads}")
        print(" * Press CTRL+C to quit")
        serve(app, host=host, port=admin_port, threads=threads,
              ident="VHS-Sat-Lite", clear_untrusted_proxy_headers=True)
    except ImportError:
        events.warn("server", "waitress not installed — falling back to Flask dev server")
        print(" * WARNING: waitress not installed, using Flask dev server")
        app.run(host=host, port=admin_port, debug=False, threaded=True)


if __name__ == "__main__":
    main()