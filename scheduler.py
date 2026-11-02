"""Overlay scheduler: periodically updates ticker files based on schedules.

Every TICK_INTERVAL seconds:
- For each running channel, resolve the currently active ticker
  (schedule -> default).
- If its text differs from what the runner knows, rewrite data/ticker/<cid>.txt.
- FFmpeg's drawtext with reload=1 picks up the new text without restart.
"""
from __future__ import annotations

import threading

from event_writer import events
from frozen_utils import APP_ROOT
from db import get_active_ticker


TICK_INTERVAL = 30.0


class OverlayScheduler(threading.Thread):
    def __init__(self, supervisor):
        super().__init__(daemon=True, name="overlay-scheduler")
        self.supervisor = supervisor
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        events.info("scheduler", f"overlay scheduler started (tick={TICK_INTERVAL:.0f}s)")
        while not self.stop_event.wait(TICK_INTERVAL):
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                events.error("scheduler", f"tick failed: {exc}")
        events.info("scheduler", "overlay scheduler stopped")

    def _tick(self) -> None:
        ticker_dir = APP_ROOT / "data" / "ticker"
        ticker_dir.mkdir(parents=True, exist_ok=True)
        for cid, runner in list(self.supervisor.runners.items()):
            if not runner.is_alive():
                continue
            try:
                active = get_active_ticker(cid)
            except Exception as exc:  # noqa: BLE001
                events.warn("scheduler", f"channel {cid}: get_active_ticker failed: {exc}")
                continue
            if not active:
                continue

            current = runner.channel.get("_active_ticker") or {}
            new_text = (active.get("text") or "").strip()
            old_text = (current.get("text") or "").strip()

            ticker_file = ticker_dir / f"{cid}.txt"
            try:
                if new_text != old_text or not ticker_file.is_file():
                    ticker_file.write_text(new_text, encoding="utf-8")
                    events.info(
                        "scheduler",
                        f"channel {cid}: ticker text updated ({len(new_text)} chars)",
                    )
                runner.channel["_active_ticker"] = {
                    **active,
                    "_file": str(ticker_file),
                }
            except OSError as exc:  # noqa: BLE001
                events.error("scheduler", f"channel {cid}: write ticker failed: {exc}")


_scheduler: OverlayScheduler | None = None


def start_scheduler(supervisor) -> OverlayScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = OverlayScheduler(supervisor)
        _scheduler.start()
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None