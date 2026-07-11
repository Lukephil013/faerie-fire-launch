"""Rotating backups of the second brain (memory.db).

memory.db is the one file in this project that cannot be regenerated — code is
in git, capture data re-accumulates, but approved facts and confirmed beliefs
are irreplaceable. This module snapshots it safely (SQLite online backup API,
so it works while the daemon holds the DB open) into a rotating set:

    data/backups/memory-YYYYMMDD-HHMMSS.db

The newest `keep` snapshots are retained; older ones are pruned. If a
`secret.salt` sits next to the DB (at-rest encryption), it is copied alongside
the snapshots once — without it an encrypted backup would be unrecoverable.

Runs automatically after the nightly pass (see inference_scheduler) and on
demand via `python tools/backup_memory.py`.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from datetime import datetime

from . import crypto
from .db import checkpoint, connect as db_connect

_SNAPSHOT_RE = re.compile(r"^memory-\d{8}-\d{6}\.db$")


def default_backup_dir(memory_db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(memory_db_path)), "backups")


def _snapshots(backup_dir: str) -> list[str]:
    """Snapshot filenames, oldest first (name order == time order)."""
    if not os.path.isdir(backup_dir):
        return []
    return sorted(n for n in os.listdir(backup_dir) if _SNAPSHOT_RE.match(n))


def backup_memory(memory_db_path: str, backup_dir: str | None = None,
                  *, keep: int = 14, now: datetime | None = None) -> dict:
    """Snapshot memory.db into backup_dir and prune old snapshots.

    Returns {"path", "kept", "pruned", "salt_copied"}. Raises on failure —
    callers that must never die (the scheduler) wrap this best-effort.
    """
    if not os.path.exists(memory_db_path):
        raise FileNotFoundError(f"memory db not found: {memory_db_path}")
    backup_dir = backup_dir or default_backup_dir(memory_db_path)
    os.makedirs(backup_dir, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(backup_dir, f"memory-{stamp}.db")

    src = db_connect(memory_db_path)
    try:
        checkpoint(src)  # fold WAL into the main file so the snapshot is complete
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)   # online backup: consistent even while in use
        finally:
            dst.close()
    finally:
        src.close()

    # Keep the encryption salt with the backups; snapshots of an encrypted DB
    # are useless without it.
    salt_copied = False
    salt_dst = os.path.join(backup_dir, "secret.salt")
    salt_candidates = [
        os.path.join(os.path.dirname(os.path.abspath(memory_db_path)), "secret.salt"),
        crypto.salt_path(),
    ]
    if not os.path.exists(salt_dst):
        salt_src = next((path for path in salt_candidates if os.path.exists(path)), None)
        if salt_src:
            shutil.copy2(salt_src, salt_dst)
            salt_copied = True

    # The automatic key is DPAPI-protected for the current Windows user.
    key_src = crypto.automatic_key_path()
    key_dst = os.path.join(backup_dir, "secret.key")
    if crypto.enabled() and os.path.exists(key_src) and not os.path.exists(key_dst):
        shutil.copy2(key_src, key_dst)

    pruned = 0
    snapshots = _snapshots(backup_dir)
    keep = max(1, int(keep))
    for name in snapshots[:-keep]:
        os.remove(os.path.join(backup_dir, name))
        pruned += 1

    return {"path": dest, "kept": min(len(snapshots), keep),
            "pruned": pruned, "salt_copied": salt_copied}
