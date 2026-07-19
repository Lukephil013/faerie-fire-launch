"""Decide when to nudge a user who has real data but no portable backup yet.

The prompt is a one-time onboarding aid, not a nag: it appears only after the
profile holds enough memories, chats, or investigations to hurt if lost, and
the user can snooze it or turn it off permanently. All prompt state lives in
the memory database's ``meta`` table so it travels with the profile.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


MEMORY_THRESHOLD = 10
CHAT_MESSAGE_THRESHOLD = 15
INVESTIGATION_ITEM_THRESHOLD = 12
SNOOZE_DAYS = 3

_DISMISSED_KEY = "backup_prompt_dismissed"
_SNOOZED_UNTIL_KEY = "backup_prompt_snoozed_until"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _count(connection: sqlite3.Connection, query: str) -> int:
    try:
        row = connection.execute(query).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0


def _read_meta(connection: sqlite3.Connection, key: str) -> str:
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row and row[0] is not None else ""
    except sqlite3.Error:
        return ""


def data_counts(cfg) -> dict:
    """Privacy-safe row counts of the content worth protecting."""
    counts = {"memories": 0, "chat_messages": 0, "investigation_items": 0}
    path = os.path.abspath(cfg.memory_db_path)
    if not os.path.isfile(path):
        return counts
    try:
        connection = sqlite3.connect(
            f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return counts
    try:
        counts["memories"] = _count(
            connection, "SELECT COUNT(*) FROM memory WHERE status='active'")
        counts["chat_messages"] = _count(
            connection, "SELECT COUNT(*) FROM companion_message WHERE role='user'")
        counts["investigation_items"] = _count(
            connection, "SELECT COUNT(*) FROM curiosity_item")
    finally:
        connection.close()
    return counts


def threshold_met(counts: dict) -> bool:
    return (int(counts.get("memories", 0)) >= MEMORY_THRESHOLD
            or int(counts.get("chat_messages", 0)) >= CHAT_MESSAGE_THRESHOLD
            or int(counts.get("investigation_items", 0))
            >= INVESTIGATION_ITEM_THRESHOLD)


def backup_configured(cfg) -> bool:
    return (bool(getattr(cfg, "instance_backup_enabled", False))
            or bool(str(getattr(cfg, "instance_backup_primary_dir", "")
                        or "").strip()))


def prompt_state(cfg) -> dict:
    """Return whether the backup nudge should be shown right now."""
    counts = data_counts(cfg)
    state = {
        "ok": True,
        "show": False,
        "configured": backup_configured(cfg),
        "dismissed": False,
        "snoozed_until": "",
        "counts": counts,
    }
    if state["configured"] or not threshold_met(counts):
        return state
    path = os.path.abspath(cfg.memory_db_path)
    if not os.path.isfile(path):
        return state
    try:
        connection = sqlite3.connect(
            f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return state
    try:
        state["dismissed"] = _read_meta(connection, _DISMISSED_KEY) == "1"
        state["snoozed_until"] = _read_meta(connection, _SNOOZED_UNTIL_KEY)
    finally:
        connection.close()
    if state["dismissed"]:
        return state
    snoozed_until = _parse_time(state["snoozed_until"])
    if snoozed_until is not None and _utc_now() < snoozed_until:
        return state
    state["show"] = True
    return state


def _write_meta(cfg, key: str, value: str) -> bool:
    path = os.path.abspath(cfg.memory_db_path)
    if not os.path.isfile(path):
        return False
    try:
        connection = sqlite3.connect(path, timeout=10)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value))
            connection.commit()
        finally:
            connection.close()
        return True
    except sqlite3.Error:
        return False


def snooze_prompt(cfg, days: int = SNOOZE_DAYS) -> dict:
    until = (_utc_now() + timedelta(days=max(1, int(days))))
    stamp = until.isoformat(timespec="seconds").replace("+00:00", "Z")
    ok = _write_meta(cfg, _SNOOZED_UNTIL_KEY, stamp)
    return {"ok": ok, "snoozed_until": stamp if ok else ""}


def dismiss_prompt(cfg) -> dict:
    return {"ok": _write_meta(cfg, _DISMISSED_KEY, "1")}
