"""Explicit forgetting across the source database, backups, and configured mirrors."""
from __future__ import annotations

import os
import re

from .backup import default_backup_dir
from .memory import MemoryStore
from .maintenance import maintenance_lock

_SNAPSHOT_RE = re.compile(r"^memory-\d{8}-\d{6}\.db$")


def _purge_memory_backups(memory_db_path: str, backup_dir: str | None = None) -> int:
    directory = backup_dir or default_backup_dir(memory_db_path)
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for name in os.listdir(directory):
        if _SNAPSHOT_RE.match(name):
            os.remove(os.path.join(directory, name))
            removed += 1
    # Salt/key copies belong to the rotating snapshot set. Once explicit
    # forgetting removes every snapshot, retaining backup-only key material is
    # unnecessary and makes the backup directory look non-purged.
    if not any(_SNAPSHOT_RE.match(name) for name in os.listdir(directory)):
        for name in ("secret.salt", "secret.key"):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                os.remove(path)
    return removed


def forget_memory(cfg, memory_id: int, *, purge_backups: bool = True,
                  sync_notion: bool = True) -> dict:
    """Serialize explicit deletion against portable backup and restore."""
    with maintenance_lock():
        return _forget_memory_locked(
            cfg, memory_id, purge_backups=purge_backups,
            sync_notion=sync_notion)


def _forget_memory_locked(cfg, memory_id: int, *, purge_backups: bool = True,
                          sync_notion: bool = True) -> dict:
    """Forget a fact locally, then rebuild downstream mirrors without it.

    Mirror failures are returned as warnings: the source deletion is never
    rolled back merely because an optional export is offline.
    """
    mem = MemoryStore(cfg.memory_db_path)
    try:
        forgotten = mem.forget(int(memory_id))
    finally:
        mem.close()

    result = {**forgotten, "backups_removed": 0, "warnings": []}
    result["goal_evidence_removed"] = 0
    result["goal_ai_candidates_removed"] = 0
    try:
        from .goals import GoalStore
        goals = GoalStore(cfg.memory_db_path)
        try:
            cur = goals.conn.execute(
                "DELETE FROM goal_evidence_link WHERE source_kind='memory' AND source_id=?",
                (str(int(memory_id)),))
            goals.conn.commit()
            result["goal_evidence_removed"] = int(cur.rowcount)
            candidate_table = goals.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='goal_agent_memory_candidate'").fetchone()
            if candidate_table:
                removed = goals.conn.execute(
                    "DELETE FROM goal_agent_memory_candidate WHERE memory_id=?",
                    (int(memory_id),))
                goals.conn.commit()
                result["goal_ai_candidates_removed"] = int(removed.rowcount)
        finally:
            goals.close()
    except Exception as error:
        result["warnings"].append(f"goals:{type(error).__name__}")
    result["source_events_removed"] = 0
    try:
        from .storage import EventLog
        events = EventLog(cfg.db_path)
        try:
            for ref in forgotten.get("source_refs", []):
                if (ref.get("kind") == "triage" and ref.get("window_start")
                        and ref.get("window_end")):
                    removed = events.forget_window(
                        ref["window_start"], ref["window_end"])
                    result["source_events_removed"] += removed["events"]
        finally:
            events.close()
    except Exception as error:
        result["warnings"].append(f"events:{type(error).__name__}")
    if purge_backups:
        result["backups_removed"] = _purge_memory_backups(
            cfg.memory_db_path, getattr(cfg, "backup_dir", "") or None)

    result.update({
        "managed_backups_removed": 0,
        "backup_privacy_epoch": None,
        "backup_purge_pending": [],
        "post_forget_baseline": False,
    })
    managed_configured = bool(
        getattr(cfg, "instance_backup_enabled", False)
        or getattr(cfg, "instance_backup_primary_dir", "")
        or getattr(cfg, "instance_backup_secondary_dir", ""))
    if managed_configured:
        try:
            from .instance_backup import (
                backup_status,
                create_instance_backup,
                purge_managed_backups,
            )
            status = backup_status(cfg)
            current_epoch = int(getattr(status, "privacy_epoch", 0) or 0)
            new_epoch = current_epoch + 1
            purge = purge_managed_backups(cfg, new_epoch)
            result["backup_privacy_epoch"] = int(
                getattr(purge, "privacy_epoch", new_epoch) or new_epoch)
            result["managed_backups_removed"] = int(
                getattr(purge, "removed", getattr(purge, "removed_count", 0)) or 0)
            pending = getattr(purge, "pending_destinations", ())
            if not pending and bool(getattr(purge, "purge_pending", False)):
                pending = ("destination",)
            result["backup_purge_pending"] = list(pending or ())
            if not bool(getattr(purge, "ok", False)):
                result["warnings"].append(
                    "instance_backup_purge:" + str(
                        getattr(purge, "error_code", "failed") or "failed"))
            elif result["backup_purge_pending"]:
                result["warnings"].append("instance_backup_purge:pending")
            elif getattr(cfg, "instance_backup_enabled", False):
                baseline = create_instance_backup(cfg, reason="post_forget")
                result["post_forget_baseline"] = bool(
                    getattr(baseline, "ok", False)
                    and getattr(baseline, "verified", True))
                if not result["post_forget_baseline"]:
                    result["warnings"].append(
                        "instance_backup_baseline:" + str(
                            getattr(baseline, "error_code", "failed") or "failed"))
        except Exception as error:
            result["warnings"].append(
                f"instance_backup:{type(error).__name__}")

    if sync_notion and getattr(cfg, "notion_sync_enabled", False):
        try:
            from .curiosity import CuriosityStore, get_curiosity_model
            from .inference import InferenceStore
            from .notion_sync import sync_curiosity_to_notion
            mem = MemoryStore(cfg.memory_db_path)
            inf = InferenceStore(cfg.memory_db_path)
            curiosities = CuriosityStore(cfg.memory_db_path)
            try:
                model = get_curiosity_model(cfg)
                for row in curiosities.list_curiosities(status="active"):
                    sync_curiosity_to_notion(
                        cfg, mem, inf, curiosities, row["id"], model)
            finally:
                curiosities.close()
                inf.close()
                mem.close()
        except Exception as error:
            result["warnings"].append(f"notion:{type(error).__name__}")
    return result
