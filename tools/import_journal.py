"""CLI: chronological journal backfill into the memory graph.

Walks data/notion/ (or --dir) oldest month first, proposes dated facts via the
model, and commits them with valid_from set to the entry dates — so temporal
supersession builds real trajectories instead of a pile of today-stamped facts.

Run:
    python tools/import_journal.py --dry-run              # free-ish preview (still calls the model)
    python tools/import_journal.py --backend stub --dry-run   # fully offline preview
    python tools/import_journal.py                        # the real import (resumes at watermark)
    python tools/import_journal.py --month 2026-05        # one month only
    python tools/import_journal.py --reset                # ignore the watermark, redo all months

After a large import, run: python tools/consolidate_memory.py
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from livingpc.config import load  # noqa: E402
from livingpc.journal_import import get_journal_model, import_journals  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402


def main() -> None:
    cfg = load("config.toml")
    p = argparse.ArgumentParser(description="import journals into memory, chronologically")
    p.add_argument("--dir", default=getattr(cfg, "journal_dir", "data/notion"))
    p.add_argument("--backend", default=None, choices=[None, "claude", "stub"],
                   help="override config llm_backend")
    p.add_argument("--month", default=None, help="only this YYYY-MM batch")
    p.add_argument("--min-confidence", type=float, default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="propose but commit nothing (no watermark advance)")
    p.add_argument("--reset", action="store_true", help="ignore the resume watermark")
    p.add_argument("--no-filter", action="store_true",
                   help="send everything; skip the local relevance pre-filter")
    p.add_argument("--deep", action="store_true",
                   help="dense-document mode: one model call per entry, "
                        "exhaustive extraction, event-time dating")
    args = p.parse_args()

    journal_dir = args.dir if os.path.isabs(args.dir) else os.path.join(ROOT, args.dir)
    if not os.path.isdir(journal_dir):
        print(f"[X] journal folder not found: {journal_dir}")
        print("    Export journals there first (data/notion/), or pass --dir.")
        return

    from livingpc.journal_import import load_journals, validate_dates
    for journal in load_journals(journal_dir):
        for warning in validate_dates(journal):
            print(f"[!] {journal['source']}: {warning}")

    model = get_journal_model(cfg, args.backend)
    mem = MemoryStore(cfg.memory_db_path)
    try:
        stats = import_journals(
            cfg, mem, model=model, journal_dir=journal_dir,
            dry_run=args.dry_run, min_confidence=args.min_confidence,
            only_month=args.month, reset=args.reset,
            filter_enabled=(False if args.no_filter else None),
            deep=args.deep)
    finally:
        mem.close()

    tag = " (dry run — nothing committed)" if args.dry_run else ""
    print(f"[journal] {stats['batches']} batch(es), {stats['entries']} entrie(s){tag}")
    f = stats.get("filter")
    if f:
        saved = (1 - (f["chars_out"] / f["chars_in"])) * 100 if f["chars_in"] else 0
        print(f"  filter: kept {f['kept']}/{f['in']} entries "
              f"({f['dropped_short']} short, {f['dropped_duplicate']} duplicate, "
              f"{f['dropped_low_signal']} low-signal, {f['trimmed']} trimmed) — "
              f"~{saved:.0f}% fewer chars sent")
    for month in stats["months"]:
        print(f"  {month['month']}: {month['entries']} entries -> "
              f"+{month['added']} added, {month['superseded']} superseded, "
              f"{month['duplicate']} duplicate, {month['stale']} stale, "
              f"{month['low_confidence']} below confidence gate")
    if not stats["months"]:
        print("  nothing to do — all months are at/behind the watermark "
              "(use --reset to redo).")
    elif not args.dry_run:
        print("  Tip: run 'python tools/consolidate_memory.py' next to merge any dupes.")


if __name__ == "__main__":
    main()
