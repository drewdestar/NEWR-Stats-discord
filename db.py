"""SQLite persistence for NEWR Stats. No secrets are stored here.

Pattern copied from newr-support/db.py: one module-level DB_PATH
(env DB_PATH, else data.db next to this file), a _conn() helper with
Row factory, and init_db() creating every table with IF NOT EXISTS.

Tables:
  message_counts: one row per (guild, user, UTC day), message count.
  voice_minutes:  one row per (guild, user, UTC day), voice minutes.
  events:         scheduled server events (see cogs/events.py).

Leaderboard/activity queries only ever read the last 30 days of
these local tables. There is NO backfill from channel history.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get(
    "DB_PATH", os.path.join(os.path.dirname(__file__), "data.db"))

WINDOW_DAYS = 30


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
            created_at TEXT NOT NULL);
        """)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_start_day() -> str:
    """UTC date string for the first day inside the 30-day window."""
    return (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS - 1)).strftime("%Y-%m-%d")


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


def leaderboard(guild_id: int, kind: str, limit: int = 10) -> list[sqlite3.Row]:
    """Top users by activity in the last 30 days.

    kind is 'text' (message count) or 'voice' (voice minutes).
    Rows have user_id and total (int messages / float minutes).
    """
    table, col = (("message_counts", "count") if kind == "text"
                  else ("voice_minutes", "minutes"))
    with _conn() as conn:
        return conn.execute(
            f"SELECT user_id, SUM({col}) AS total FROM {table} "
            "WHERE guild_id=? AND day>=? GROUP BY user_id "
            "ORDER BY total DESC, user_id ASC LIMIT ?",
            (guild_id, _window_start_day(), limit)).fetchall()


def user_activity(guild_id: int, user_id: int) -> dict[str, float]:
    """One user's message count + voice minutes in the last 30 days."""
    start = _window_start_day()
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


# --- Events -----------------------------------------------------------

def create_event(guild_id: int, channel_id: int, name: str, description: str,
                 start_at_utc_iso: str, created_by: int) -> int:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO events (guild_id, channel_id, name, description, "
            "start_at, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, name, description, start_at_utc_iso,
             created_by, now()))
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
