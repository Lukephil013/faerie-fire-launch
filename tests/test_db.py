"""WAL connection factory: journal mode, busy timeout, and cross-connection concurrency."""
import threading
import time

from livingpc import diagnostics
from livingpc.db import checkpoint, connect


def test_connect_enables_wal_and_busy_timeout(tmp_path):
    path = str(tmp_path / "sample.db")
    conn = connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    finally:
        conn.close()


def test_wal_persists_in_database_file(tmp_path):
    path = str(tmp_path / "sample.db")
    connect(path).close()
    import sqlite3
    plain = sqlite3.connect(path)
    try:
        assert plain.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        plain.close()


def test_open_reader_does_not_block_writer(tmp_path):
    """In delete-journal mode this write raises 'database is locked'; WAL must not."""
    path = str(tmp_path / "sample.db")
    setup = connect(path)
    setup.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    setup.execute("INSERT INTO t (v) VALUES ('seed')")
    setup.commit()

    reader = connect(path)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    errors = []

    def write():
        try:
            writer = connect(path, timeout=5.0)
            writer.execute("INSERT INTO t (v) VALUES ('concurrent')")
            writer.commit()
            writer.close()
        except Exception as error:  # pragma: no cover - failure detail
            errors.append(error)

    thread = threading.Thread(target=write)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors

    reader.rollback()
    reader.close()
    check = connect(path)
    try:
        assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        checkpoint(check)
    finally:
        check.close()
    setup.close()


def test_long_transaction_holder_is_logged(tmp_path, monkeypatch):
    """The holder-side watchdog should name long write transactions."""
    monkeypatch.setattr(diagnostics, "DIAG_DIR", str(tmp_path))
    log_path = tmp_path / "capture_debug.log"
    monkeypatch.setattr(diagnostics, "DIAG_LOG", str(log_path))

    path = str(tmp_path / "sample.db")
    conn = connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.commit()
        conn.execute("INSERT INTO t (v) VALUES ('held')")
        conn._ff_tx_started_at = time.monotonic() - 6.0
        conn._ff_tx_first_verb = "INSERT"
        conn._ff_tx_last_verb = "INSERT"
        conn.commit()
    finally:
        conn.close()

    text = log_path.read_text(encoding="utf-8")
    assert "[db-tx]" in text
    assert "sample.db" in text
    assert "transaction held" in text
    assert "first=INSERT" in text
