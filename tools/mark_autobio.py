"""One-shot: insert date markers into data/notion/Autobio.md so deep import
reads it as ~21 dated section entries instead of one trimmed blob.

Idempotent — safe to run twice. Sections get 05/27/2021 (the writing date);
the 10/15/22 update becomes 2022; the 07/03/2026 note keeps its date.

Run:  python tools/mark_autobio.py
Then: python tools/import_journal.py --month 2021-05 --reset --deep
      python tools/import_journal.py --month 2022-10 --reset --deep
      python tools/consolidate_memory.py
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "notion", "Autobio.md")

HEADERS = [
    "How I was during Childhood.", "Video Games.", "Spirituality.",
    "Jordan Peterson, Red Pill, Self-Improvement", "School.",
    "Korea and Social Relationships.", "Jobs.", "Post-Korea",
    "Family members, Individuals, and their effects on my life.",
    "Dad.", "Mom.", "Me.", "Aaron.", "Eli.", "Gramzie.",
    "Eli Naron and Weed.", "Hansol/한솔.(wife/gf)", "Weed.",
]
DATE_LINE = re.compile(r"^\s*\d{1,2}\s*[/\-.]\s*\d{1,2}(\s*[/\-.]\s*\d{2,4})?\s*$")


def main() -> None:
    if not os.path.exists(PATH):
        print(f"[X] not found: {PATH}")
        sys.exit(1)
    s = open(PATH, encoding="utf-8").read()
    changed = 0

    if "default_year: 2021" not in s:
        s2 = re.sub(r"default_year: \d{4}", "default_year: 2021", s, count=1)
        changed += int(s2 != s)
        s = s2
    if "07/03/2026 Note" in s:
        s = s.replace("07/03/2026 Note", "07/03/2026\nNote (added 2026):", 1)
        changed += 1
    if "10/15/22 update." in s:
        s = s.replace("10/15/22 update.", "10/15/2022\nUpdate (added 2022):", 1)
        changed += 1

    lines = s.splitlines()
    out = []
    for i, line in enumerate(lines):
        if line.strip() in HEADERS:
            # skip if the previous non-empty line is already a date marker
            prev = next((l for l in reversed(out) if l.strip()), "")
            if not DATE_LINE.match(prev):
                out.append("05/27/2021")
                changed += 1
        out.append(line)
    open(PATH, "w", encoding="utf-8").write("\n".join(out) + "\n")

    # verify what the importer will now see
    sys.path.insert(0, ROOT)
    from livingpc.journal_import import load_journals
    journal = next(j for j in load_journals(os.path.dirname(PATH))
                   if j["file"] == "Autobio.md")
    dated = [e["date"] for e in journal["entries"] if e["date"]]
    print(f"[autobio] {changed} change(s); now {len(journal['entries'])} entries, "
          f"{len(dated)} dated ({min(dated)} -> {max(dated)})" if dated else
          f"[autobio] {changed} change(s); still no dated entries — tell Claude")


if __name__ == "__main__":
    main()
