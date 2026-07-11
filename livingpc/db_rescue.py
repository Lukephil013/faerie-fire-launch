"""Self-service SQLite status and rescue helpers.

This module reports metadata only: file paths/sizes, lock status, PRAGMA status,
and process ids that appear to hold database files. It never reads private row
payloads from memory, goals, curiosities, OCR, browser history, or chat tables.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

from .diagnostics import APP_DIR, log_diag


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _db_files(path: str) -> list[str]:
    return [path, path + "-wal", path + "-shm", path + "-journal"]


def _file_info(path: str) -> dict[str, Any]:
    exists = os.path.exists(path)
    return {
        "path": os.path.abspath(path),
        "exists": exists,
        "size": os.path.getsize(path) if exists else 0,
    }


def _connect(path: str, *, timeout: float) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=timeout)
    conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout * 1000))}")
    return conn


def database_snapshot(path: str, *, timeout: float = 0.35) -> dict[str, Any]:
    """Return lock/readability metadata for one SQLite DB file."""
    snap: dict[str, Any] = {
        "path": os.path.abspath(path),
        "exists": os.path.exists(path),
        "files": [_file_info(file_path) for file_path in _db_files(path)],
        "ok": False,
        "locked": False,
        "error": "",
        "journal_mode": None,
        "locking_mode": None,
        "quick_check": None,
    }
    if not snap["exists"]:
        snap["ok"] = True
        return snap
    try:
        conn = _connect(path, timeout=timeout)
        try:
            snap["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
            snap["locking_mode"] = conn.execute("PRAGMA locking_mode").fetchone()[0]
            snap["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
            snap["ok"] = True
        finally:
            conn.close()
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        snap["error"] = message
        snap["locked"] = "locked" in str(error).lower() or "busy" in str(error).lower()
    return snap


def unlock_database(path: str, *, timeout: float = 1.5) -> dict[str, Any]:
    """Best-effort unlock/checkpoint operation for one SQLite DB.

    This cannot safely kill another writer. It can clear normal WAL backlog,
    release hot-journal state through SQLite itself, and prove whether a short
    write lock can be acquired now.
    """
    result: dict[str, Any] = {
        "path": os.path.abspath(path),
        "exists": os.path.exists(path),
        "ok": False,
        "locked": False,
        "actions": [],
        "error": "",
        "checkpoint": None,
        "quick_check": None,
    }
    if not result["exists"]:
        result["ok"] = True
        result["actions"].append("database file is missing; nothing to unlock")
        return result
    try:
        conn = _connect(path, timeout=timeout)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            result["checkpoint"] = list(row) if row is not None else None
            result["actions"].append("wal checkpoint requested")
            conn.execute("PRAGMA optimize")
            result["actions"].append("optimize requested")
            result["quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            result["actions"].append("write lock acquired and released")
            result["ok"] = True
        finally:
            conn.close()
    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        result["error"] = message
        result["locked"] = "locked" in str(error).lower() or "busy" in str(error).lower()
        log_diag("db-rescue", f"unlock failed path={os.path.basename(path)} error={type(error).__name__}")
    return result


def process_holders(paths: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Find likely Python/Faerie processes touching the app.

    On Windows, asking every process for open file handles can be surprisingly
    slow or hang behind antivirus/permission boundaries. Keep this fast and
    conservative: show related Python/Faerie processes by cwd/cmdline rather
    than blocking the rescue tool while trying to prove the exact handle owner.
    """
    open_file_holders: list[dict[str, Any]] = []
    related_python: list[dict[str, Any]] = []
    try:
        import psutil

        for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd", "status"]):
            try:
                info = proc.info
                cmdline = " ".join(info.get("cmdline") or [])
                cwd = info.get("cwd") or ""
                name = info.get("name") or ""
                if ("python" in name.lower() or "python" in cmdline.lower()) and (
                    _norm(cwd).startswith(_norm(APP_DIR)) or _norm(APP_DIR) in _norm(cmdline)
                ):
                    related_python.append({
                        "pid": info.get("pid"),
                        "name": name,
                        "status": info.get("status"),
                        "cwd": cwd,
                        "cmdline": cmdline,
                        "files": [],
                    })
            except Exception:
                continue
    except Exception as error:
        related_python.append({
            "pid": None,
            "name": "psutil unavailable",
            "status": type(error).__name__,
            "cwd": "",
            "cmdline": str(error),
            "files": [],
        })
    return {
        "open_file_holders": open_file_holders,
        "related_python_processes": related_python[:12],
    }


def database_status(config) -> dict[str, Any]:
    paths = [config.memory_db_path, config.db_path]
    return {
        "ok": True,
        "memory": database_snapshot(config.memory_db_path),
        "events": database_snapshot(config.db_path),
        "holders": process_holders(paths),
    }


def rescue_databases(config) -> dict[str, Any]:
    paths = [config.memory_db_path, config.db_path]
    memory = unlock_database(config.memory_db_path)
    events = unlock_database(config.db_path)
    status = database_status(config)
    ok = bool(memory.get("ok") and events.get("ok") and
              not status["memory"].get("locked") and not status["events"].get("locked"))
    return {"ok": ok, "memory": memory, "events": events,
            "status": status, "holders": status["holders"]}
