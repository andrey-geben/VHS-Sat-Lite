"""Authentication for VHS-Sat, modelled after VHS-Streamer auth.py.

Supports both plaintext (legacy / manual edits in config.yaml) and
Werkzeug-hashed passwords. /setup always writes hashed passwords.
"""
from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import (
    Blueprint, flash, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from config import config


auth_bp = Blueprint("auth", __name__)


HASH_PREFIXES = ("pbkdf2:", "scrypt:", "argon2")


def _check_password(stored: str, candidate: str) -> bool:
    if not stored:
        return False
    if stored.startswith(HASH_PREFIXES):
        return check_password_hash(stored, candidate)
    return stored == candidate


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def find_user(username: str) -> dict | None:
    for user in config.get("security", "users", default=[]) or []:
        if user.get("username") == username:
            return user
    return None


def authenticate(username: str, password: str) -> dict | None:
    user = find_user(username)
    if not user:
        return None
    if not _check_password(user.get("password", ""), password):
        return None
    return user


def current_user() -> dict | None:
    return session.get("user")


def login_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not config.get("security", "auth_enabled", default=True):
            return view(*args, **kwargs)
        if "user" in session:
            return view(*args, **kwargs)

        # GET — сохраняем next, чтобы вернуть пользователя куда он шёл
        if request.method == "GET":
            return redirect(url_for("auth.login", next=request.full_path))

        # POST/PUT/DELETE — данные формы всё равно потеряются при редиректе,
        # не пытаемся восстановить URL, честно просим заполнить заново
        flash("Сессия истекла — войдите и повторите действие", "warn")
        return redirect(url_for("auth.login"))

    return wrapped


def admin_required(view: Callable) -> Callable:
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not config.get("security", "auth_enabled", default=True):
            return view(*args, **kwargs)
        user = session.get("user")
        if not user or user.get("role") != "admin":
            flash("Требуются права администратора", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = authenticate(username, password)
        if user:
            session["user"] = {
                "username": user["username"],
                "role": user.get("role", "viewer"),
            }
            nxt = request.args.get("next") or url_for("dashboard")

            # open-redirect защита + не идём на URL с query-string вида POST
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("dashboard")

            return redirect(nxt)
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    session.pop("user", None)
    flash("Вы вышли из системы", "info")
    return redirect(url_for("auth.login"))