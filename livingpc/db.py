"""Shared SQLite connection factory: WAL journaling plus a busy timeout.

Every process in Faerie Fire (GUI, native agent windows, tray service with
its inference/curiosity/nightly cycles) opens the same database files with
long-lived connections. In SQLite's default delete-journal mode one writer
blocks every reader and one open reader blocks the writer, which is why
"database is locked" errors were endemic. WAL (write-ahead logging) lets
readers and the single writer proceed without blocking each other; the busy
timeout absorbs the remaining writer-vs-writer contention.

All ``sqlite3.connect`` call sites for shared databases must go through
:func:`connect`. The WAL setting persists inside the database file, but
applying it on every connect is cheap and keeps behaviour explicit.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

_SLOW_SECONDS = 5.0
_WRITE_VERBS = {
    "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER",
    "BEGIN", "VACUUM", "REINDEX",
}


def _verb(sql) -> str:
    return str(sql).lstrip().split(None, 1)[0][:16].upper() if sql else "?"


def _log_slow(db_name: str, sql, elapsed: float) -> None:
    """Metadata-only slow-statement log: db file, first SQL keyword, seconds.

    Never logs parameters, values, or full statements (privacy invariant).
    """
    try:
        from .diagnostics import log_diag
        log_diag("db-slow", f"{db_name}: {_verb(sql)} waited {elapsed:.1f}s "
                 f"thread={threading.get_ident()} "
                 f"(another connection held the write lock)")
    except Exception:
        pass


def _log_long_transaction(db_name: str, action: str, elapsed: float,
                          first_verb: str, last_verb: str) -> None:
    """Metadata-only transaction holder log.

    This names connections that *held* a write transaction across slow work.
    It intentionally logs only file name, SQL verbs, duration, and thread id.
    """
    try:
        from .diagnostics import log_diag
        log_diag(
            "db-tx",
            f"{db_name}: transaction held {elapsed:.1f}s before {action} "
            f"first={first_verb or '?'} last={last_verb or '?'} "
            f"thread={threading.get_ident()}",
        )
    except Exception:
        pass


class _TimedConnection(sqlite3.Connection):
    """Connection that reports statements blocked behind the write lock."""

    _ff_name = "?"
    _ff_tx_started_at = None
    _ff_tx_first_verb = ""
    _ff_tx_last_verb = ""

    def _note_statement(self, sql) -> None:
        verb = _verb(sql)
        if verb not in _WRITE_VERBS:
            return
        self._ff_tx_last_verb = verb
        if self.in_transaction and self._ff_tx_started_at is None:
            self._ff_tx_started_at = time.monotonic()
            self._ff_tx_first_verb = verb

    def _log_transaction_age(self, action: str) -> None:
        started = self._ff_tx_started_at
        if started is None:
            return
        elapsed = time.monotonic() - started
        if elapsed > _SLOW_SECONDS:
            _log_long_transaction(
                self._ff_name, action, elapsed,
                self._ff_tx_first_verb, self._ff_tx_last_verb)

    def _clear_transaction_age(self) -> None:
        self._ff_tx_started_at = None
        self._ff_tx_first_verb = ""
        self._ff_tx_last_verb = ""

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        started = time.monotonic()
        try:
            result = super().execute(sql, *args, **kwargs)
            self._note_statement(sql)
            return result
        finally:
            elapsed = time.monotonic() - started
            if elapsed > _SLOW_SECONDS:
                _log_slow(self._ff_name, sql, elapsed)

    def executemany(self, sql, *args, **kwargs):  # type: ignore[override]
        started = time.monotonic()
        try:
            result = super().executemany(sql, *args, **kwargs)
            self._note_statement(sql)
            return result
        finally:
            elapsed = time.monotonic() - started
            if elapsed > _SLOW_SECONDS:
                _log_slow(self._ff_name, sql, elapsed)

    def executescript(self, sql_script, *args, **kwargs):  # type: ignore[override]
        started = time.monotonic()
        try:
            result = super().executescript(sql_script, *args, **kwargs)
            self._note_statement(sql_script)
            return result
        finally:
            elapsed = time.monotonic() - started
            if elapsed > _SLOW_SECONDS:
                _log_slow(self._ff_name, sql_script, elapsed)

    def commit(self):  # type: ignore[override]
        self._log_transaction_age("COMMIT")
        started = time.monotonic()
        try:
            return super().commit()
        finally:
            elapsed = time.monotonic() - started
            if elapsed > _SLOW_SECONDS:
                _log_slow(self._ff_name, "COMMIT", elapsed)
            if not self.in_transaction:
                self._clear_transaction_age()

    def rollback(self):  # type: ignore[override]
        self._log_transaction_age("ROLLBACK")
        try:
            return super().rollback()
        finally:
            if not self.in_transaction:
                self._clear_transaction_age()

    def close(self):  # type: ignore[override]
        self._log_transaction_age("CLOSE")
        return super().close()


def connect(db_path: str, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Open ``db_path`` with WAL mode and ``timeout`` seconds of patience.

    Callers keep setting their own ``row_factory`` / ``foreign_keys`` so this
    stays a drop-in replacement for ``sqlite3.connect``.
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=timeout, factory=_TimedConnection)
    conn._ff_name = os.path.basename(db_path)
    conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout * 1000))}")
    mode = ""
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    except sqlite3.DatabaseError as error:
        mode = f"error: {error}"
    if str(mode).lower() != "wal":
        # Conversion needs a moment of exclusive access; a process holding the
        # DB open blocks it. Log loudly — silent delete-mode is how permanent
        # 'database is locked' errors hide.
        try:
            from .diagnostics import log_diag
            log_diag("db", f"{os.path.basename(db_path)} still journal_mode="
                     f"{mode!r} (WAL conversion blocked by another connection)")
        except Exception:
            pass
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold WAL content back into the main database file.

    Used before whole-file copies (backups) so the copy
    contains everything without needing the ``-wal`` sidecar.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        pass
