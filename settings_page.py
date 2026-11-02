"""Settings editor: /settings page + save handlers.

Registered as a blueprint in both server.py (full) and server_lite.py (lite).
Reads/writes config.yaml through ConfigManager. Fields marked `restart` in
the template require a manual server restart; everything else takes effect
immediately because the app reads config on each request.
"""
from __future__ import annotations

import secrets

from flask import Blueprint, flash, redirect, render_template, request, url_for

from auth import authenticate, current_user, hash_password, login_required
from config import config
from event_writer import events


settings_bp = Blueprint("settings", __name__)

VALID_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def _to_bool(v) -> bool:
    return str(v).lower() in ("1", "true", "on", "yes")


def _to_int(v, default=None):
    if v in (None, ""):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@settings_bp.route("/settings")
@login_required
def settings_page():
    return render_template(
        "settings.html",
        cfg_server=config.get("server", default={}),
        cfg_viewer=config.get("viewer", default={}),
        cfg_security=config.get("security", default={}),
        cfg_about=config.get("about", default={}),
        cfg_logging=config.get("logging", default={}),
    )


@settings_bp.route("/settings/regenerate-playlist-token", methods=["POST"])
@login_required
def regenerate_playlist_token():
    token = secrets.token_urlsafe(24)
    config.set("viewer", "playlist_token", value=token)
    events.info("settings", "playlist token regenerated")
    flash("Токен плейлиста обновлён. Не забудьте обновить URL на ТВ.", "info")
    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/settings/save", methods=["POST"])
@login_required
def settings_save():
    # --- server ---
    server_title = (request.form.get("server_title", "").strip()
                    or config.get("server", "title", default="VHS-Sat"))
    server_host = (request.form.get("server_host", "").strip()
                   or config.get("server", "host", default="0.0.0.0"))
    server_port = _to_int(request.form.get("server_port"),
                          config.get("server", "port", default=8300))
    server_threads = _to_int(request.form.get("server_threads"),
                             config.get("server", "threads", default=8))
    server_debug = _to_bool(request.form.get("server_debug"))

    # --- viewer ---
    viewer_title = (request.form.get("viewer_title", "").strip()
                    or config.get("viewer", "title", default="Домашнее ТВ"))
    viewer_port = _to_int(request.form.get("viewer_port"),
                          config.get("viewer", "port", default=8301))
    viewer_public = _to_bool(request.form.get("viewer_public"))
    viewer_enabled = _to_bool(request.form.get("viewer_enabled"))

    # playlist token — empty in form = no token (clear). If field absent from
    # form entirely, keep current value (defensive for old forms).
    if "playlist_token" in request.form:
        playlist_token = request.form.get("playlist_token", "").strip()
    else:
        playlist_token = config.get("viewer", "playlist_token", default="") or ""

    # --- security ---
    auth_enabled = _to_bool(request.form.get("auth_enabled"))

    # --- about ---
    about_version = (request.form.get("about_version", "").strip()
                     or config.get("about", "version", default=""))
    about_author = (request.form.get("about_author", "").strip()
                    or config.get("about", "author", default=""))
    about_contact = request.form.get("about_contact", "").strip()
    about_repo_url = request.form.get("about_repo_url", "").strip()
    about_description = (request.form.get("about_description", "").strip()
                         or config.get("about", "description", default=""))

    # --- logging ---
    log_level = (request.form.get("log_level", "").strip().upper()
                 or config.get("logging", "level", default="INFO"))
    if log_level not in VALID_LEVELS:
        log_level = "INFO"

    # --- validation ---
    errors = []
    if not (1 <= server_port <= 65535):
        errors.append("server.port вне диапазона 1..65535")
    if not (1 <= viewer_port <= 65535):
        errors.append("viewer.port вне диапазона 1..65535")
    if server_port == viewer_port:
        errors.append("server.port и viewer.port не могут совпадать")
    if not (1 <= server_threads <= 64):
        errors.append("server.threads вне диапазона 1..64")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("settings.settings_page"))

    # --- restart detection (compare before vs after) ---
    restart_fields: list[str] = []
    if (server_host != config.get("server", "host")
            or server_port != config.get("server", "port")):
        restart_fields.append("server.host/port")
    if server_debug != bool(config.get("server", "debug")):
        restart_fields.append("server.debug")
    if server_threads != config.get("server", "threads", default=8):
        restart_fields.append("server.threads")
    if (viewer_enabled != bool(config.get("viewer", "enabled"))
            or viewer_port != config.get("viewer", "port")):
        restart_fields.append("viewer.enabled/port")

    # --- persist ---
    config.set("server", "title", value=server_title)
    config.set("server", "host", value=server_host)
    config.set("server", "port", value=server_port)
    config.set("server", "threads", value=server_threads)
    config.set("server", "debug", value=server_debug)

    config.set("viewer", "title", value=viewer_title)
    config.set("viewer", "port", value=viewer_port)
    config.set("viewer", "public", value=viewer_public)
    config.set("viewer", "enabled", value=viewer_enabled)
    config.set("viewer", "playlist_token", value=playlist_token)

    config.set("security", "auth_enabled", value=auth_enabled)

    config.set("about", "version", value=about_version)
    config.set("about", "author", value=about_author)
    config.set("about", "contact", value=about_contact)
    config.set("about", "repo_url", value=about_repo_url)
    config.set("about", "description", value=about_description)

    config.set("logging", "level", value=log_level)

    events.info("settings", "general settings updated",
                restart_fields=restart_fields)

    if restart_fields:
        flash("Настройки сохранены. Требуется перезапуск сервера: "
              + ", ".join(restart_fields), "warn")
    else:
        flash("Настройки сохранены.", "info")
    return redirect(url_for("settings.settings_page"))


@settings_bp.route("/settings/password", methods=["POST"])
@login_required
def settings_password():
    user = current_user()
    if not user:
        return redirect(url_for("settings.settings_page"))

    old_pw = request.form.get("old_password", "")
    new_pw = request.form.get("new_password", "")
    new_pw2 = request.form.get("new_password2", "")

    if not authenticate(user["username"], old_pw):
        flash("Текущий пароль неверен", "error")
        return redirect(url_for("settings.settings_page"))
    if len(new_pw) < 6:
        flash("Новый пароль должен быть не короче 6 символов", "error")
        return redirect(url_for("settings.settings_page"))
    if new_pw != new_pw2:
        flash("Новые пароли не совпадают", "error")
        return redirect(url_for("settings.settings_page"))

    users = config.get("security", "users") or []
    for u in users:
        if u.get("username") == user["username"]:
            u["password"] = hash_password(new_pw)
            break
    config.set("security", "users", value=users)

    events.info("settings", f"password changed for {user['username']}")
    flash("Пароль изменён.", "info")
    return redirect(url_for("settings.settings_page"))