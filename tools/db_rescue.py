"""Check or rescue Faerie Fire's local SQLite databases.

Usage:
    python tools/db_rescue.py
    python tools/db_rescue.py --unlock

This prints metadata only: lock status, file sizes, PRAGMA health, and likely
process holders. It does not read private memory/goal/chat payloads.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.config import load  # noqa: E402
from livingpc.db_rescue import database_status, rescue_databases  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unlock", action="store_true",
                        help="attempt safe WAL checkpoint + write-lock probe")
    args = parser.parse_args(argv)
    cfg = load("config.toml")
    result = rescue_databases(cfg) if args.unlock else database_status(cfg)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
