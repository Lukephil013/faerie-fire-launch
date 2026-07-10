"""View recent browser-history and clipboard events.

    python view_activity.py                 # both, most recent 25
    python view_activity.py --type browser  # browser only
    python view_activity.py --type clipboard
    python view_activity.py --limit 50
"""
from __future__ import annotations

import argparse
from datetime import datetime

from livingpc.config import load
from livingpc.storage import EventLog
from livingpc import crypto


def _fmt(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts or "...."


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--type", choices=["browser", "clipboard", "all"], default="all")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--db", default=None)
    args = p.parse_args()

    cfg = load("config.toml")
    store = EventLog(args.db or cfg.db_path)

    types = ("browser", "clipboard") if args.type == "all" else (args.type,)
    placeholders = ",".join("?" * len(types))
    rows = store.conn.execute(
        f"SELECT ts, type, app, text_payload FROM events "
        f"WHERE type IN ({placeholders}) ORDER BY ts DESC LIMIT ?",
        (*types, args.limit),
    ).fetchall()

    b = store.count("browser")
    c = store.count("clipboard")
    print(f"browser events: {b}   clipboard events: {c}\n")

    if not rows:
        print("Nothing recorded yet. Let capture run a bit (browser scans every "
              "~2 min; clipboard on copy).")
        store.close()
        return

    for r in rows:
        text = (crypto.dec(r["text_payload"]) or "").strip().replace("\n", " ⏎ ")
        if len(text) > 200:
            text = text[:200] + " ..."
        tag = "🌐" if r["type"] == "browser" else "📋"
        print(f"{tag} [{_fmt(r['ts'])}] {r['app'] or ''}")
        print(f"    {text}")
        print()
    store.close()


if __name__ == "__main__":
    main()
