"""Reminders — set in chat, fired as desktop toasts by the daemon.

`/remind in 20m stretch`, `/remind at 5pm take out trash`, `/remind tomorrow
9am call the bank`. Stored in their own table in memory.db (same WAL discipline
as everything else via livingpc/db.py); the inference scheduler's 30-second
poll fires due ones through livingpc/notify.py, so reminders work whenever the
tray daemon is running — no Windows Task Scheduler involved.

Parsing is deliberately small and predictable, not a full NLP date parser:
  in 20m / in 2h / in 1h30m          relative
  at 5pm / at 17:30 / at 9:15am      today (or tomorrow if already past)
  tomorrow 9am / tomorrow at 17:00   next day
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .db import connect as db_connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    due_ts TEXT NOT NULL,
    text TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminder_due ON reminder (fired, due_ts);
"""

_REL = re.compile(r"^in\s+(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?\b",
                  re.IGNORECASE)
_AT = re.compile(r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE)


def parse_when(text: str, *, now: datetime | None = None
               ) -> tuple[datetime | None, str]:
    """Split '<when> <message>' -> (due datetime, message). None when unparsed."""
    now = now or datetime.now()
    raw = text.strip()
    tomorrow = False
    if raw.lower().startswith("tomorrow"):
        tomorrow = True
        raw = raw[len("tomorrow"):].strip()

    match = _REL.match(raw)
    if match and not tomorrow and (match.group(1) or match.group(2)):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        message = raw[match.end():].strip()
        return now + timedelta(hours=hours, minutes=minutes), message

    match = _AT.match(raw)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = (match.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None, text.strip()
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if tomorrow:
            due += timedelta(days=1)
        elif due <= now:
            due += timedelta(days=1)   # "at 5pm" after 5pm means tomorrow
        message = raw[match.end():].strip()
        return due, message
    return None, text.strip()


class ReminderStore:
    def __init__(self, db_path: str):
        self.conn = db_connect(db_path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def add(self, due: datetime, text: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO reminder (due_ts, text, created_ts) VALUES (?, ?, ?)",
            (due.isoformat(timespec="seconds"), text,
             datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
        return int(cur.lastrowid)

    def pending(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, due_ts, text FROM reminder WHERE fired=0 "
            "ORDER BY due_ts").fetchall()
        return [{"id": r[0], "due_ts": r[1], "text": r[2]} for r in rows]

    def due(self, *, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        rows = self.conn.execute(
            "SELECT id, due_ts, text FROM reminder WHERE fired=0 AND due_ts <= ? "
            "ORDER BY due_ts", (now.isoformat(timespec="seconds"),)).fetchall()
        return [{"id": r[0], "due_ts": r[1], "text": r[2]} for r in rows]

    def mark_fired(self, reminder_id: int) -> None:
        self.conn.execute("UPDATE reminder SET fired=1 WHERE id=?", (reminder_id,))
        self.conn.commit()

    def cancel(self, reminder_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM reminder WHERE id=? AND fired=0", (reminder_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self.conn.close()


def fire_due(cfg, *, now: datetime | None = None) -> int:
    """Toast every due reminder and mark it fired. Returns count. Best-effort
    callers (the scheduler) wrap this; it raises only on DB-level trouble."""
    from .notify import notify
    store = ReminderStore(cfg.memory_db_path)
    try:
        fired = 0
        for reminder in store.due(now=now):
            notify("⏰ Reminder", reminder["text"] or "(no message)", cfg=cfg)
            store.mark_fired(reminder["id"])
            fired += 1
        return fired
    finally:
        store.close()
