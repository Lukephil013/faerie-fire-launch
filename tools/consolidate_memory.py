"""CLI: run the memory consolidation (hygiene) pass on memory.db.

Merges duplicate active facts (newest survives; older copies are closed like a
supersession, never deleted), prunes stale rejection rows, and prunes stale
inference evidence. The nightly pass does this automatically.

Run:
    python tools/consolidate_memory.py --dry-run   # see what would happen
    python tools/consolidate_memory.py             # do it
    python tools/consolidate_memory.py --report    # size snapshot only
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from livingpc.config import load  # noqa: E402
from livingpc.consolidate import consolidate, report  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402


def main() -> None:
    cfg = load("config.toml")
    p = argparse.ArgumentParser(description="consolidate memory.db (dedupe + prune)")
    p.add_argument("--memory-db", default=cfg.memory_db_path)
    p.add_argument("--similarity", type=float, default=cfg.consolidate_value_similarity)
    p.add_argument("--dry-run", action="store_true", help="show changes without applying")
    p.add_argument("--report", action="store_true", help="print sizes only, change nothing")
    args = p.parse_args()
    if not os.path.exists(args.memory_db):
        print(f"[X] memory db not found: {args.memory_db}")
        return
    mem = MemoryStore(args.memory_db)
    try:
        if args.report:
            sizes = report(mem)
            print(f"[memory] {sizes['active']} active fact(s), "
                  f"{sizes['superseded']} superseded, {sizes['edges']} edge(s), "
                  f"{sizes['rejections']} rejection(s), {sizes['evidence']} evidence row(s)")
            for category, n in sizes["per_category"].items():
                print(f"  {category}: {n}")
            return
        result = consolidate(
            mem, similarity=args.similarity,
            rejection_retention_days=cfg.consolidate_rejection_retention_days,
            evidence_retention_days=cfg.consolidate_evidence_retention_days,
            dry_run=args.dry_run)
        verb = "would merge" if result["dry_run"] else "merged"
        print(f"[consolidate] {verb} {result['merged']} duplicate(s) in "
              f"{result['groups']} group(s); active {result['active_before']} "
              f"-> {result['active_after']}")
        print(f"  pruned {result['pruned_rejections']} stale rejection(s), "
              f"{result['pruned_evidence']} stale evidence row(s)"
              + (" (dry run)" if result["dry_run"] else ""))
        for dup_id, survivor_id in result["merges"]:
            print(f"  fact {dup_id} -> kept {survivor_id}")
    finally:
        mem.close()


if __name__ == "__main__":
    main()
