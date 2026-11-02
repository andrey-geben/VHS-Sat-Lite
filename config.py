"""Configuration manager for VHS-Sat.

Loads config.yaml, merges with defaults, provides ConfigManager singleton.
On first run generates a secret_key and persists it back to config.yaml.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import yaml


from frozen_utils import APP_ROOT

CONFIG_PATH = APP_ROOT / "config.yaml"


DEFAULTS: dict[str, Any] = {
    "server": {
        "title": "VHS-Sat",
        "host": "0.0.0.0",
        "port": 8300,
        "threads": 8,
        "debug": False,
        "secret_key": "",
    },
    "about": {
        "version": "0.3.0-dev",
        "author": "Andrew Genben",
        "contact": "",
        "repo_url": "",
        "description": (
            "Домашний стриминговый сервер: локальные каналы из папок, "
            "ретрансляция DVB-S/S2 (Linux), HLS и multicast MPEG-TS."
        ),
        "based_on": {
            "name": "VHS-Streamer",
            "url": "https://github.com/andrey-geben/VHS-Streamer",
            "note": "архитектурные паттерны: Flask-каркас, YAML-конфиг, "
                    "EventWriter, отдельный фоновый сервис",
        },
    },
    "viewer": {
        "enabled": True,
        "port": 8301,
        "public": True,
        "title": "Домашнее ТВ",
        "playlist_token": "",
    },
    "security": {
        "auth_enabled": True,
        "users": [],
    },
    "adapters": [],
    "ci_slots": [],
    "database": {"path": "data/vhs-sat.db"},
    "logging": {"events_file": "logs/events.jsonl", "level": "INFO"},
    "recordings": {"enabled": False, "directory": "recordings"},
    "hls": {"enabled": True, "directory": "data/hls", "public": True},
    "bumpers": {"directory": "data/bumpers"},
}


def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigManager:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        raw: dict[str, Any] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}
        self._data = deep_merge(DEFAULTS, raw)
        if not self._data["server"].get("secret_key"):
            self._data["server"]["secret_key"] = secrets.token_hex(32)
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(self._data, fh, allow_unicode=True, sort_keys=False)

    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def set(self, *keys: str, value: Any) -> None:
        *parents, last = keys
        node = self._data
        for key in parents:
            node = node.setdefault(key, {})
        node[last] = value
        self.save()

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def has_users(self) -> bool:
        return bool(self.get("security", "users"))

    # --- adapter helpers ---
    def upsert_adapter(self, adapter: dict) -> dict:
        cfg = self.get("adapters") or []
        for i, existing in enumerate(cfg):
            if existing["id"] == adapter["id"]:
                cfg[i] = {**existing, **adapter}
                self.set("adapters", value=cfg)
                return cfg[i]
        cfg.append(adapter)
        self.set("adapters", value=cfg)
        return adapter

    def sync_adapters(self, discovered: list[dict]) -> list[dict]:
        """Add any newly discovered adapter to config; keep existing entries."""
        for disc in discovered:
            existing = next(
                (a for a in (self.get("adapters") or []) if a["id"] == disc["id"]),
                None,
            )
            if existing is None:
                self.upsert_adapter({
                    "id": disc["id"],
                    "device": disc["device"],
                    "frontend": disc["frontend"],
                    "lnb": disc.get("lnb", {
                        "type": "universal", "low": 9750, "high": 10600,
                        "voltage": "off", "tone": "auto",
                    }),
                    "diseqc": disc.get("diseqc", {"version": "1.0", "port": 1}),
                })
        return self.get("adapters") or []


config = ConfigManager()