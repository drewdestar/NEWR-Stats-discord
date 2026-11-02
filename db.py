"""SQLite persistence for NEWR Stats. No secrets are stored here.

Pattern copied from newr-support/db.py: one module-level DB_PATH
(env DB_PATH, else data.db next to this file), a _conn() helper with
Row factory, and init_db() creating every table with IF NOT EXISTS.

Tables:
  message_counts: one row per (guild, user, UTC day), message count.
  voice_minutes:  one row per (guild, user, UTC day), voice minutes.
  events:         scheduled server events (see cogs/events.py).
  event_attendees: one row per (event, user) RSVP.
  stats_channels: live-stats channel ids per guild (see cogs/stats.py).
  rep_points:     reputation points per (guild, user).
  rep_cooldowns:  last rep-gift time per (guild, giver).

Leaderboard/activity queries only ever read local tables. There is
NO backfill from channel history. The default window is the last
30 days; /leaderboard also offers week (last 7 days) and month
(since the 1st of the current UTC month).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

WINDOW_DAYS = 30
REP_COOLDOWN_HOURS = 24


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns/tables introduced after the first release to
    databases created by an older init_db(), without losing rows."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    if "repeat" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN repeat TEXT NOT NULL DEFAULT 'none'")
    if "next_created" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN next_created INTEGER NOT NULL DEFAULT 0")


def init_db() -> None:
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS message_counts(
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            day TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, day));
        CREATE TABLE IF NOT EXISTS voice_minutes(
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            day TEXT NOT NULL, minutes REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, day));
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            start_at TEXT NOT NULL, created_by INTEGER NOT NULL,
            cancelled INTEGER NOT NULL DEFAULT 0,
            reminded INTEGER NOT NULL DEFAULT 0,
            repeat TEXT NOT NULL DEFAULT 'none',
            next_created INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS event_attendees(
            event_id INTEGER NOT NULL, guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (event_id, user_id));
        CREATE TABLE IF NOT EXISTS stats_channels(
            guild_id INTEGER NOT NULL, kind TEXT NOT NULL,
            channel_id INTEGER NOT NULL, category_id INTEGER,
            PRIMARY KEY (guild_id, kind));
        CREATE TABLE IF NOT EXISTS rep_points(
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            points INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id));
        CREATE TABLE IF NOT EXISTS rep_cooldowns(
            guild_id INTEGER NOT NULL, giver_id INTEGER NOT NULL,
            last_given TEXT NOT NULL,
            PRIMARY KEY (guild_id, giver_id));
        """)
        _migrate(conn)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_start_day() -> str:
    """UTC date string for the first day inside the 30-day window."""
    return (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS - 1)).strftime("%Y-%m-%d")


def window_start_day(window: str = "30d") -> str:
    """First UTC day included by a leaderboard window.

    'week'  -> last 7 days (today minus 6 days).
    'month' -> since the 1st of the current UTC month.
    anything else ('30d') -> last 30 days (today minus 29 days).
    """
    today = datetime.now(timezone.utc)
    if window == "week":
        return (today - timedelta(days=6)).strftime("%Y-%m-%d")
    if window == "month":
        return today.replace(day=1).strftime("%Y-%m-%d")
    return (today - timedelta(days=WINDOW_DAYS - 1)).strftime("%Y-%m-%d")


def window_label(window: str) -> str:
    if window == "week":
        return "last 7 days"
    if window == "month":
        return "this calendar month (UTC)"
    return "last 30 days"


# --- Activity ---------------------------------------------------------

def record_message(guild_id: int, user_id: int, day: str | None = None) -> None:
    """Increment one user's message count for one UTC day."""
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO message_counts VALUES (?, ?, ?, 1) "
            "ON CONFLICT(guild_id, user_id, day) "
            "DO UPDATE SET count = count + 1",
            (guild_id, user_id, day))


def record_voice_minutes(guild_id: int, user_id: int, minutes: float,
                         day: str | None = None) -> None:
    """Add voice minutes for one user for one UTC day. No-op for <= 0."""
    if minutes <= 0:
        return
    day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO voice_minutes VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id, day) "
            "DO UPDATE SET minutes = minutes + excluded.minutes",
            (guild_id, user_id, day, minutes))


def leaderboard(guild_id: int, kind: str, limit: int = 10,
                window: str = "30d") -> list[sqlite3.Row]:
    """Top users by activity in the given window.

    kind is 'text' (message count) or 'voice' (voice minutes).
    Rows have user_id and total (int messages / float minutes).
    Default window '30d' preserves the original behaviour.
    """
    table, col = (("message_counts", "count") if kind == "text"
                  else ("voice_minutes", "minutes"))
    with _conn() as conn:
        return conn.execute(
            f"SELECT user_id, SUM({col}) AS total FROM {table} "
            "WHERE guild_id=? AND day>=? GROUP BY user_id "
            "ORDER BY total DESC, user_id ASC LIMIT ?",
            (guild_id, window_start_day(window), limit)).fetchall()


def user_activity(guild_id: int, user_id: int,
                  window: str = "30d") -> dict[str, float]:
    """One user's message count + voice minutes in the given window."""
    start = window_start_day(window)
    with _conn() as conn:
        msg = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM message_counts "
            "WHERE guild_id=? AND user_id=? AND day>=?",
            (guild_id, user_id, start)).fetchone()[0]
        voice = conn.execute(
            "SELECT COALESCE(SUM(minutes), 0) FROM voice_minutes "
            "WHERE guild_id=? AND user_id=? AND day>=?",
            (guild_id, user_id, start)).fetchone()[0]
    return {"messages": int(msg), "voice_minutes": float(voice)}


def user_rank(guild_id: int, user_id: int, kind: str,
              window: str = "30d") -> dict | None:
    """A user's rank/percentile for one activity kind, or None if
    the user has no activity in the window.

    Rank 1 is the top user; ties break by user_id ascending, the
    same order as leaderboard(). Percentile is
    100 * (N - rank) / (N - 1) for N tracked users (100 for the
    top user, 0 for the bottom; 100 when N == 1), i.e. the share
    of tracked members ranked below the user.
    Returns {'rank', 'total', 'tracked', 'percentile'}.
    """
    table, col = (("message_counts", "count") if kind == "text"
                  else ("voice_minutes", "minutes"))
    start = window_start_day(window)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT user_id, SUM({col}) AS total FROM {table} "
            "WHERE guild_id=? AND day>=? GROUP BY user_id "
            "ORDER BY total DESC, user_id ASC",
            (guild_id, start)).fetchall()
    tracked = len(rows)
    for rank, row in enumerate(rows, start=1):
        if row["user_id"] == user_id:
            if tracked <= 1:
                percentile = 100.0
            else:
                percentile = 100.0 * (tracked - rank) / (tracked - 1)
            return {"rank": rank, "total": row["total"],
                    "tracked": tracked, "percentile": percentile}
    return None


# --- Events -----------------------------------------------------------

def create_event(guild_id: int, channel_id: int, name: str, description: str,
                 start_at_utc_iso: str, created_by: int,
                 repeat: str = "none") -> int:
    repeat = repeat if repeat in ("none", "weekly") else "none"
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (guild_id, channel_id, name, description, "
            "start_at, created_by, repeat, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, name, description, start_at_utc_iso,
             created_by, repeat, now()))
        return cur.lastrowid


def list_events(guild_id: int, upcoming_only: bool = True) -> list[sqlite3.Row]:
    with _conn() as conn:
        if upcoming_only:
            return conn.execute(
                "SELECT * FROM events WHERE guild_id=? AND cancelled=0 "
                "AND start_at>=? ORDER BY start_at ASC",
                (guild_id, now())).fetchall()
        return conn.execute(
            "SELECT * FROM events WHERE guild_id=? ORDER BY start_at ASC",
            (guild_id,)).fetchall()


def get_event(event_id: int, guild_id: int) -> sqlite3.Row | None:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE id=? AND guild_id=?",
            (event_id, guild_id)).fetchone()


def cancel_event(event_id: int, guild_id: int) -> bool:
    """Mark an event cancelled. Returns False if missing/already cancelled."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE events SET cancelled=1 WHERE id=? AND guild_id=? "
            "AND cancelled=0", (event_id, guild_id))
        return cur.rowcount > 0


def update_event(event_id: int, guild_id: int, name: str | None = None,
                 description: str | None = None,
                 start_at_utc_iso: str | None = None,
                 channel_id: int | None = None) -> bool:
    """Update only the given fields of a pending event.

    If the start time changes, reminded is reset to 0 so the moved
    event can be reminded again; other edits keep the flag.
    Returns False if the event does not exist.
    """
    sets, params = [], []
    if name is not None:
        sets.append("name=?"); params.append(name)
    if description is not None:
        sets.append("description=?"); params.append(description)
    if start_at_utc_iso is not None:
        sets.append("start_at=?"); params.append(start_at_utc_iso)
        sets.append("reminded=0")
    if channel_id is not None:
        sets.append("channel_id=?"); params.append(channel_id)
    if not sets:
        return False
    params.extend([event_id, guild_id])
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE events SET {', '.join(sets)} WHERE id=? AND guild_id=?",
            params)
        return cur.rowcount > 0


def due_reminders(now_iso: str | None = None) -> list[sqlite3.Row]:
    """Events starting within the next 1 hour, not yet reminded/cancelled."""
    at = now_iso or now()
    horizon = (datetime.fromisoformat(at.replace("Z", "+00:00"))
               + timedelta(hours=1)).isoformat()
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE cancelled=0 AND reminded=0 "
            "AND start_at>=? AND start_at<=? ORDER BY start_at ASC",
            (at, horizon)).fetchall()


def upcoming_within(guild_id: int, hours: int) -> list[sqlite3.Row]:
    """Non-cancelled events starting within the next `hours` hours."""
    at = now()
    horizon = (datetime.now(timezone.utc)
               + timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE guild_id=? AND cancelled=0 "
            "AND start_at>=? AND start_at<=? ORDER BY start_at ASC",
            (guild_id, at, horizon)).fetchall()


def mark_reminded(event_id: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE events SET reminded=1 WHERE id=?", (event_id,))


def passed_weekly_events(now_iso: str | None = None) -> list[sqlite3.Row]:
    """Non-cancelled weekly events whose start has passed and whose
    next occurrence has not been created yet."""
    at = now_iso or now()
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM events WHERE cancelled=0 AND repeat='weekly' "
            "AND next_created=0 AND start_at<? ORDER BY start_at ASC",
            (at,)).fetchall()


def create_next_occurrence(event_row: sqlite3.Row) -> int:
    """Create the +7-day occurrence of a weekly event and mark the
    original as having spawned its successor (idempotent: the
    caller only passes rows with next_created=0)."""
    start = datetime.fromisoformat(
        event_row["start_at"].replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    next_start = (start + timedelta(days=7)).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (guild_id, channel_id, name, description, "
            "start_at, created_by, repeat, created_at) VALUES (?, ?, ?, ?, ?, ?, 'weekly', ?)",
            (event_row["guild_id"], event_row["channel_id"], event_row["name"],
             event_row["description"], next_start, event_row["created_by"], now()))
        conn.execute("UPDATE events SET next_created=1 WHERE id=?",
                     (event_row["id"],))
        return cur.lastrowid


# --- Event RSVP -------------------------------------------------------

def rsvp_join(event_id: int, guild_id: int, user_id: int) -> str:
    """Join an RSVP list. Returns 'joined', 'already', or 'not_found'
    (not_found covers a guild mismatch; callers validate cancelled/past
    separately so the error message can name the reason)."""
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM events WHERE id=? AND guild_id=?",
                        (event_id, guild_id)).fetchone() is None:
            return "not_found"
        try:
            conn.execute(
                "INSERT INTO event_attendees VALUES (?, ?, ?)",
                (event_id, guild_id, user_id))
            return "joined"
        except sqlite3.IntegrityError:
            return "already"


def rsvp_leave(event_id: int, guild_id: int, user_id: int) -> str:
    """Leave an RSVP list. Returns 'left', 'not_attending', 'not_found'."""
    with _conn() as conn:
        if conn.execute("SELECT 1 FROM events WHERE id=? AND guild_id=?",
                        (event_id, guild_id)).fetchone() is None:
            return "not_found"
        cur = conn.execute(
            "DELETE FROM event_attendees WHERE event_id=? AND user_id=?",
            (event_id, user_id))
        return "left" if cur.rowcount > 0 else "not_attending"


def event_attendees(event_id: int) -> list[int]:
    """Attendee user ids for an event, ascending (stable order)."""
    with _conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT user_id FROM event_attendees WHERE event_id=? "
            "ORDER BY user_id ASC", (event_id,)).fetchall()]


# --- Live stats channels ----------------------------------------------

def set_stats_channel(guild_id: int, kind: str, channel_id: int,
                      category_id: int | None) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO stats_channels VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, kind) DO UPDATE SET "
            "channel_id=excluded.channel_id, category_id=excluded.category_id",
            (guild_id, kind, channel_id, category_id))


def get_stats_channels(guild_id: int) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT * FROM stats_channels WHERE guild_id=?", (guild_id,)).fetchall()


def clear_stats_channels(guild_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM stats_channels WHERE guild_id=?", (guild_id,))


# --- Reputation ---------------------------------------------------------

def give_rep(guild_id: int, giver_id: int, recipient_id: int) -> tuple[str, int | None]:
    """Award 1 reputation point, with anti-abuse rules.

    Returns (status, points_or_remaining):
      ('ok', new_total)            - point awarded.
      ('self', None)               - giver == recipient.
      ('cooldown', seconds_left)   - giver already gave within 24h.
    Bots are rejected by the caller (it can see discord.Member.bot);
    this layer only enforces self-rep and the cooldown.
    """
    if giver_id == recipient_id:
        return "self", None
    at = datetime.now(timezone.utc)
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_given FROM rep_cooldowns WHERE guild_id=? AND giver_id=?",
            (guild_id, giver_id)).fetchone()
        if row is not None:
            last = datetime.fromisoformat(row["last_given"].replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            elapsed = (at - last).total_seconds()
            limit = REP_COOLDOWN_HOURS * 3600
            if elapsed < limit:
                return "cooldown", int(limit - elapsed)
        conn.execute(
            "INSERT INTO rep_points VALUES (?, ?, 1) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET points = points + 1",
            (guild_id, recipient_id))
        conn.execute(
            "INSERT INTO rep_cooldowns VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, giver_id) DO UPDATE SET last_given=excluded.last_given",
            (guild_id, giver_id, at.isoformat()))
        total = conn.execute(
            "SELECT points FROM rep_points WHERE guild_id=? AND user_id=?",
            (guild_id, recipient_id)).fetchone()[0]
        return "ok", int(total)


def rep_points_for(guild_id: int, user_id: int) -> int:
    with _conn() as conn:
        row = conn.execute(
            "SELECT points FROM rep_points WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)).fetchone()
    return int(row[0]) if row else 0


def rep_top(guild_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(
            "SELECT user_id, points FROM rep_points WHERE guild_id=? "
            "AND points > 0 ORDER BY points DESC, user_id ASC LIMIT ?",
            (guild_id, limit)).fetchall()
