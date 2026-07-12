"""Local, metadata-only cadence for unsolicited reflective prompts.

The cadence is shared by Investigations, inference review, and GoalAI so each
subsystem cannot independently nag the user. Prompt bodies are deliberately
not stored here: only kind, opaque subject key, timing, and explicit feedback.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .db import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS reflection_prompt_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    eligible_at TEXT NOT NULL,
    shown_at TEXT,
    resolved_at TEXT,
    snooze_count INTEGER NOT NULL DEFAULT 0,
    usefulness INTEGER,
    burden INTEGER,
    CHECK (status IN ('pending','shown','acted','snoozed','ignored','never','suppressed')),
    CHECK (usefulness IS NULL OR usefulness BETWEEN 1 AND 5),
    CHECK (burden IS NULL OR burden BETWEEN 1 AND 5)
);
CREATE INDEX IF NOT EXISTS idx_reflection_prompt_pending
ON reflection_prompt_event(status,eligible_at,priority DESC,id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reflection_prompt_open_subject
ON reflection_prompt_event(kind,subject_key)
WHERE status IN ('pending','shown','snoozed');
CREATE TABLE IF NOT EXISTS reflection_prompt_preference (
    kind TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    ignored_count INTEGER NOT NULL DEFAULT 0,
    snooze_count INTEGER NOT NULL DEFAULT 0,
    suppress_until TEXT,
    never_prompt INTEGER NOT NULL DEFAULT 0,
    last_feedback_at TEXT,
    PRIMARY KEY (kind,subject_key)
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _utc(value: datetime) -> datetime:
    return _aware(value).astimezone(timezone.utc)


def in_quiet_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """Return whether local ``now`` falls in a possibly overnight quiet span."""
    hour = _aware(now).hour
    start, end = int(start_hour) % 24, int(end_hour) % 24
    if start == end:
        return False
    return hour >= start or hour < end if start > end else start <= hour < end


class ReflectionCadenceStore:
    """Persist and arbitrate reflection prompts without retaining prompt text."""

    def __init__(self, db_path: str):
        self.conn = connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _pref(self, kind: str, subject_key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM reflection_prompt_preference WHERE kind=? AND subject_key=?",
            (str(kind), str(subject_key))).fetchone()

    def offer(self, kind: str, subject_key: str, trigger_kind: str, *,
              priority: int = 0, now: datetime | None = None,
              backlog_limit: int = 3) -> dict:
        """Queue or refresh one metadata-only prompt; never duplicates a subject."""
        now = _utc(now or utc_now())
        stamp = now.isoformat()
        kind, subject_key = str(kind), str(subject_key)
        pref = self._pref(kind, subject_key)
        if pref and pref["never_prompt"]:
            return {"accepted": False, "reason": "never"}
        if pref and pref["suppress_until"] and pref["suppress_until"] > stamp:
            return {"accepted": False, "reason": "suppressed"}
        existing = self.conn.execute(
            "SELECT * FROM reflection_prompt_event WHERE kind=? AND subject_key=? "
            "AND status IN ('pending','shown','snoozed') ORDER BY id DESC LIMIT 1",
            (kind, subject_key)).fetchone()
        if existing:
            if existing["status"] != "shown":
                self.conn.execute(
                    "UPDATE reflection_prompt_event SET priority=MAX(priority,?),"
                    "trigger_kind=? WHERE id=?",
                    (int(priority), str(trigger_kind), existing["id"]))
                self.conn.commit()
            return {"accepted": True, "event_id": existing["id"], "deduped": True}
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM reflection_prompt_event "
            "WHERE status IN ('pending','snoozed')").fetchone()[0]
        if pending >= max(1, int(backlog_limit)):
            return {"accepted": False, "reason": "backlog"}
        cur = self.conn.execute(
            "INSERT INTO reflection_prompt_event "
            "(kind,subject_key,trigger_kind,priority,status,created_at,eligible_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (kind, subject_key, str(trigger_kind), int(priority), "pending", stamp, stamp))
        self.conn.commit()
        return {"accepted": True, "event_id": cur.lastrowid, "deduped": False}

    def claim_next(self, *, now: datetime | None = None, min_days: int = 7,
                   quiet_start_hour: int = 21, quiet_end_hour: int = 8) -> dict | None:
        """Claim at most one prompt, enforcing quiet hours and the global cap."""
        local_now = _aware(now or utc_now())
        if in_quiet_hours(local_now, quiet_start_hour, quiet_end_hour):
            return None
        now = _utc(local_now)
        last = self.conn.execute(
            "SELECT shown_at FROM reflection_prompt_event WHERE shown_at IS NOT NULL "
            "ORDER BY shown_at DESC LIMIT 1").fetchone()
        if last and datetime.fromisoformat(last["shown_at"]) > now - timedelta(days=max(0, int(min_days))):
            return None
        row = self.conn.execute(
            "SELECT * FROM reflection_prompt_event WHERE status IN ('pending','snoozed') "
            "AND eligible_at<=? ORDER BY priority DESC,created_at,id LIMIT 1",
            (now.isoformat(),)).fetchone()
        if not row:
            return None
        self.conn.execute(
            "UPDATE reflection_prompt_event SET status='shown',shown_at=? WHERE id=?",
            (now.isoformat(), row["id"]))
        self.conn.commit()
        return dict(self.conn.execute(
            "SELECT * FROM reflection_prompt_event WHERE id=?", (row["id"],)).fetchone())

    def feedback(self, event_id: int, action: str, *, usefulness=None, burden=None,
                 now: datetime | None = None, snooze_base_days: int = 3,
                 ignore_suppress_days: int = 30) -> dict:
        """Record explicit burden/usefulness and apply snooze/ignore preferences."""
        now = _utc(now or utc_now())
        row = self.conn.execute(
            "SELECT * FROM reflection_prompt_event WHERE id=?", (int(event_id),)).fetchone()
        if not row:
            raise KeyError("reflection prompt not found")
        action = str(action or "acted").strip().lower()
        if action not in {"acted", "snooze", "ignore", "never"}:
            raise ValueError("invalid reflection feedback action")
        useful = None if usefulness is None else max(1, min(5, int(usefulness)))
        heavy = None if burden is None else max(1, min(5, int(burden)))
        pref = self._pref(row["kind"], row["subject_key"])
        ignored = int(pref["ignored_count"] if pref else 0)
        snoozed = int(pref["snooze_count"] if pref else 0)
        suppress_until, never_prompt = None, 0
        status, eligible_at, resolved_at = action, row["eligible_at"], now.isoformat()
        if action == "snooze":
            snoozed += 1
            days = min(28, max(1, int(snooze_base_days)) * (2 ** (snoozed - 1)))
            eligible_at = (now + timedelta(days=days)).isoformat()
            status, resolved_at = "snoozed", None
        elif action == "ignore":
            ignored += 1
            days = max(1, int(ignore_suppress_days)) * min(3, ignored)
            suppress_until = (now + timedelta(days=days)).isoformat()
            status = "ignored"
        elif action == "never":
            never_prompt, status = 1, "never"
        self.conn.execute(
            "UPDATE reflection_prompt_event SET status=?,eligible_at=?,resolved_at=?,"
            "snooze_count=?,usefulness=?,burden=? WHERE id=?",
            (status, eligible_at, resolved_at, snoozed, useful, heavy, int(event_id)))
        self.conn.execute(
            "INSERT INTO reflection_prompt_preference "
            "(kind,subject_key,ignored_count,snooze_count,suppress_until,never_prompt,last_feedback_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,subject_key) DO UPDATE SET "
            "ignored_count=excluded.ignored_count,snooze_count=excluded.snooze_count,"
            "suppress_until=COALESCE(excluded.suppress_until,reflection_prompt_preference.suppress_until),"
            "never_prompt=MAX(reflection_prompt_preference.never_prompt,excluded.never_prompt),"
            "last_feedback_at=excluded.last_feedback_at",
            (row["kind"], row["subject_key"], ignored, snoozed, suppress_until,
             never_prompt, now.isoformat()))
        self.conn.commit()
        return {"ok": True, "event_id": int(event_id), "status": status,
                "eligible_at": eligible_at, "suppress_until": suppress_until}

    def snapshot(self, limit: int = 12) -> dict:
        rows = [dict(row) for row in self.conn.execute(
            "SELECT * FROM reflection_prompt_event ORDER BY id DESC LIMIT ?", (int(limit),))]
        pending = sum(row["status"] in {"pending", "snoozed"} for row in rows)
        last_shown = next((row["shown_at"] for row in rows if row["shown_at"]), None)
        return {"events": rows, "pending": pending, "last_shown_at": last_shown}
