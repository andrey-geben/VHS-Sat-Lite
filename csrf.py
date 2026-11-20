"""CSRF-защита админских форм."""
from __future__ import annotations

import secrets

from flask import abort, request, session

SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")
TOKEN_KEY = "_csrf_token"


def get_csrf_token() -> str:
    token = session.get(TOKEN_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[TOKEN_KEY] = token
    return token


def csrf_protect() -> None:
    if request.method in SAFE_METHODS:
        return
    if request.endpoint == "static":
        return
    sent = (
        request.form.get("csrf_token")
        or request.headers.get("X-CSRF-Token")
        or ""
    )
    expected = session.get(TOKEN_KEY) or ""
    if not expected or not sent or not secrets.compare_digest(sent, expected):
        abort(400, description="CSRF token missing or invalid")
