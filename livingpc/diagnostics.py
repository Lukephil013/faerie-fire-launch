"""Operational diagnostics for capture/tray coordination.

This file intentionally logs process/state metadata only. It does not include
OCR text, clipboard contents, URLs, screenshots, or window titles.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIAG_DIR = os.path.join(APP_DIR, "diagnostics")
DIAG_LOG = os.path.join(DIAG_DIR, "capture_debug.log")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_diag(source: str, message: str) -> None:
    """Append one diagnostic line, best-effort and non-fatal."""
    try:
        os.makedirs(DIAG_DIR, exist_ok=True)
        with open(DIAG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{utc_now()} pid={os.getpid()} [{source}] {message}\n")
    except Exception:
        pass


def read_pid_file(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
        return int(raw) if raw else None
    except Exception:
        return None


def process_summary(pid: int | None) -> str:
    if not pid:
        return "none"
    try:
        import psutil

        p = psutil.Process(pid)
        cmdline = " ".join(p.cmdline())
        return (
            f"pid={pid} name={p.name()} status={p.status()} "
            f"cwd={safe_call(p.cwd)} cmdline={cmdline}"
        )
    except Exception as ex:
        return f"pid={pid} ({type(ex).__name__}: {ex})"


def safe_call(fn):
    try:
        return fn()
    except Exception:
        return "unknown"


def python_processes() -> list[str]:
    """Return concise summaries of live Python-ish processes."""
    rows: list[str] = []
    try:
        import psutil

        for p in psutil.process_iter(["pid", "name", "cmdline", "cwd", "status"]):
            try:
                name = (p.info.get("name") or "").lower()
                cmd = " ".join(p.info.get("cmdline") or [])
                if "python" not in name and "python" not in cmd.lower():
                    continue
                rows.append(
                    f"pid={p.info['pid']} name={p.info.get('name')} "
                    f"status={p.info.get('status')} cwd={p.info.get('cwd')} "
                    f"cmdline={cmd}"
                )
            except Exception:
                continue
    except Exception as ex:
        rows.append(f"could not inspect processes: {type(ex).__name__}: {ex}")
    return rows


def event_summary(db_path: str) -> list[str]:
    """Return counts and latest timestamps by event type, without payloads."""
    if not os.path.exists(db_path):
        return [f"database missing: {db_path}"]
    try:
        from .db import connect as db_connect
        conn = db_connect(db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT type, COUNT(*) AS n, MAX(ts) AS latest "
                "FROM events GROUP BY type ORDER BY type"
            ).fetchall()
            if not rows:
                return ["events table is empty"]
            return [f"{r['type']}: count={r['n']} latest={r['latest']}" for r in rows]
        finally:
            conn.close()
    except Exception as ex:
        return [f"could not read events: {type(ex).__name__}: {ex}"]


def runtime_header() -> list[str]:
    return [
        f"time_utc={utc_now()}",
        f"app_dir={APP_DIR}",
        f"python={sys.executable}",
        f"pid={os.getpid()}",
    ]
