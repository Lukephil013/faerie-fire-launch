"""CLI: file a brain dump into your project docs.

Thin wrapper around `livingpc.filing.file_dump`. The dump can come from an
argument, a file, or stdin. `--dry-run` shows where it would go without
writing anything; `--list` shows the current project docs.

Run:
    python tools/file_dump.py "half an essay about my Etsy SEO idea..."
    python tools/file_dump.py --file dump.txt
    echo "idea..." | python tools/file_dump.py
    python tools/file_dump.py "..." --dry-run
    python tools/file_dump.py --list
    python tools/file_dump.py --undo <entry-id>
    python tools/file_dump.py --distill <slug>        # diff + explicit y/N
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from livingpc.config import load  # noqa: E402
from livingpc import filing  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="file a brain dump into project docs")
    p.add_argument("dump", nargs="?", default="", help="the dump text")
    p.add_argument("--file", default="", help="read the dump from this file")
    p.add_argument("--backend", default="", help="claude | stub | ollama")
    p.add_argument("--dry-run", action="store_true",
                   help="show the proposed filing without writing")
    p.add_argument("--list", action="store_true", help="list project docs")
    p.add_argument("--undo", default="", metavar="ENTRY_ID",
                   help="remove a previously filed entry by id")
    p.add_argument("--distill", default="", metavar="SLUG",
                   help="propose a restructured version of one doc (asks y/N)")
    args = p.parse_args()

    cfg = load("config.toml")
    projects_dir = filing.projects_dir_for(cfg)

    if args.list:
        docs = filing.projects_overview(projects_dir)
        if not docs:
            print("(no project docs yet)")
        for d in docs:
            line = f"- {d['slug']}: {d['title']}"
            if d["summary"]:
                line += f" — {d['summary'][:100]}"
            print(line)
        return

    if args.undo:
        result = filing.undo(projects_dir, args.undo)
        if not result["found"]:
            print(f"[X] no entry with id {args.undo}")
        elif result["deleted_doc"]:
            print(f"[OK] entry removed; empty doc deleted: {result['path']}")
        else:
            print(f"[OK] entry removed from {result['path']}")
        return

    backend = filing.get_backend(cfg, args.backend or None)

    if args.distill:
        result = filing.distill_project(cfg, args.distill, backend=backend)
        if not result["changed"]:
            print("[OK] the model proposes no changes.")
            return
        print(result["diff"])
        answer = input("\nApply this restructure? A pre-distill copy is saved "
                       "to projects/.history/ first. [y/N] ").strip().lower()
        if answer == "y":
            filing.distill_project(cfg, args.distill, backend=backend, apply=True)
            print(f"[OK] applied: {result['path']}")
        else:
            print("[OK] left untouched.")
        return

    dump = args.dump
    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as f:
            dump = f.read()
    if not dump and not sys.stdin.isatty():
        dump = sys.stdin.read()
    if not dump.strip():
        p.error("nothing to file: pass text, --file, or pipe via stdin")

    result = filing.file_dump(cfg, dump, backend=backend, dry_run=args.dry_run)
    if result["clarify"]:
        print(f"[?] {result['clarify']}")
        return
    for item in result["filed"]:
        if result["dry_run"]:
            print(f"[dry-run] {item['action']} -> {item['project']} "
                  f"(\"{item['section_title']}\", conf {item['confidence']:.0%}, "
                  f"{item['chars']} chars)")
        else:
            verb = "created" if item["created"] else "filed under"
            print(f"[OK] {verb} {item['title']} ({item['path']})  "
                  f"undo: python tools/file_dump.py --undo {item['entry_id']}")


if __name__ == "__main__":
    main()
