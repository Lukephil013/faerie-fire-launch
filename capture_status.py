"""Print the real capture status: when the last capture actually happened,
plus whether a capture process holds the lock. Truthful even across processes.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from livingpc.config import load
from livingpc.storage import EventLog
from livingpc.lockfile import is_running
from livingpc.service import LOCK_PATH, STOP_PATH
from livingpc.diagnostics import event_summary, process_summary, read_pid_file


def _age_seconds(ts_text: str | None) -> float | None:
    if not ts_text:
        return None
    ts = datetime.fromisoformat(ts_text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


def main():
    cfg = load("config.toml")
    screen_age = None
    any_age = None
    any_type = None
    try:
        e = EventLog(cfg.db_path)
        row = e.conn.execute("""
            SELECT ts FROM events
            WHERE type = 'ocr'
            ORDER BY id DESC LIMIT 1
        """).fetchone()
        if row and row[0]:
            screen_age = _age_seconds(row[0])
        row = e.conn.execute("SELECT ts, type FROM events ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[0]:
            any_age = _age_seconds(row[0])
            any_type = row[1]
        e.close()
    except Exception as ex:
        print("Could not read the event log:", ex)

    lock = is_running(LOCK_PATH)
    lock_pid = read_pid_file(LOCK_PATH)
    print(f"Capture process holding the lock: {'YES' if lock else 'no'}")
    print(f"Lock path: {LOCK_PATH}")
    print(f"Lock owner: {process_summary(lock_pid)}")
    print(f"Stop signal present: {'YES' if os.path.exists(STOP_PATH) else 'no'}")
    print("")

    active_window = max(12, int(cfg.tick * 3))
    quiet_window = int(cfg.max_interval + cfg.tick * 3)
    if screen_age is None:
        print("No screen/OCR captures recorded yet.")
    elif lock and screen_age <= active_window:
        print(f"==> SCREEN CAPTURING NOW — last screen capture {int(screen_age)}s ago.")
    elif lock and screen_age <= quiet_window:
        print(f"==> RUNNING, SCREEN QUIET — last screen capture {int(screen_age)}s ago.")
        print("   (This is what Pause/idle looks like unless the OCR timestamp keeps updating.)")
    elif lock:
        m, s = int(screen_age // 60), int(screen_age % 60)
        print(f"==> RUNNING, NO RECENT SCREEN CAPTURE — last screen capture {m}m {s}s ago.")
        print("   (Likely paused or idle. If you are active and unpaused, capture is stuck.)")
    else:
        m, s = int(screen_age // 60), int(screen_age % 60)
        print(f"Last screen capture was {m}m {s}s ago.")
        print("   (Capture is stopped: no live process owns the capture lock.)")
    if any_age is not None and any_type != "ocr":
        print(f"Newest event overall: {any_type}, {int(any_age)}s ago.")
        print("   (Browser/clipboard events can update while screen capture is paused.)")

    print("")
    print("Event summary by type:")
    for line in event_summary(cfg.db_path):
        print("  " + line)


if __name__ == "__main__":
    main()
