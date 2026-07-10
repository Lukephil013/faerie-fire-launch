"""Create a shareable diagnostics bundle for capture/tray coordination.

The bundle excludes payloads: no OCR text, clipboard contents, browser URLs,
screenshots, or database files are copied.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import zipfile
from datetime import datetime

from livingpc.config import load
from livingpc.diagnostics import (
    APP_DIR,
    DIAG_DIR,
    DIAG_LOG,
    event_summary,
    process_summary,
    python_processes,
    read_pid_file,
    runtime_header,
)
from livingpc.lockfile import is_running
from livingpc.service import LOCK_PATH, STOP_PATH


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _tail(path: str, max_lines: int = 400) -> str:
    if not os.path.exists(path):
        return f"missing: {path}\n"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except Exception as ex:
        return f"could not read {path}: {type(ex).__name__}: {ex}\n"


def _recent_event_metadata(db_path: str, limit: int = 80) -> list[str]:
    if not os.path.exists(db_path):
        return [f"database missing: {db_path}"]
    try:
        from livingpc.db import connect as db_connect
        conn = db_connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, ts, type, app,
                       CASE WHEN blob_ref IS NULL THEN 0 ELSE 1 END AS has_blob,
                       CASE WHEN text_payload IS NULL THEN 0 ELSE length(text_payload) END AS payload_len
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as ex:
        return [f"could not read events: {type(ex).__name__}: {ex}"]
    if not rows:
        return ["no events"]
    return [
        "id={id} ts={ts} type={type} app={app} has_blob={has_blob} payload_len={payload_len}".format(
            id=r["id"],
            ts=r["ts"],
            type=r["type"],
            app=r["app"] or "",
            has_blob=r["has_blob"],
            payload_len=r["payload_len"],
        )
        for r in rows
    ]


def main() -> None:
    cfg = load("config.toml")
    os.makedirs(DIAG_DIR, exist_ok=True)
    bundle_dir = os.path.join(DIAG_DIR, "bundle_" + _stamp())
    os.makedirs(bundle_dir, exist_ok=True)

    lock_pid = read_pid_file(LOCK_PATH)
    tray_lock_path = os.path.join(APP_DIR, "tray.lock")
    tray_pid = read_pid_file(tray_lock_path)

    lines = []
    lines.extend(runtime_header())
    lines.extend(
        [
            "",
            "[paths]",
            f"config_db_path={os.path.abspath(cfg.db_path)}",
            f"blob_dir={os.path.abspath(cfg.blob_dir)}",
            f"capture_lock_path={LOCK_PATH}",
            f"capture_stop_path={STOP_PATH}",
            f"tray_lock_path={tray_lock_path}",
            "",
            "[capture_lock]",
            f"running={is_running(LOCK_PATH)}",
            f"owner={process_summary(lock_pid)}",
            f"stop_file_present={os.path.exists(STOP_PATH)}",
            "",
            "[tray_lock]",
            f"running={is_running(tray_lock_path)}",
            f"owner={process_summary(tray_pid)}",
            "",
            "[config]",
            f"tick={cfg.tick}",
            f"idle_limit={cfg.idle_limit}",
            f"max_interval={cfg.max_interval}",
            f"ocr_enabled={cfg.ocr_enabled}",
            f"browser_history_enabled={cfg.browser_history_enabled}",
            f"browser_poll_seconds={cfg.browser_poll_seconds}",
            f"clipboard_enabled={cfg.clipboard_enabled}",
            f"clipboard_poll_seconds={cfg.clipboard_poll_seconds}",
            f"blocklist={','.join(cfg.blocklist)}",
            "",
            "[event_summary]",
        ]
    )
    lines.extend(event_summary(cfg.db_path))
    lines.extend(["", "[recent_event_metadata]"])
    lines.extend(_recent_event_metadata(cfg.db_path))
    lines.extend(["", "[python_processes]"])
    lines.extend(python_processes())
    _write(os.path.join(bundle_dir, "summary.txt"), "\n".join(lines) + "\n")

    _write(os.path.join(bundle_dir, "capture_debug_tail.log"), _tail(DIAG_LOG))
    companion_log = os.path.join(APP_DIR, "livingpc", "companion", "companion_error.log")
    _write(os.path.join(bundle_dir, "companion_error_tail.log"), _tail(companion_log))

    zip_path = bundle_dir + ".zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in os.listdir(bundle_dir):
            z.write(os.path.join(bundle_dir, name), arcname=name)

    shutil.rmtree(bundle_dir, ignore_errors=True)
    print("Diagnostics bundle created:")
    print(zip_path)
    print("")
    print("Send me that zip, or paste summary.txt plus capture_debug_tail.log.")


if __name__ == "__main__":
    main()
