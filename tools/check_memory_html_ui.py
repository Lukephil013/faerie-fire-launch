"""Fast local checks for the large pywebview memory.html file.

Usage:
    python tools/check_memory_html_ui.py

This deliberately avoids launching the GUI.  It syntax-checks the embedded
JavaScript with Node.js and runs the focused pytest harness for Growth UI state
regressions.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "livingpc" / "ui" / "memory.html"


def _script() -> str:
    text = HTML.read_text(encoding="utf-8")
    match = re.search(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    if not match:
        raise RuntimeError("memory.html has no inline <script> block")
    return match.group(1)


def _run(args: list[str], *, input_text: str | None = None) -> int:
    print(" ".join(args))
    result = subprocess.run(
        args,
        input=input_text,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    return int(result.returncode)


def main() -> int:
    code = _run(["node", "--check", "-"], input_text=_script())
    if code:
        return code
    return _run(["python", "-m", "pytest", "tests/test_memory_html_ui.py", "-q"])


if __name__ == "__main__":
    sys.exit(main())
