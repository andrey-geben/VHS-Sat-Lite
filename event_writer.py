"""JSONL event writer, modelled after VHS-Streamer's EventWriter."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import config


class EventWriter:
    def __init__(self, path: str | Path | None = None):
        from frozen_utils import resolve_path
        if path is None:
            path = config.get("logging", "events_file", default="logs/events.jsonl")
        self.path = resolve_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()

    _LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}

    def write(self, level: str, source: str, message: str, **extra: Any) -> None:
        try:
            min_level = self._LEVELS.get(
                str(config.get("logging", "level", default="INFO")).upper(), 20,
            )
        except Exception:  # noqa: BLE001
            min_level = 20
        if self._LEVELS.get(level.upper(), 20) < min_level:
            return  # suppressed by logging.level in config.yaml

        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": level.upper(),
            "source": source,
            "message": message,
        }
        if extra:
            record["extra"] = extra
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    def info(self, source: str, message: str, **kw: Any) -> None:
        self.write("INFO", source, message, **kw)

    def warn(self, source: str, message: str, **kw: Any) -> None:
        self.write("WARN", source, message, **kw)

    def error(self, source: str, message: str, **kw: Any) -> None:
        self.write("ERROR", source, message, **kw)

    def read_tail(self, limit: int = 200) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            with self.path.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()[-limit:]
        result = []
        for line in lines:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result
    def clear(self, by: str | None = None) -> int:
        """Truncate the events file. Returns number of lines removed.

        The file is truncated (not deleted) so any open handles keep working.
        Optionally logs a single marker line indicating who cleared it.
        """
        with self._lock:
            if not self.path.exists():
                return 0
            with self.path.open("rb") as fh:
                count = sum(1 for _ in fh)
            with self.path.open("w", encoding="utf-8") as fh:
                fh.write("")  # truncate
        if by:
            self.info("events", f"events cleared by {by}", removed=count)
        return count

events = EventWriter()