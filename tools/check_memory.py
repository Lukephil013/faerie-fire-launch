"""CLI: quick health + import verification for memory.db.

Prints database integrity, the journal-import watermark, and what the import
actually committed (counts by category and by month), plus overall memory
sizes. Read-only.

Run:  python tools/check_memory.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from livingpc.config import load  # noqa: E402
from livingpc.consolidate import report  # noqa: E402
from livingpc.journal_import import WATERMARK_KEY  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402


def main() -> None:
    cfg = load("config.toml")
    if not os.path.exists(cfg.memory_db_path):
        print(f"[X] memory db not found: {cfg.memory_db_path}")
        return
    mem = MemoryStore(cfg.memory_db_path)
    try:
        ok = mem.conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"integrity_check: {ok}")
        print(f"journal-import watermark: {mem.get_meta(WATERMARK_KEY) or '(never run)'}")

        journal = mem.conn.execute(
            "SELECT COUNT(*) FROM memory WHERE source_refs LIKE '%journal_import%'"
        ).fetchone()[0]
        active_j = mem.conn.execute(
            "SELECT COUNT(*) FROM memory WHERE source_refs LIKE '%journal_import%' "
            "AND status='active'").fetchone()[0]
        print(f"journal-import facts: {journal} total, {active_j} active")

        print("\nby category (journal-import, active):")
        for row in mem.conn.execute(
                "SELECT category, COUNT(*) n FROM memory "
                "WHERE source_refs LIKE '%journal_import%' AND status='active' "
                "GROUP BY category ORDER BY n DESC"):
            print(f"  {row['category']}: {row['n']}")

        print("\nby month (journal-import, valid_from):")
        for row in mem.conn.execute(
                "SELECT substr(valid_from,1,7) m, COUNT(*) n FROM memory "
                "WHERE source_refs LIKE '%journal_import%' GROUP BY m ORDER BY m"):
            print(f"  {row['m']}: {row['n']}")

        sizes = report(mem)
        print(f"\nwhole graph: {sizes['active']} active fact(s), "
              f"{sizes['superseded']} superseded, {sizes['edges']} edge(s), "
              f"{sizes['rejections']} rejection(s), {sizes['evidence']} evidence row(s)")
    finally:
        mem.close()


if __name__ == "__main__":
    main()
