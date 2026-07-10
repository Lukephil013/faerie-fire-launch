"""CLI: snapshot memory.db into the rotating backup set.

Thin wrapper around `livingpc.backup.backup_memory`. The nightly pass does this
automatically; run it by hand before anything risky.

Run:
    python tools/backup_memory.py                 # -> data/backups/, keep 14
    python tools/backup_memory.py --keep 30
    python tools/backup_memory.py --dir "D:/Backups/FaerieFire"
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from livingpc.backup import backup_memory, default_backup_dir  # noqa: E402
from livingpc.config import load  # noqa: E402


def main() -> None:
    cfg = load("config.toml")
    p = argparse.ArgumentParser(description="snapshot memory.db (rotating backups)")
    p.add_argument("--memory-db", default=cfg.memory_db_path)
    p.add_argument("--dir", default=cfg.backup_dir or default_backup_dir(cfg.memory_db_path))
    p.add_argument("--keep", type=int, default=cfg.backup_keep)
    args = p.parse_args()
    if not os.path.exists(args.memory_db):
        print(f"[X] memory db not found: {args.memory_db}")
        return
    result = backup_memory(args.memory_db, args.dir, keep=args.keep)
    print(f"[backup] {result['path']}")
    print(f"  {result['kept']} snapshot(s) kept, {result['pruned']} pruned.")
    if result["salt_copied"]:
        print("  secret.salt copied alongside (needed to restore encrypted data).")


if __name__ == "__main__":
    main()
