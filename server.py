"""VHS-Sat Lite — Windows-only build: локальные каналы, IPTV, камеры, мозаика.

Точка входа PyInstaller. Маршруты вынесены в пакет `routes/` (Flask blueprints),
общие помощники — в `helpers.py`, объекты времени выполнения — в `runtime.py`.

Production mode uses waitress WSGI.
"""
from __future__ import annotations

import platform
import threading
import webbrowser

from flask import Flask, flash, redirect, render_template, request, url_for

from auth import auth_bp, current_user, hash_password
from config import config
from csrf import csrf_protect, get_csrf_token
from db import init_db
from event_writer import events
from frozen_utils import APP_ROOT, RESOURCE_ROOT
from runtime import DATA_DIR
from settings_page import settings_bp

from routes.api import bp as api_bp
from routes.channels import bp as channels_bp
from routes.epg import bp as epg_bp
from routes.events import bp as events_bp
from routes.iptv import bp as iptv_bp
from routes.main import bp as main_bp
from routes.mosaic import bp as mosaic_bp
from routes.overlays import bp as overlays_bp
from routes.system import bp as system_bp


def create_lite_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(RESOURCE_ROOT / "templates"),
        static_folder=str(RESOURCE_ROOT / "static"),
    )
    app.config["SECRET_KEY"] = config.get("server", "secret_key")
    app.jinja_env.globals["csrf_token"] = get_csrf_token

    @app.template_filter("fromjson_safe")
    def _fromjson_safe(value):
        try:
            return json.loads(value or "{}")
        except (ValueError, TypeError):
            return {}

    init_db()

    for sub in ("hls", "bumpers", "playlists", "ticker", "pip"):
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)
    (APP_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    (APP_ROOT / "downloads").mkdir(parents=True, exist_ok=True)

    app.register_blueprint(auth_bp)
    app.register_blueprint(settings_bp)
    for bp in (main_bp, channels_bp, iptv_bp, overlays_bp, mosaic_bp,
               events_bp, epg_bp, system_bp, api_bp):
        app.register_blueprint(bp)

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
        allowed = {"setup", "static", "auth.login", "api.api_status"}
        if request.endpoint in allowed:
            return None
        if not config.has_users():
            return redirect(url_for("setup"))
        return None

    app.before_request(csrf_protect)

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        # Повторная первичная настройка запрещена: иначе любой в сети может
        # перезаписать администратора через POST /setup.
        if config.has_users():
            if request.method == "POST":
                flash("Первичная настройка уже выполнена", "warn")
            return redirect(url_for("main.dashboard"))
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
