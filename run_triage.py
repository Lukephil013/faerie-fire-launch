"""Daily triage — distill activity into memory.

Two ways it runs:

  GENERATE (non-interactive, for the nightly scheduler):
      python run_triage.py --generate
    Runs the LLM, parks proposals as "pending" in memory.db, and exits.

  REVIEW (interactive, when you sit down):
      python run_triage.py            # review pending; if none, generate live then review
      python run_triage.py --review   # review pending only
    Walks each proposal: [a]pprove / [e]dit / [r]eject / [s]kip.

Other flags:
      --date 2026-06-24    which day to summarize (default: today)
      --backend stub       offline, no API key (for testing)
      --show-summary       print the redacted text sent to the model
"""
from __future__ import annotations

import argparse
import json
import os

from livingpc.config import load
from livingpc.storage import EventLog
from livingpc.memory import MemoryStore, today
from livingpc import crypto
from livingpc.triage.llm import get_backend
from livingpc.triage.pipeline import run_triage, apply_result, AUTO_COMMIT_CONFIDENCE


# --------------------------------------------------------------------------
def generate(cfg, date: str, show_summary: bool, incremental: bool = True) -> int:
    """Run the pipeline and save proposals as pending. Returns count saved."""
    events = EventLog(cfg.db_path)
    memory = MemoryStore(cfg.memory_db_path)
    try:
        backend = get_backend(cfg)
        ctx = run_triage(events, memory, backend, date, incremental=incremental)
        if show_summary:
            print("\n===== redacted summary sent to model =====")
            print(ctx.summary)
            print("==========================================\n")

        # Confident facts auto-commit; low-confidence facts + questions are
        # dropped. Getting curious about you is the inference engine's job now.
        threshold = getattr(cfg, "auto_commit_confidence", AUTO_COMMIT_CONFIDENCE)
        counts = apply_result(memory, ctx.result, date,
                              auto_commit_confidence=threshold,
                              watermark=ctx.window_end if incremental else None,
                              window_start=ctx.window_start if incremental else None)
        auto = counts["auto_committed"]
        dropped = counts["dropped"]

        # maintenance: delete screenshots older than the retention window (text kept)
        purged = _purge_old_blobs(events, cfg)
    finally:
        events.close()
        memory.close()
    msg = f"Auto-committed {auto} confident fact(s); dropped {dropped} low-confidence item(s)."
    if purged:
        msg += f" (cleaned up {purged} old screenshot(s))"
    print(msg)
    return auto


def _purge_old_blobs(events, cfg) -> int:
    from datetime import datetime, timezone, timedelta
    days = getattr(cfg, "blob_retention_days", 3)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        return events.purge_blobs(before_ts=cutoff)
    except Exception:
        return 0


# --------------------------------------------------------------------------
def _ask(label: str) -> str:
    try:
        return (input(f"{label} [a]pprove / [e]dit / [r]eject / [s]kip: ")
                .strip().lower() or "s")
    except EOFError:
        return "s"


def review(cfg) -> None:
    memory = MemoryStore(cfg.memory_db_path)
    pend = memory.list_pending()
    if not pend:
        print("Nothing pending to review.")
        memory.close()
        return

    approved = 0
    for row in pend:
        kind = row["kind"]
        p = json.loads(row["payload"])

        if kind == "statement":
            print("\n--- NEW STATEMENT ---")
            print(f"  [{p['category']}] {p['attribute']}: {p['value']}"
                  f"  (confidence {p.get('confidence', 0):.0%})")
            if p.get("note"):
                print(f"  note: {p['note']}")
            c = _ask("  add?")
            if c == "a":
                memory.add(p["category"], p["attribute"], p["value"],
                           confidence=p.get("confidence"))
                approved += 1; memory.clear_pending(row["id"])
            elif c == "e":
                v = input("    new value: ").strip()
                if v:
                    memory.add(p["category"], p["attribute"], v,
                               confidence=p.get("confidence"))
                    approved += 1
                memory.clear_pending(row["id"])
            elif c == "r":
                memory.add_rejection("statement", p.get("category", ""),
                                     f"{p.get('attribute','')}: {p.get('value','')}")
                memory.clear_pending(row["id"])

        elif kind == "supersession":
            old = memory.get(p["memory_id"])
            if old is None:
                memory.clear_pending(row["id"]); continue
            print("\n--- SUPERSESSION ---")
            print(f"  was : [{old['category']}] {old['attribute']}: {crypto.dec(old['value'])}")
            print(f"  now : {p['value']}")
            print(f"  why : {p.get('reason', '')}")
            c = _ask("  apply?")
            if c == "a":
                memory.supersede(p["memory_id"], p["value"], attribute=p.get("attribute"))
                approved += 1; memory.clear_pending(row["id"])
            elif c == "e":
                v = input("    new value: ").strip()
                if v:
                    memory.supersede(p["memory_id"], v, attribute=p.get("attribute"))
                    approved += 1
                memory.clear_pending(row["id"])
            elif c == "r":
                cat = old["category"] if old is not None else ""
                memory.add_rejection("supersession", cat, f"update: {p.get('value','')}")
                memory.clear_pending(row["id"])

        elif kind == "question":
            print("\n--- QUESTION ---")
            print(f"  {p['text']}")
            ans = input("  your answer (blank to skip, keeps for later): ").strip()
            if ans:
                cat = p.get("category") or input("  category: ").strip() or "General"
                memory.add(cat, "note", ans, confidence=1.0)
                approved += 1; memory.clear_pending(row["id"])

    print(f"\nApproved {approved} item(s) into memory. "
          f"{memory.count_pending()} still pending.")
    memory.close()


# --------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="daily triage")
    p.add_argument("--config", default="config.toml")
    p.add_argument("--date", default=today())
    p.add_argument("--db")
    p.add_argument("--memory-db")
    p.add_argument("--backend")
    p.add_argument("--generate", action="store_true",
                   help="non-interactive: generate proposals into pending and exit")
    p.add_argument("--review", action="store_true",
                   help="review pending proposals only")
    p.add_argument("--full", action="store_true",
                   help="summarize the whole day instead of only since last triage")
    p.add_argument("--show-summary", action="store_true")
    args = p.parse_args()

    cfg = load(args.config if os.path.exists(args.config) else None)
    if args.db:
        cfg.db_path = args.db
    if args.memory_db:
        cfg.memory_db_path = args.memory_db
    if args.backend:
        cfg.llm_backend = args.backend

    # incremental for "today"; whole-day only for an explicit past date or --full
    incremental = (args.date == today()) and not args.full

    if args.generate:
        generate(cfg, args.date, args.show_summary, incremental=incremental)
        return

    if args.review:
        review(cfg)
        return

    # default: review pending; if none, generate live then review
    mem = MemoryStore(cfg.memory_db_path)
    has_pending = mem.count_pending() > 0
    mem.close()
    if not has_pending:
        print(f"No pending proposals. Generating for {args.date} "
              f"using backend '{cfg.llm_backend}'...")
        generate(cfg, args.date, args.show_summary, incremental=incremental)
    review(cfg)


if __name__ == "__main__":
    main()
