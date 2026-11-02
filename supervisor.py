"""Per-channel ffmpeg supervisor. One thread per channel,
one or two subprocess per enabled output (two when active pip_image schedule)."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from event_writer import events
from frozen_utils import APP_ROOT
from sources import FolderSource, MosaicSource, StreamSource
from db import set_channel_running


_PORT_RANGE = range(20000, 20101)


def _allocate_port() -> int:
    for p in _PORT_RANGE:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
            continue
    raise RuntimeError("no free ports in 20000-20100")


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


def _needs_two_process(channel: dict) -> bool:
    """Two-process mode only for IPTV/camera with an *active* pip_image schedule."""
    if channel.get("source_type") not in ("iptv", "camera"):
        return False
    try:
        from db import get_active_schedule
        sched = get_active_schedule(channel["id"], "pip_image")
        if not sched:
            return False
        payload = json.loads(sched.get("payload") or "{}")
        fname = payload.get("file")
        if not fname:
            return False
        if not (APP_ROOT / "data" / "pip" / fname).is_file():
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


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
            "mode": "single",
            "port": None,
            "active_pip_file": None,
        }
        self._preview_timeline: list[dict] | None = None
        self._last_preview: str = ""

    def stop(self) -> None:
        self.stop_event.set()

    def enabled_outputs(self) -> list[str]:
        outs = []
        if self.channel.get("hls_enabled"):
            outs.append("hls")
        if self.channel.get("multicast_enabled"):
            outs.append("multicast")
        return outs

    def _env(self) -> dict:
        env = os.environ.copy()
        env["PATH"] = str(APP_ROOT / "bin") + os.pathsep + env.get("PATH", "")
        return env

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
                st = self.channel.get("source_type")
                delay = 1.0 if st == "folder" else self.RESTART_DELAY
                events.warn("supervisor",
                            f"channel {self.channel['id']} ended, "
                            f"restart in {delay:.1f}s")
                if self.stop_event.wait(delay):
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
        st = self.channel.get("source_type")
        if st == "folder":
            return True  # always loop; FFmpeg exits at end of concat, supervisor restarts
        return st in ("iptv", "camera")

    def _run_once(self) -> None:
        self.state["error"] = None
        outputs = self.enabled_outputs()
        if not outputs:
            raise RuntimeError("no output enabled (hls/multicast)")

        channel = dict(self.channel)

        # Preview timeline (folder channels only, when enabled)
        if (channel.get("source_type") == "folder"
                and channel.get("preview_enabled")):
            try:
                from preview_utils import build_timeline
                self._preview_timeline = build_timeline(channel)
                if self._preview_timeline:
                    events.info("supervisor",
                                f"channel {channel['id']}: preview timeline built "
                                f"({len(self._preview_timeline)} files)")
            except Exception as exc:  # noqa: BLE001
                events.warn("supervisor",
                            f"channel {channel['id']}: preview timeline failed: {exc}")
                self._preview_timeline = None
        else:
            self._preview_timeline = None

        use_two = _needs_two_process(channel)

        if use_two:
            try:
                from db import get_active_schedule
                sched = get_active_schedule(channel["id"], "pip_image")
                if sched:
                    payload = json.loads(sched.get("payload") or "{}")
                    fname = payload.get("file")
                    if fname:
                        channel["_active_pip"] = {"file": fname}
            except Exception as exc:  # noqa: BLE001
                events.warn("supervisor",
                            f"channel {channel['id']}: active pip resolve failed: {exc}")

        if use_two and channel.get("_active_pip"):
            self.state["mode"] = "two"
            self.state["active_pip_file"] = channel["_active_pip"]["file"]
            self._run_two_process(channel, outputs)
        else:
            self.state["mode"] = "single"
            self.state["active_pip_file"] = None
            self._run_single_process(channel, outputs)

        hls_dir = None
        if "hls" in self.procs:
            hls_dir = APP_ROOT / "data" / "hls" / str(channel["id"])

            def watchdog():
                if not hls_dir:
                    return
                max_wait = 45 if channel.get("source_type") == "folder" else 15
                for _ in range(max_wait):
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
                        f"ffmpeg не создаёт HLS-сегменты за {max_wait} сек — "
                        "проверьте /events"
                    )
                    events.warn(
                        "supervisor",
                        f"channel {channel['id']}: no HLS after {max_wait}s",
                    )

        try:
            while not self.stop_event.is_set():
                time.sleep(1.0)
                if not any(p.poll() is None for p in self.procs.values()):
                    return
                # clear false-positive "no HLS" error once HLS actually appears
                if self.state.get("error") and hls_dir:
                    try:
                        if ((hls_dir / "index.m3u8").exists()
                                or any(hls_dir.glob("seg_*.ts"))):
                            self.state["error"] = None
                    except OSError:
                        pass
        finally:
            self._cleanup()

    def _run_single_process(self, channel: dict, outputs: list[str]) -> None:
        source = _make_source(channel)
        env = self._env()
        for out in outputs:
            if out == "hls":
                hls_dir = APP_ROOT / "data" / "hls" / str(channel["id"])
                hls_dir.mkdir(parents=True, exist_ok=True)
                # delete every file with retry (segments, .vtt, .tmp, index)
                for f in list(hls_dir.iterdir()):
                    if not f.is_file():
                        continue
                    for _ in range(5):
                        try:
                            f.unlink()
                            break
                        except OSError:
                            time.sleep(0.1)
            cmd = source.build_ffmpeg_cmd(out)
            events.info("supervisor",
                        f"channel {channel['id']} start output={out}",
                        cmd=" ".join(cmd[:8]) + " ...")
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

    def _run_two_process(self, channel: dict, outputs: list[str]) -> None:
        source = _make_source(channel)
        env = self._env()
        port = _allocate_port()
        self.state["port"] = port

        # --- FFmpeg #1: source -> TCP ---
        cmd1 = source.build_source_cmd(port)
        events.info("supervisor",
                    f"channel {channel['id']} start source on tcp:{port}",
                    cmd=" ".join(cmd1[:8]) + " ...")
        proc1 = subprocess.Popen(
            cmd1, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self.procs["source"] = proc1
        threading.Thread(
            target=self._read_stderr, args=("source", proc1), daemon=True,
        ).start()

        time.sleep(1.0)
        if proc1.poll() is not None:
            raise RuntimeError(
                f"FFmpeg #1 (source) exited immediately with code {proc1.returncode}"
            )

        # --- FFmpeg #2: TCP -> overlays -> output(s) ---
        for out in outputs:
            cmd2 = source.build_overlay_cmd(port, out)
            events.info("supervisor",
                        f"channel {channel['id']} start overlay output={out} "
                        f"(tcp:{port})",
                        cmd=" ".join(cmd2[:8]) + " ...")
            proc2 = subprocess.Popen(
                cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=env,
            )
            self.procs[out] = proc2
            threading.Thread(
                target=self._read_progress, args=(out, proc2), daemon=True,
            ).start()
            threading.Thread(
                target=self._read_stderr, args=(out, proc2), daemon=True,
            ).start()
    # ---------- helpers ----------
    def _read_progress(self, out: str, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line.startswith("out_time_ms="):
                continue
            try:
                ms = int(line.split("=", 1)[1])
            except ValueError:
                continue
            self.state["position_sec"] = ms // 1_000_000
            self._update_preview(self.state["position_sec"])

    def _update_preview(self, position_sec: int) -> None:
        if not self._preview_timeline:
            return
        try:
            from preview_utils import next_name_near_end
            lead = int(self.channel.get("preview_lead_sec") or 60)
            next_name = next_name_near_end(self._preview_timeline, position_sec, lead)
        except Exception:  # noqa: BLE001
            return
        text = f"Далее: {next_name}" if next_name else ""
        if text == self._last_preview:
            return
        self._last_preview = text
        preview_dir = APP_ROOT / "data" / "preview"
        preview_dir.mkdir(parents=True, exist_ok=True)
        target = preview_dir / f"{self.channel['id']}.txt"
        try:
            target.write_text(text, encoding="utf-8")
            events.info("supervisor",
                        f"channel {self.channel['id']}: preview "
                        f"{'active' if text else 'cleared'} ({next_name or 'none'})")
        except OSError as exc:  # noqa: BLE001
            events.warn("supervisor",
                        f"channel {self.channel['id']}: preview write failed: {exc}")

    def _read_stderr(self, out: str, proc: subprocess.Popen) -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            line = line.rstrip()
            if not line:
                continue
            low = line.lower()
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
        # clear preview text so a stale file is not picked up on next start
        try:
            target = APP_ROOT / "data" / "preview" / f"{self.channel['id']}.txt"
            if target.is_file():
                target.write_text("", encoding="utf-8")
        except OSError:
            pass
        self._last_preview = ""
        # clear HLS folder so next start begins from MEDIA-SEQUENCE:0
        hls_dir = APP_ROOT / "data" / "hls" / str(self.channel["id"])
        if hls_dir.is_dir():
            for f in list(hls_dir.iterdir()):
                try:
                    f.unlink()
                except OSError:
                    pass


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