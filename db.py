"""SQLite layer for VHS-Sat. Cross-platform."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from config import config
from frozen_utils import resolve_path


DB_PATH = resolve_path(config.get("database", "path", default="data/vhs-sat.db"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL DEFAULT 'folder',
  enabled INTEGER NOT NULL DEFAULT 1,

  folder TEXT,
  rotation TEXT NOT NULL DEFAULT 'random',
  reshuffle_on_loop INTEGER NOT NULL DEFAULT 0,
  file_extensions TEXT NOT NULL DEFAULT 'mp4,mkv,avi,ts,mov,m4v,webm',
  codec_strategy TEXT NOT NULL DEFAULT 'auto',
  bumper_enabled INTEGER NOT NULL DEFAULT 1,
  bumper_duration_sec INTEGER NOT NULL DEFAULT 5,

  stream_url TEXT,
  stream_user TEXT,
  stream_pass TEXT,

  channel_number INTEGER,

  hls_enabled INTEGER NOT NULL DEFAULT 1,
  multicast_enabled INTEGER NOT NULL DEFAULT 0,
  multicast_addr TEXT DEFAULT '239.1.1.1',
  multicast_port INTEGER DEFAULT 5000,
  ticker_enabled INTEGER NOT NULL DEFAULT 0,
  ticker_text TEXT DEFAULT '',
  ticker_speed INTEGER NOT NULL DEFAULT 100,
  ticker_fontsize INTEGER NOT NULL DEFAULT 24,
  ticker_color TEXT DEFAULT 'white',
  ticker_bgcolor TEXT DEFAULT 'black@0.5',

  pip_enabled INTEGER NOT NULL DEFAULT 0,
  pip_mode TEXT DEFAULT 'none',
  pip_source TEXT DEFAULT '',
  pip_position TEXT DEFAULT 'top-right',
  pip_width INTEGER NOT NULL DEFAULT 320,
  pip_margin INTEGER NOT NULL DEFAULT 16,


  adapter_id TEXT,
  transponder_id INTEGER,
  service_id INTEGER,

  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channel_state (
  channel_id INTEGER PRIMARY KEY,
  running INTEGER NOT NULL DEFAULT 0,
  started_at TEXT,
  last_error TEXT,
  FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS file_durations (
  path TEXT PRIMARY KEY,
  mtime REAL NOT NULL,
  duration_sec INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mosaic_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  stream_url TEXT NOT NULL,
  stream_user TEXT,
  stream_pass TEXT,
  label TEXT,
  FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_mosaic_channel
  ON mosaic_sources(channel_id, position);

CREATE TABLE IF NOT EXISTS overlay_schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  days TEXT NOT NULL DEFAULT '1,2,3,4,5,6,7',
  payload TEXT NOT NULL DEFAULT '{}',
  priority INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_overlay_sched_channel
  ON overlay_schedules(channel_id, enabled);

CREATE TABLE IF NOT EXISTS tickers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL,
  name TEXT NOT NULL DEFAULT 'Ticker',
  text TEXT NOT NULL DEFAULT '',
  speed INTEGER NOT NULL DEFAULT 100,
  fontsize INTEGER NOT NULL DEFAULT 24,
  color TEXT NOT NULL DEFAULT 'white',
  bgcolor TEXT NOT NULL DEFAULT 'black@0.5',
  is_default INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')),
  FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tickers_channel ON tickers(channel_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tickers_default
  ON tickers(channel_id) WHERE is_default = 1;
"""


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(channels)").fetchall()}
    new_cols = [
        ("stream_url", "TEXT"),
        ("stream_user", "TEXT"),
        ("stream_pass", "TEXT"),
        ("channel_number", "INTEGER"),
        ("ticker_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("ticker_text", "TEXT DEFAULT ''"),
        ("ticker_speed", "INTEGER NOT NULL DEFAULT 100"),
        ("ticker_fontsize", "INTEGER NOT NULL DEFAULT 24"),
        ("ticker_color", "TEXT DEFAULT 'white'"),
        ("ticker_bgcolor", "TEXT DEFAULT 'black@0.5'"),
        ("pip_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("pip_mode", "TEXT DEFAULT 'none'"),
        ("pip_source", "TEXT DEFAULT ''"),
        ("pip_position", "TEXT DEFAULT 'top-right'"),
        ("pip_width", "INTEGER NOT NULL DEFAULT 320"),
        ("pip_margin", "INTEGER NOT NULL DEFAULT 16"),
    ]
    added_number = False
    for name, ddl in new_cols:
        if name not in existing:
            conn.execute(f"ALTER TABLE channels ADD COLUMN {name} {ddl}")
            if name == "channel_number":
                added_number = True

    if added_number:
        rows = conn.execute("SELECT id FROM channels ORDER BY id").fetchall()
        for i, row in enumerate(rows, start=1):
            conn.execute("UPDATE channels SET channel_number = ? WHERE id = ?",
                         (i, row[0]))

    rows = conn.execute(
        "SELECT id FROM channels WHERE channel_number IS NULL ORDER BY id"
    ).fetchall()
    if rows:
        used = {r[0] for r in conn.execute(
            "SELECT channel_number FROM channels WHERE channel_number IS NOT NULL"
        ).fetchall()}
        n = 1
        for row in rows:
            while n in used:
                n += 1
            conn.execute("UPDATE channels SET channel_number = ? WHERE id = ?",
                         (n, row[0]))
            used.add(n)
            n += 1

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_number "
        "ON channels(channel_number) WHERE channel_number IS NOT NULL"
    )
    # --- overlay_schedules: add ticker_id ---
    sched_cols = {r[1] for r in conn.execute("PRAGMA table_info(overlay_schedules)").fetchall()}
    if "ticker_id" not in sched_cols:
        try:
            conn.execute("ALTER TABLE overlay_schedules ADD COLUMN ticker_id INTEGER")
            # one-time: drop orphan ticker schedules (they referenced payload, now invalid)
            conn.execute(
                "DELETE FROM overlay_schedules WHERE kind = 'ticker' AND ticker_id IS NULL"
            )
        except sqlite3.OperationalError:
            pass

    # --- migrate legacy ticker_* from channels to tickers ---
    channels_cols = {r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
    if "ticker_text" in channels_cols:
        try:
            rows = conn.execute(
                "SELECT id, ticker_text, ticker_enabled, ticker_speed, "
                "ticker_fontsize, ticker_color, ticker_bgcolor FROM channels"
            ).fetchall()
            for row in rows:
                cid = row[0]
                text = (row[1] or "").strip()
                enabled = row[2] or 0
                if not text and not enabled:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM tickers WHERE channel_id = ? LIMIT 1", (cid,)
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    "INSERT INTO tickers "
                    "(channel_id, name, text, speed, fontsize, color, bgcolor, is_default) "
                    "VALUES (?, 'По умолчанию', ?, ?, ?, ?, ?, 1)",
                    (cid, text, row[3] or 100, row[4] or 24,
                     row[5] or "white", row[6] or "black@0.5"),
                )
        except sqlite3.OperationalError:
            pass

        # drop legacy ticker_* columns from channels
        for col in ("ticker_enabled", "ticker_text", "ticker_speed",
                    "ticker_fontsize", "ticker_color", "ticker_bgcolor"):
            try:
                conn.execute(f"ALTER TABLE channels DROP COLUMN {col}")
            except sqlite3.OperationalError:
                pass

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_CHANNEL_FIELDS = [
    "name", "source_type", "folder", "rotation", "reshuffle_on_loop",
    "file_extensions", "codec_strategy", "bumper_enabled", "bumper_duration_sec",
    "stream_url", "stream_user", "stream_pass", "channel_number",
    "hls_enabled", "multicast_enabled", "multicast_addr", "multicast_port",
    "pip_enabled", "pip_mode", "pip_source", "pip_position",
    "pip_width", "pip_margin",
    "enabled",
]


def list_channels() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM channels ORDER BY COALESCE(channel_number, 9999), id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_channel(channel_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?",
                           (channel_id,)).fetchone()
        return dict(row) if row else None


def create_channel(data: dict) -> int:
    values = [data.get(f) for f in _CHANNEL_FIELDS]
    cols = ", ".join(_CHANNEL_FIELDS)
    ph = ", ".join("?" * len(_CHANNEL_FIELDS))
    with get_db() as conn:
        cur = conn.execute(f"INSERT INTO channels ({cols}) VALUES ({ph})", values)
        return cur.lastrowid


def update_channel(channel_id: int, data: dict) -> None:
    allowed = {f for f in _CHANNEL_FIELDS if f not in ("source_type",)}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k} = ?" for k in updates)
    with get_db() as conn:
        conn.execute(
            f"UPDATE channels SET {sets}, updated_at = datetime('now') WHERE id = ?",
            list(updates.values()) + [channel_id],
        )


def delete_channel(channel_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))


def find_channels_by_folder(folder: str, exclude_id: int | None = None) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM channels WHERE source_type = 'folder' AND folder = ?",
            (folder,),
        ).fetchall()
    return [dict(r) for r in rows if r["id"] != exclude_id]


def find_channels_by_stream_url(url: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM channels WHERE source_type IN ('iptv','camera') AND stream_url = ?",
            (url,),
        ).fetchall()
    return [dict(r) for r in rows]


def find_channel_by_number(num: int, exclude_id: int | None = None) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM channels WHERE channel_number = ?", (num,),
        ).fetchone()
    if row and row["id"] != exclude_id:
        return dict(row)
    return None


def next_free_number() -> int:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT channel_number FROM channels WHERE channel_number IS NOT NULL"
        ).fetchall()
    used = {r[0] for r in rows}
    n = 1
    while n in used:
        n += 1
    return n


# ---------- mosaic sources ----------
def list_mosaic_sources(channel_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM mosaic_sources WHERE channel_id = ? ORDER BY position, id",
            (channel_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_mosaic_source(src_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM mosaic_sources WHERE id = ?",
                           (src_id,)).fetchone()
        return dict(row) if row else None


def add_mosaic_source(channel_id: int, data: dict) -> int:
    with get_db() as conn:
        nxt = conn.execute(
            "SELECT COALESCE(MAX(position)+1, 0) FROM mosaic_sources WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO mosaic_sources "
            "(channel_id, position, stream_url, stream_user, stream_pass, label) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, nxt, data.get("stream_url"),
             data.get("stream_user"), data.get("stream_pass"), data.get("label")),
        )
        return cur.lastrowid


def update_mosaic_source(src_id: int, data: dict) -> None:
    allowed = {"stream_url", "stream_user", "stream_pass", "label"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k} = ?" for k in updates)
    with get_db() as conn:
        conn.execute(f"UPDATE mosaic_sources SET {sets} WHERE id = ?",
                     list(updates.values()) + [src_id])


def delete_mosaic_source(src_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM mosaic_sources WHERE id = ?", (src_id,))


def move_mosaic_source(src_id: int, direction: str) -> None:
    with get_db() as conn:
        src = conn.execute("SELECT * FROM mosaic_sources WHERE id = ?",
                           (src_id,)).fetchone()
        if not src:
            return
        cid = src["channel_id"]
        pos = src["position"]
        if direction == "up":
            other = conn.execute(
                "SELECT * FROM mosaic_sources WHERE channel_id = ? AND position < ? "
                "ORDER BY position DESC LIMIT 1", (cid, pos),
            ).fetchone()
        else:
            other = conn.execute(
                "SELECT * FROM mosaic_sources WHERE channel_id = ? AND position > ? "
                "ORDER BY position ASC LIMIT 1", (cid, pos),
            ).fetchone()
        if not other:
            return
        conn.execute("UPDATE mosaic_sources SET position = ? WHERE id = ?",
                     (other["position"], src["id"]))
        conn.execute("UPDATE mosaic_sources SET position = ? WHERE id = ?",
                     (pos, other["id"]))

        # ---------- channel state (persist running status across restarts) ----------
        def set_channel_running(channel_id: int, running: bool, error: str | None = None) -> None:
            with get_db() as conn:
                if running:
                    conn.execute(
                        "INSERT INTO channel_state (channel_id, running, started_at, last_error) "
                        "VALUES (?, 1, datetime('now'), NULL) "
                        "ON CONFLICT(channel_id) DO UPDATE SET "
                        "running = 1, started_at = datetime('now'), last_error = NULL",
                        (channel_id,),
                    )
                else:
                    conn.execute(
                        "INSERT INTO channel_state (channel_id, running, last_error) "
                        "VALUES (?, 0, ?) "
                        "ON CONFLICT(channel_id) DO UPDATE SET "
                        "running = 0, last_error = ?",
                        (channel_id, error, error),
                    )

        def get_running_channel_ids() -> list[int]:
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT channel_id FROM channel_state WHERE running = 1"
                ).fetchall()
            return [r[0] for r in rows]

# ---------- channel state (persist running status across restarts) ----------
def set_channel_running(channel_id: int, running: bool, error: str | None = None) -> None:
    with get_db() as conn:
        if running:
            conn.execute(
                "INSERT INTO channel_state (channel_id, running, started_at, last_error) "
                "VALUES (?, 1, datetime('now'), NULL) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "running = 1, started_at = datetime('now'), last_error = NULL",
                (channel_id,),
            )
        else:
            conn.execute(
                "INSERT INTO channel_state (channel_id, running, last_error) "
                "VALUES (?, 0, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET "
                "running = 0, last_error = ?",
                (channel_id, error, error),
            )


def get_running_channel_ids() -> list[int]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT channel_id FROM channel_state WHERE running = 1"
        ).fetchall()
    return [r[0] for r in rows]

# ---------- overlay schedules ----------
_SCHEDULE_FIELDS = [
    "kind", "start_time", "end_time", "days", "payload", "priority",
    "enabled", "ticker_id",
]


def list_schedules(channel_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM overlay_schedules WHERE channel_id = ? "
            "ORDER BY priority DESC, created_at DESC",
            (channel_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_schedule(sid: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM overlay_schedules WHERE id = ?", (sid,)
        ).fetchone()
    return dict(row) if row else None


def create_schedule(channel_id: int, data: dict) -> int:
    values = [data.get(f) for f in _SCHEDULE_FIELDS]
    cols = ", ".join(["channel_id"] + _SCHEDULE_FIELDS)
    ph = ", ".join("?" * (len(_SCHEDULE_FIELDS) + 1))
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT INTO overlay_schedules ({cols}) VALUES ({ph})",
            [channel_id] + values,
        )
        return cur.lastrowid


def update_schedule(sid: int, data: dict) -> None:
    updates = {k: v for k, v in data.items() if k in _SCHEDULE_FIELDS}
    if not updates:
        return
    sets = ", ".join(f"{k} = ?" for k in updates)
    with get_db() as conn:
        conn.execute(
            f"UPDATE overlay_schedules SET {sets} WHERE id = ?",
            list(updates.values()) + [sid],
        )


def delete_schedule(sid: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM overlay_schedules WHERE id = ?", (sid,))


def _schedule_is_active(sched: dict, now_dt) -> bool:
    """Check if the schedule is active at given moment (local time).

    - Days: '1,2,3' where 1=Mon ... 7=Sun (ISO weekday)
    - Handles windows crossing midnight (e.g. 22:00-06:00)
    - start_time == end_time is treated as full day
    """
    try:
        sh, sm = map(int, (sched.get("start_time") or "00:00").split(":")[:2])
        eh, em = map(int, (sched.get("end_time") or "23:59").split(":")[:2])
    except (ValueError, AttributeError):
        return False

    try:
        days = {int(d.strip()) for d in (sched.get("days") or "").split(",") if d.strip()}
    except ValueError:
        days = set()
    if not days:
        days = {1, 2, 3, 4, 5, 6, 7}

    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    now_min = now_dt.hour * 60 + now_dt.minute
    today_iso = now_dt.isoweekday()  # 1..7

    if start_min == end_min:
        return today_iso in days

    if start_min < end_min:
        return start_min <= now_min < end_min and today_iso in days

    # window crosses midnight
    if now_min >= start_min:
        return today_iso in days
    if now_min < end_min:
        yesterday_iso = today_iso - 1 if today_iso > 1 else 7
        return yesterday_iso in days
    return False


def get_active_schedule(channel_id: int, kind: str,
                        now_dt=None) -> dict | None:
    """Return the currently active schedule of given kind, or None.

    Priority: higher wins; ties broken by created_at DESC.
    """
    if now_dt is None:
        from datetime import datetime
        now_dt = datetime.now()

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM overlay_schedules "
            "WHERE channel_id = ? AND kind = ? AND enabled = 1 "
            "ORDER BY priority DESC, created_at DESC",
            (channel_id, kind),
        ).fetchall()

    for row in rows:
        sched = dict(row)
        if _schedule_is_active(sched, now_dt):
            return sched
    return None
def get_active_ticker(channel_id: int, now_dt=None) -> dict | None:
    """Return the ticker that should be shown right now.

    Priority:
    1. Active ticker-schedule → its ticker.
    2. No active schedule → default ticker (is_default=1).
    3. Nothing → None.
    """
    from datetime import datetime
    if now_dt is None:
        now_dt = datetime.now()
    sched = get_active_schedule(channel_id, "ticker", now_dt)
    if sched and sched.get("ticker_id"):
        t = get_ticker(sched["ticker_id"])
        if t:
            return t
    return get_default_ticker(channel_id)
# ---------- tickers ----------
_TICKER_FIELDS = [
    "name", "text", "speed", "fontsize", "color", "bgcolor", "is_default"
]


def list_tickers(channel_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM tickers WHERE channel_id = ? "
            "ORDER BY is_default DESC, name, id",
            (channel_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ticker(tid: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tickers WHERE id = ?", (tid,)).fetchone()
    return dict(row) if row else None


def get_default_ticker(channel_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM tickers WHERE channel_id = ? AND is_default = 1",
            (channel_id,),
        ).fetchone()
    return dict(row) if row else None


def create_ticker(channel_id: int, data: dict) -> int:
    values = [data.get(f) for f in _TICKER_FIELDS]
    cols = ", ".join(["channel_id"] + _TICKER_FIELDS)
    ph = ", ".join("?" * (len(_TICKER_FIELDS) + 1))
    with get_db() as conn:
        cur = conn.execute(
            f"INSERT INTO tickers ({cols}) VALUES ({ph})",
            [channel_id] + values,
        )
        return cur.lastrowid


def update_ticker(tid: int, data: dict) -> None:
    updates = {k: v for k, v in data.items() if k in _TICKER_FIELDS}
    if not updates:
        return
    sets = ", ".join(f"{k} = ?" for k in updates)
    with get_db() as conn:
        conn.execute(
            f"UPDATE tickers SET {sets} WHERE id = ?",
            list(updates.values()) + [tid],
        )


def delete_ticker(tid: int) -> None:
    with get_db() as conn:
        conn.execute(
            "DELETE FROM overlay_schedules WHERE ticker_id = ?", (tid,)
        )
        conn.execute("DELETE FROM tickers WHERE id = ?", (tid,))


def _expand_schedule(start_min: int, end_min: int,
                     days: set[int]) -> list[tuple[int, int]]:
    """Return list of (abs_start, abs_end) in week-minutes (day*1440 + minutes)."""
    intervals: list[tuple[int, int]] = []
    for d in days:
        if start_min == end_min:
            intervals.append((d * 1440, (d + 1) * 1440))
        elif start_min < end_min:
            intervals.append((d * 1440 + start_min, d * 1440 + end_min))
        else:
            # crosses midnight
            intervals.append((d * 1440 + start_min, (d + 1) * 1440))
            next_day = d + 1 if d < 7 else 1
            intervals.append((next_day * 1440, next_day * 1440 + end_min))
    return intervals


def _intervals_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def check_ticker_overlap(channel_id: int, start_time: str, end_time: str,
                         days_str: str,
                         exclude_id: int | None = None) -> dict | None:
    """Return conflicting ticker schedule (dict) or None.

    Only checks kind='ticker' schedules that are enabled and have ticker_id.
    """
    try:
        sh, sm = map(int, start_time.split(":"))
        eh, em = map(int, end_time.split(":"))
    except (ValueError, AttributeError):
        return None
    new_start = sh * 60 + sm
    new_end = eh * 60 + em
    try:
        new_days = {int(d) for d in days_str.split(",") if d.strip()}
    except (ValueError, AttributeError):
        new_days = set()
    if not new_days:
        new_days = {1, 2, 3, 4, 5, 6, 7}
    new_intervals = _expand_schedule(new_start, new_end, new_days)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, start_time, end_time, days FROM overlay_schedules "
            "WHERE channel_id = ? AND kind = 'ticker' AND enabled = 1 "
            "AND ticker_id IS NOT NULL",
            (channel_id,),
        ).fetchall()

    for row in rows:
        if exclude_id is not None and row["id"] == exclude_id:
            continue
        try:
            esh, esm = map(int, row["start_time"].split(":"))
            eeh, eem = map(int, row["end_time"].split(":"))
            e_start = esh * 60 + esm
            e_end = eeh * 60 + eem
            e_days = {int(d) for d in row["days"].split(",") if d.strip()}
        except (ValueError, AttributeError):
            continue
        if not e_days:
            e_days = {1, 2, 3, 4, 5, 6, 7}
        e_intervals = _expand_schedule(e_start, e_end, e_days)
        for ni in new_intervals:
            for ei in e_intervals:
                if _intervals_overlap(ni, ei):
                    return dict(row)
    return None