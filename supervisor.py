"""Per-channel ffmpeg supervisor. One thread per channel,
one subprocess per enabled output."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from event_writer import events
from frozen_utils import APP_ROOT
from sources import FolderSource, MosaicSource, StreamSource
from db import set_channel_running


def _make_source(channel: dict):
    st = channel.get("source_type") or "folder"
    if st == "folder":
        return FolderSource(channel)
    if st in ("iptv", "camera"):
        return StreamSource(channel)
    if st == "mosaic":
        from db import list_mosaic_sources
        cams = list_mosaic_sources(channel["id"])
        return MosaicSource(channel, cams)
    raise ValueError(f"unknown source_type: {st}")


class ChannelRunner(threading.Thread):
    RESTART_DELAY = 3.0

    def __init__(self, channel: dict):
        super().__init__(daemon=True, name=f"channel-{channel['id']}")
        self.channel = channel
        self.stop_event = threading.Event()
        self._shutdown = False
        self.procs: dict[str, subprocess.Popen] = {}
        self.state: dict = {
            "running": False,
            "started_at": None,
            "current_file": None,
            "position_sec": 0,
            "error": None,
        }

    def stop(self) -> None:
        self.stop_event.set()

    def enabled_outputs(self) -> list[str]:
        outs = []
        if self.channel.get("hls_enabled"):
            outs.append("hls")
        if self.channel.get("multicast_enabled"):
            outs.append("multicast")
        return outs

    # ---------- thread body ----------
    def run(self) -> None:
        self.state["running"] = True
        self.state["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            while not self.stop_event.is_set():
                self._run_once()
                if self.stop_event.is_set():
                    break
                if not self._should_restart():
                    break
                events.warn("supervisor",
                            f"channel {self.channel['id']} crashed, "
                            f"restart in {self.RESTART_DELAY:.0f}s")
                if self.stop_event.wait(self.RESTART_DELAY):
                    break
        except Exception as exc:  # noqa: BLE001
            self.state["error"] = str(exc)
            events.error("supervisor",
                         f"channel {self.channel['id']} crashed: {exc}")
        finally:
            self._cleanup()
            if not self._shutdown:
                try:
                    set_channel_running(self.channel["id"], False)
                except Exception:  # noqa: BLE001
                    pass
            self.state["running"] = False
            events.info("supervisor", f"channel {self.channel['id']} stopped")

    def _should_restart(self) -> bool:
        return self.channel.get("source_type") in ("iptv", "camera")

    def _run_once(self) -> None:
        self.state["error"] = None  # reset on each attempt
        source = _make_source(self.channel)
        outputs = self.enabled_outputs()
        if not outputs:
            raise RuntimeError("no output enabled (hls/multicast)")

        for out in outputs:
            cmd = source.build_ffmpeg_cmd(out)
            events.info("supervisor",
                        f"channel {self.channel['id']} start output={out}",
                        cmd=" ".join(cmd[:8]) + " ...")
            env = os.environ.copy()
            env["PATH"] = str(APP_ROOT / "bin") + os.pathsep + env.get("PATH", "")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=env,
            )
            self.procs[out] = proc
            threading.Thread(
                target=self._read_progress, args=(out, proc), daemon=True,
            ).start()
            threading.Thread(
                target=self._read_stderr, args=(out, proc), daemon=True,
            ).start()

        # watchdog: warn if HLS doesn't appear within 15 s
        hls_dir = None
        if "hls" in self.procs:
            hls_dir = APP_ROOT / "data" / "hls" / str(self.channel["id"])

        def watchdog():
            if not hls_dir:
                return
            for _ in range(15):
                if self.stop_event.is_set():
                    return
                try:
                    if ((hls_dir / "index.m3u8").exists()
                            or any(hls_dir.glob("seg_*.ts"))):
                        return
                except OSError:
                    pass
                time.sleep(1)
            if not self.stop_event.is_set():
                self.state["error"] = (
                    "ffmpeg не создаёт HLS-сегменты за 15 сек — "
                    "проверьте /events"
                )
                events.warn(
                    "supervisor",
                    f"channel {self.channel['id']}: no HLS after 15s",
                )

        if hls_dir:
            threading.Thread(target=watchdog, daemon=True).start()

        try:
            while not self.stop_event.is_set():
                time.sleep(1.0)
                if not any(p.poll() is None for p in self.procs.values()):
                    return
        finally:
            self._cleanup()

    # ---------- helpers ----------
    def _read_progress(self, out: str, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    ms = int(line.split("=", 1)[1])
                    self.state["position_sec"] = ms // 1_000_000
                except ValueError:
                    pass

    def _read_stderr(self, out: str, proc: subprocess.Popen) -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            line = line.rstrip()
            if not line:
                continue
            low = line.lower()
            # current_file only for folder channels — streams have URLs, not files
            if (self.channel.get("source_type") == "folder"
                    and "opening" in low and "for reading" in low):
                name = line.split("'", 2)[-2] if "'" in line else line
                self.state["current_file"] = Path(name).name
            if any(w in low for w in ("error", "failed", "invalid")):
                events.warn("ffmpeg",
                            f"channel {self.channel['id']} [{out}]: {line[-300:]}")

    def _cleanup(self) -> None:
        for _name, p in list(self.procs.items()):
            if p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        self.procs.clear()


class Supervisor:
    def __init__(self) -> None:
        self.runners: dict[int, ChannelRunner] = {}
        self._lock = threading.Lock()

    def start(self, channel: dict) -> bool:
        cid = channel["id"]
        with self._lock:
            existing = self.runners.get(cid)
            if existing and existing.is_alive():
                return False

            # Resolve active ticker and prepare text file for drawtext:reload=1
            try:
                from db import get_active_ticker
                active_t = get_active_ticker(cid)
            except Exception as exc:  # noqa: BLE001
                events.error("supervisor", f"channel {cid} ticker resolve failed: {exc}")
                active_t = None

            if active_t:
                ticker_file = APP_ROOT / "data" / "ticker" / f"{cid}.txt"
                try:
                    ticker_file.parent.mkdir(parents=True, exist_ok=True)
                    ticker_file.write_text(active_t.get("text") or "", encoding="utf-8")
                    channel = {
                        **channel,
                        "_active_ticker": {**active_t, "_file": str(ticker_file)},
                    }
                except OSError as exc:  # noqa: BLE001
                    events.error("supervisor",
                                 f"channel {cid}: failed to write ticker file: {exc}")
                    channel = {**channel, "_active_ticker": None}
            else:
                channel = {**channel, "_active_ticker": None}

            runner = ChannelRunner(channel)
            self.runners[cid] = runner
            runner.start()
            try:
                set_channel_running(cid, True)
            except Exception:  # noqa: BLE001
                pass
            return True

    def stop(self, channel_id: int) -> bool:
        with self._lock:
            runner = self.runners.pop(channel_id, None)
        if not runner:
            return False
        runner.stop()
        runner.join(timeout=15)
        return True

    def get_state(self, channel_id: int) -> dict:
        runner = self.runners.get(channel_id)
        if runner:
            return dict(runner.state)
        return {"running": False}

    def list_states(self) -> dict[int, dict]:
        return {cid: dict(r.state) for cid, r in self.runners.items()}

    def shutdown_all(self) -> None:
        for cid in list(self.runners.keys()):
            runner = self.runners.get(cid)
            if runner:
                runner._shutdown = True
            self.stop(cid)


supervisor = Supervisor()