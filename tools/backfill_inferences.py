"""Backfill inference evidence from your capture history, and reset pre-rework
inference rows.

Why: the evidence-accumulation engine starts with an empty `evidence` table and
only looks ~1h back on its first live run, so it won't mine the activity you've
already captured. This walks the stored sessions in `living_computer.db`, turns
them into per-app/per-day **evidence** rows in `memory.db`, and (optionally) wipes
the low-confidence inference rows left over from the previous version so the
"forming" view isn't cluttered with dead bars.

It writes only the `evidence` table and (with --reset) deletes candidate/partial
`inference` rows. Confirmed beliefs and rejections are left untouched. Nothing is
sent anywhere; synthesis into claims happens later, when the inference loop runs.

Run:
    python tools/backfill_inferences.py --reset            # last 30 days (default)
    python tools/backfill_inferences.py --reset --days 0   # all history
    python tools/backfill_inferences.py --days 14 --min-minutes 5
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MEMORY = os.path.join(ROOT, "data", "memory.db")
DEFAULT_EVENTS = os.path.join(ROOT, "data", "living_computer.db")
INTERNAL_APPS = {"python.exe", "pythonw.exe"}
DEFAULT_BLOCKLIST = {"1Password.exe", "Bitwarden.exe"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _appname(app: str | None) -> str:
    return (app or "").replace("\\", "/").rsplit("/", 1)[-1] or "(unknown)"


def integrity_ok(path: str) -> bool:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        res = con.execute("PRAGMA integrity_check").fetchone()
        con.close()
        return bool(res) and res[0] == "ok"
    except sqlite3.DatabaseError:
        return False


def reset_pre_rework(mem: sqlite3.Connection) -> int:
    """Delete candidate/partial inference rows (they predate the evidence layer or
    will be regenerated from evidence by synthesis). Keep confirmed/rejected."""
    n = mem.execute(
        "SELECT COUNT(*) FROM inference WHERE status IN ('candidate','partial')"
    ).fetchone()[0]
    mem.execute("DELETE FROM inference WHERE status IN ('candidate','partial')")
    mem.commit()
    return int(n)


def backfill_evidence(mem: sqlite3.Connection, ev: sqlite3.Connection, *,
                      days: int, min_minutes: float, cap_per_theme: int,
                      blocklist: set) -> tuple[int, dict]:
    q = "SELECT app, window_title, start_ts, end_ts FROM sessions WHERE end_ts IS NOT NULL"
    params: list = []
    if days and days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q += " AND start_ts >= ?"
        params.append(since)

    dwell: dict = defaultdict(float)          # (app, date) -> seconds
    for app, title, start_ts, end_ts in ev.execute(q, params):
        name = _appname(app)
        if name.lower() in INTERNAL_APPS or name in blocklist:
            continue
        if (title or "").strip().lower().startswith("faerie fire"):
            continue
        try:
            secs = (datetime.fromisoformat(end_ts)
                    - datetime.fromisoformat(start_ts)).total_seconds()
        except (TypeError, ValueError):
            continue
        if secs <= 0:
            continue
        dwell[(name, str(start_ts)[:10])] += secs

    per_theme: dict = defaultdict(int)
    added = 0
    for (name, date), secs in sorted(dwell.items(), key=lambda kv: kv[1], reverse=True):
        if secs < min_minutes * 60:
            continue
        if per_theme[name] >= cap_per_theme:
            continue
        obs = f"~{round(secs / 60)} min on {name} on {date}"
        mem.execute(
            "INSERT INTO evidence (theme, observation, weight, source_refs, created_at) "
            "VALUES (?,?,?,?,?)", (name, obs, 1.0, "[]", _now()))
        per_theme[name] += 1
        added += 1
    mem.commit()
    return added, dict(per_theme)


def advance_watermark(ev: sqlite3.Connection) -> None:
    ev.execute(
        "INSERT INTO meta (key, value) VALUES ('inference_watermark', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (_now(),))
    ev.commit()


def main() -> None:
    p = argparse.ArgumentParser(description="backfill inference evidence from capture history")
    p.add_argument("--memory-db", default=DEFAULT_MEMORY)
    p.add_argument("--events-db", default=DEFAULT_EVENTS)
    p.add_argument("--days", type=int, default=30, help="history window (0 = all)")
    p.add_argument("--min-minutes", type=float, default=3.0,
                   help="ignore an app/day with less dwell than this")
    p.add_argument("--cap", type=int, default=40, help="max evidence items per theme")
    p.add_argument("--reset", action="store_true",
                   help="delete pre-rework candidate/partial inference rows first")
    p.add_argument("--no-watermark", action="store_true",
                   help="don't advance the inference watermark (the loop may re-ingest history)")
    args = p.parse_args()

    if not os.path.exists(args.events_db):
        print(f"[X] capture db not found: {args.events_db}")
        return
    if not integrity_ok(args.events_db):
        print(f"[X] {args.events_db} failed PRAGMA integrity_check — it may be open in a\n"
              "    running capture process (stop capture and retry) or genuinely corrupt.")
        return

    mem = sqlite3.connect(args.memory_db)
    ev = sqlite3.connect(f"file:{args.events_db}?mode=ro", uri=True)
    try:
        if args.reset:
            removed = reset_pre_rework(mem)
            print(f"[reset] removed {removed} pre-rework candidate/partial inference(s).")
        added, per_theme = backfill_evidence(
            mem, ev, days=args.days, min_minutes=args.min_minutes,
            cap_per_theme=args.cap, blocklist=DEFAULT_BLOCKLIST)
        ev.close()
        if not args.no_watermark:
            evw = sqlite3.connect(args.events_db)
            try:
                advance_watermark(evw)
            finally:
                evw.close()
    finally:
        mem.close()

    span = "all history" if args.days == 0 else f"last {args.days} days"
    print(f"[backfill] added {added} evidence item(s) across {len(per_theme)} theme(s) "
          f"({span}).")
    for theme, n in sorted(per_theme.items(), key=lambda kv: kv[1], reverse=True)[:12]:
        print(f"    {n:>3}  {theme}")
    print("\nNext: run the inference loop (open the Inferences tab and click 'Run "
          "inference now', or let the scheduler run) to synthesise this evidence into "
          "claims. Themes with enough evidence can now graduate past the 80% gate.")


if __name__ == "__main__":
    main()
