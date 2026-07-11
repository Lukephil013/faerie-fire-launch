"""SQLite event log + focus-session grouping.

Two tables:
  * sessions -- contiguous spans where one window stayed in foreground.
  * events   -- timestamped activity rows (window changes, OCR captures, etc.).

Lightweight rows are kept long-term; screenshot blobs referenced by `blob_ref`
are purged after triage via purge_blobs(). This is platform-independent and
fully testable off-Windows.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from . import crypto
from .db import connect as db_connect


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    app           TEXT,
    window_title  TEXT,
    start_ts      TEXT NOT NULL,
    end_ts        TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    type          TEXT NOT NULL,          -- 'window' | 'ocr' | 'browser' | 'clipboard'
    app           TEXT,
    window_title  TEXT,
    text_payload  TEXT,
    blob_ref      TEXT,
    session_id    INTEGER,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class EventLog:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.conn = db_connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- sessions ---------------------------------------------------------
    def start_session(self, app: str, title: str, ts: str | None = None) -> int:
        ts = ts or now_iso()
        cur = self.conn.execute(
            "INSERT INTO sessions (app, window_title, start_ts) VALUES (?, ?, ?)",
            (app, crypto.enc(title), ts),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def end_session(self, session_id: int, ts: str | None = None) -> None:
        ts = ts or now_iso()
        self.conn.execute(
            "UPDATE sessions SET end_ts = ? WHERE id = ? AND end_ts IS NULL",
            (ts, session_id),
        )
        self.conn.commit()

    # --- events -----------------------------------------------------------
    def log_event(
        self,
        type: str,
        app: str | None = None,
        window_title: str | None = None,
        text_payload: str | None = None,
        blob_ref: str | None = None,
        session_id: int | None = None,
        ts: str | None = None,
    ) -> int:
        ts = ts or now_iso()
        cur = self.conn.execute(
            "INSERT INTO events (ts, type, app, window_title, text_payload, blob_ref, session_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            # OCR text is the sensitive payload -> encrypt at rest (no-op without a key).
            (ts, type, app, crypto.enc(window_title), crypto.enc(text_payload),
             blob_ref, session_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # --- maintenance ------------------------------------------------------
    def purge_blobs(self, before_ts: str | None = None, unlink: bool = True) -> int:
        """Delete screenshot files and null their blob_ref. Text payloads stay.

        Called after a daily triage pass consumes the images. Returns the number
        of blobs purged.
        """
        clause = "blob_ref IS NOT NULL"
        params: tuple = ()
        if before_ts is not None:
            clause += " AND ts < ?"
            params = (before_ts,)
        rows = self.conn.execute(
            f"SELECT id, blob_ref FROM events WHERE {clause}", params
        ).fetchall()
        for row in rows:
            path = row["blob_ref"]
            if unlink and path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self.conn.execute(
            f"UPDATE events SET blob_ref = NULL WHERE {clause}", params
        )
        self.conn.commit()
        return len(rows)

    def forget_window(self, start: str, end: str) -> dict:
        """Delete an explicitly forgotten source window, including its blobs."""
        sessions = self.conn.execute(
            "SELECT id FROM sessions WHERE start_ts < ? "
            "AND COALESCE(end_ts, start_ts) >= ?", (end, start),
        ).fetchall()
        session_ids = [row["id"] for row in sessions]
        clauses = ["(ts >= ? AND ts <= ?)"]
        params: list[object] = [start, end]
        if session_ids:
            clauses.append("session_id IN (%s)" % ",".join("?" for _ in session_ids))
            params.extend(session_ids)
        where = " OR ".join(clauses)
        rows = self.conn.execute(
            f"SELECT blob_ref FROM events WHERE {where}", params,
        ).fetchall()
        for row in rows:
            path = row["blob_ref"]
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        removed_events = self.conn.execute(
            f"DELETE FROM events WHERE {where}", params,
        ).rowcount
        removed_sessions = 0
        if session_ids:
            removed_sessions = self.conn.execute(
                "DELETE FROM sessions WHERE id IN (%s)" %
                ",".join("?" for _ in session_ids), session_ids,
            ).rowcount
        self.conn.commit()
        return {"events": removed_events, "sessions": removed_sessions,
                "blobs": sum(1 for row in rows if row["blob_ref"])}

    # --- read helpers (handy for inspection / future triage) -------------
    def count(self, type: str | None = None) -> int:
        if type is None:
            return int(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE type = ?", (type,)
            ).fetchone()[0]
        )

    def recent_sessions(self, limit: int = 20):
        return self.conn.execute(
            "SELECT * FROM sessions ORDER BY start_ts DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- meta (collector watermarks etc.) --------------------------------
    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
