"""Generate a small tracked handoff from Git metadata without private content."""
from __future__ import annotations

import argparse
import subprocess
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .project_context import AREAS, ROOT
else:
    from project_context import AREAS, ROOT


HANDOFF_PATH = ROOT / "docs" / "HANDOFF.md"
MAX_FILES = 20


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=15, check=False,
    )
    return result.stdout.rstrip()


def changed_files(staged: bool) -> list[str]:
    if staged:
        output = _git("diff", "--cached", "--name-only", "--diff-filter=ACMRD")
        return [line for line in output.splitlines() if line and line != "docs/HANDOFF.md"]
    rows = []
    for line in _git("status", "--short").splitlines():
        relative = line[3:].strip().strip('"')
        if relative and relative != "docs/HANDOFF.md":
            rows.append(relative)
    return rows


def infer_areas(files: list[str]) -> list[str]:
    areas = []
    file_set = set(files)
    for name, area in AREAS.items():
        if file_set.intersection(area.files) or file_set.intersection(area.tests):
            areas.append(name)
    if any(path.startswith(("tools/", "docs/", ".githooks/")) or path in {"AGENTS.md", "CLAUDE.md"}
           for path in files):
        areas.append("agent-context")
    return sorted(set(areas)) or ["general"]


def render_handoff(
    *,
    generated_at: str,
    branch: str,
    head: str,
    files: list[str],
    areas: list[str],
    recent_commits: list[str],
) -> str:
    shown = files[:MAX_FILES]
    file_lines = "\n".join(f"- `{path}`" for path in shown) or "- No uncommitted files detected."
    if len(files) > MAX_FILES:
        file_lines += f"\n- ...and {len(files) - MAX_FILES} more; use `git show --stat -1`."
    context_lines = []
    for area in areas:
        if area in AREAS:
            context_lines.append(f"- `python tools/project_context.py {area}`")
    if not context_lines:
        context_lines.append("- `python tools/project_context.py all`")
    commits = "\n".join(f"- {item}" for item in recent_commits[:3]) or "- No commits yet."
    return f"""# Current Agent Handoff

Generated automatically before commit. It contains Git metadata only, never
memory, OCR, clipboard, conversation, URL, screenshot, or window-title content.

## Start Here

- Branch: `{branch or '(detached)'}`
- Parent HEAD: `{head or '(none)'}`
- Generated UTC: `{generated_at}`
- Changed areas: {', '.join(areas)}
- Read the latest commit message, then run the narrow context command below.
- Do not scan README, FEATURES, design documents, or devlogs unless the task requires history.

## Changed Files In This Commit

{file_lines}

## Context Commands

{chr(10).join(context_lines)}

## Verification

- `python tools/project_context.py all --verify`
- `python -m pytest -q`
- Test status is not inferred by the hook; verify before changing or shipping code.

## Recent History Before This Commit

{commits}
"""


def generate(staged: bool = False) -> str:
    files = changed_files(staged)
    text = render_handoff(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        branch=_git("branch", "--show-current"),
        head=_git("rev-parse", "--short", "HEAD"),
        files=files,
        areas=infer_areas(files),
        recent_commits=_git("log", "-3", "--pretty=%h %s").splitlines(),
    )
    HANDOFF_PATH.parent.mkdir(parents=True, exist_ok=True)
    HANDOFF_PATH.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="describe staged files for a pre-commit hook")
    args = parser.parse_args()
    text = generate(staged=args.staged)
    print(f"Updated {HANDOFF_PATH.relative_to(ROOT)} ({len(text)} chars).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
