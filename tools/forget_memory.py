"""Forget one memory fact and remove it from backups and configured mirrors."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from livingpc.config import load
from livingpc.forget import forget_memory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("memory_id", type=int)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--keep-backups", action="store_true")
    parser.add_argument("--skip-mirrors", action="store_true")
    parser.add_argument("--yes", action="store_true",
                        help="required confirmation for destructive deletion")
    args = parser.parse_args()
    if not args.yes:
        raise SystemExit("Refusing to delete without --yes")
    result = forget_memory(
        load(args.config), args.memory_id,
        purge_backups=not args.keep_backups,
        sync_notion=not args.skip_mirrors,
    )
    print(
        f"Forgot memory {result['memory_id']}; removed "
        f"{result['backups_removed']} backup(s)."
    )
    if result["warnings"]:
        print("Mirror warnings: " + ", ".join(result["warnings"]))


if __name__ == "__main__":
    main()
