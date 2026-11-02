"""VHS-Sat viewer app — runs on a separate port (default 8301).

Public by default (home LAN). Set viewer.public=false to require auth;
the session cookie is shared with the admin app because both use the
same SECRET_KEY (cookies are port-agnostic on the same host).

Serves /playlist.m3u(.m3u8) — a live M3U playlist of all running
channels. If viewer.playlist_token is set, requires ?token=XXX.

Serves /download — landing page for the Android TV APK with a QR code
and installation instructions. The APK is read from downloads/*.apk
(newest wins) at the project root (or next to the .exe in frozen mode).
"""
from __future__ import annotations

import base64
import hmac
import io
from pathlib import Path

from flask import (
    Flask, abort, jsonify, redirect, render_template, request,
    Response, send_from_directory, session, url_for,
)

from config import config
from db import get_channel, list_channels
from event_writer import events
from frozen_utils import APP_ROOT, RESOURCE_ROOT
from supervisor import supervisor


DATA_DIR = APP_ROOT / "data"
DOWNLOAD_DIR = APP_ROOT / "downloads"

GROUP_TITLES = {
    "folder": "Папки",
    "iptv": "IPTV",
    "camera": "Камеры",
    "mosaic": "Мозаики",
}

APK_MIME = "application/vnd.android.package-archive"


def _channel_view(ch: dict) -> dict:
    st = supervisor.get_state(ch["id"])
    return {**ch, **st, "running": bool(st.get("running"))}


def _admin_base() -> str:
    admin_port = config.get("server", "port", default=8300)
    host = request.host.split(":")[0]
    return f"http://{host}:{admin_port}"


def _auth_redirect():
    if config.get("viewer", "public", default=True):
        return None
    if session.get("user"):
        return None
    target = f"{_admin_base()}/login?next={request.url}"
    return redirect(target)


def _playlist_url_for(ch: dict, host: str, viewer_port: int, prefer: str) -> str | None:
    has_hls = bool(ch.get("hls_enabled"))
    has_mcast = bool(ch.get("multicast_enabled") and ch.get("multicast_addr"))

    hls_url = f"http://{host}:{viewer_port}/hls/{ch['id']}/index.m3u8"
    mcast_url = (f"udp://@{ch['multicast_addr']}:{ch['multicast_port']}"
                 if has_mcast else None)

    if prefer == "multicast":
        return mcast_url or (hls_url if has_hls else None)
    return hls_url if has_hls else mcast_url


def _generate_m3u(channels: list[dict], host: str, viewer_port: int, prefer: str) -> str:
    lines: list[str] = ["#EXTM3U"]
    for ch in channels:
        url = _playlist_url_for(ch, host, viewer_port, prefer)
        if not url:
            continue
        num = ch.get("channel_number") or 0
        name = (ch.get("name") or f"Channel {ch['id']}").strip()
        group = GROUP_TITLES.get(ch.get("source_type") or "", "Другое")
        lines.append(
            f'#EXTINF:-1 tvg-chno="{num}" tvg-name="{name}" '
            f'group-title="{group}",{name}'
        )
        lines.append(url)
    return "\n".join(lines) + "\n"


def _latest_apk() -> Path | None:
    if not DOWNLOAD_DIR.is_dir():
        return None
    apks = [p for p in DOWNLOAD_DIR.glob("*.apk") if p.is_file()]
    if not apks:
        return None
    apks.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return apks[0]


def _make_qr_data_uri(text: str) -> str | None:
    try:
        import qrcode
    except ImportError:
        events.warn("download", "qrcode not installed — QR unavailable")
        return None
    try:
        qr = qrcode.QRCode(
            version=None,
            box_size=10,
            border=2,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
        )
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        events.warn("download", f"QR generation failed: {exc}")
        return None


def create_viewer_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(RESOURCE_ROOT / "templates"),
        static_folder=str(RESOURCE_ROOT / "static"),
    )
    app.config["SECRET_KEY"] = config.get("server", "secret_key")

    @app.context_processor
    def inject():
        token_set = bool(config.get("viewer", "playlist_token", default="") or "")
        return {
            "title": config.get("viewer", "title", default="Домашнее ТВ"),
            "viewer_port": config.get("viewer", "port", default=8301),
            "admin_url": _admin_base(),
            "about": config.get("about", default={}),
            "playlist_public": not token_set,
            "has_apk": _latest_apk() is not None,
        }

    @app.before_request
    def _gate():
        # playlist and download must remain reachable for TV apps
        if request.endpoint in ("playlist_m3u", "download_page",
                                "download_apk", "download_file"):
            return None
        r = _auth_redirect()
        if r:
            return r
        return None

    @app.route("/")
    @app.route("/watch")
    def watch():
        channels = [_channel_view(c) for c in list_channels() if c.get("enabled")]
        return render_template("watch.html", channels=channels)

    @app.route("/watch/<int:cid>")
    def watch_player(cid):
        ch = get_channel(cid)
        if not ch or not ch.get("enabled"):
            abort(404)
        return render_template("watch_player.html", channel=_channel_view(ch))

    @app.route("/about")
    def about():
        return render_template("about_viewer.html")

    @app.route("/hls/<int:cid>/<path:filename>")
    def hls_file(cid, filename):
        hls_root = DATA_DIR / "hls" / str(cid)
        if not hls_root.is_dir():
            abort(404)
        return send_from_directory(hls_root, filename, mimetype=None)

    # ---------- playlist ----------
    @app.route("/playlist.m3u8")
    @app.route("/playlist.m3u")
    def playlist_m3u():
        required_token = config.get("viewer", "playlist_token", default="") or ""
        if required_token:
            provided = request.args.get("token", "")
            if not hmac.compare_digest(provided, required_token):
                abort(404)

        host = request.host.split(":")[0]
        viewer_port = config.get("viewer", "port", default=8301)
        prefer = (request.args.get("format") or "hls").lower()
        if prefer not in ("hls", "multicast"):
            prefer = "hls"
        type_filter = (request.args.get("type") or "").strip().lower()

        running: list[dict] = []
        for ch in list_channels():
            if not ch.get("enabled"):
                continue
            if not supervisor.get_state(ch["id"]).get("running"):
                continue
            if type_filter and ch.get("source_type") != type_filter:
                continue
            running.append(ch)

        body = _generate_m3u(running, host, viewer_port, prefer)
        events.info(
            "playlist",
            f"playlist served: {len(running)} channels, "
            f"format={prefer}, type={type_filter or 'all'}",
        )

        ext = (request.path.rsplit(".", 1)[-1] or "m3u").lower()
        if ext == "m3u8":
            mime, filename = "application/vnd.apple.mpegurl", "vhs-sat.m3u8"
        else:
            mime, filename = "audio/x-mpegurl", "vhs-sat.m3u"

        return Response(
            body,
            mimetype=mime,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Access-Control-Allow-Origin": "*",
            },
        )

    # ---------- android APK download ----------
    @app.route("/download")
    def download_page():
        apk = _latest_apk()
        host = request.host.split(":")[0]
        viewer_port = config.get("viewer", "port", default=8301)
        admin_port = config.get("server", "port", default=8300)
        download_url = f"http://{host}:{viewer_port}/download/apk"
        server_url = f"http://{host}:{viewer_port}"
        admin_url = f"http://{host}:{admin_port}"

        qr_data_uri = None
        if apk:
            qr_data_uri = _make_qr_data_uri(download_url)
            try:
                size_mb = round(apk.stat().st_size / 1024 / 1024, 1)
            except OSError:
                size_mb = 0
            events.info("download", f"APK page served: {apk.name} ({size_mb} MB)")
        else:
            size_mb = 0

        return render_template(
            "download.html",
            apk=apk,
            apk_size_mb=size_mb,
            download_url=download_url,
            server_url=server_url,
            admin_url=admin_url,
            qr_data_uri=qr_data_uri,
        )

    @app.route("/download/apk")
    def download_apk():
        apk = _latest_apk()
        if not apk:
            abort(404)
        events.info("download", f"APK served: {apk.name}")
        return send_from_directory(
            str(apk.parent),
            apk.name,
            mimetype=APK_MIME,
            as_attachment=True,
            download_name="vhs-sat-tv.apk",
        )

    @app.route("/download/file/<path:filename>")
    def download_file(filename):
        if not DOWNLOAD_DIR.is_dir():
            abort(404)
        # block path traversal
        target = (DOWNLOAD_DIR / filename).resolve()
        try:
            target.relative_to(DOWNLOAD_DIR.resolve())
        except ValueError:
            abort(404)
        if not target.is_file() or not target.name.lower().endswith(".apk"):
            abort(404)
        events.info("download", f"APK served by name: {target.name}")
        return send_from_directory(
            str(target.parent),
            target.name,
            mimetype=APK_MIME,
            as_attachment=True,
            download_name=target.name,
        )

    # ---------- API ----------
    @app.route("/api/channels")
    def api_channels():
        from epg_utils import now_next
        result = []
        for ch in list_channels():
            if not ch.get("enabled"):
                continue
            cv = _channel_view(ch)
            if ch.get("source_type") == "folder" and cv.get("running"):
                try:
                    cv["epg_now"], cv["epg_next"] = now_next(
                        ch, supervisor.get_state(ch["id"]),
                    )
                except Exception:  # noqa: BLE001
                    cv["epg_now"] = cv["epg_next"] = None
            result.append(cv)
        return jsonify(result)

    @app.route("/api/channels/<int:cid>")
    def api_channel(cid):
        ch = get_channel(cid)
        if not ch or not ch.get("enabled"):
            return jsonify({"error": "not found"}), 404
        return jsonify(_channel_view(ch))

    return app


def _log_startup():
    port = config.get("viewer", "port", default=8301)
    public = config.get("viewer", "public", default=True)
    token_set = bool(config.get("viewer", "playlist_token", default="") or "")
    apk = _latest_apk()
    events.info("viewer", f"viewer on http://0.0.0.0:{port} (public={public})")
    events.info(
        "viewer",
        f"playlist: http://0.0.0.0:{port}/playlist.m3u "
        f"(token {'set' if token_set else 'not set'})",
    )
    events.info(
        "viewer",
        f"apk download: http://0.0.0.0:{port}/download "
        f"({apk.name if apk else 'no APK in downloads/'})",
    )