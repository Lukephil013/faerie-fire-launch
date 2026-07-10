"""HTML front-ends for the pywebview apps (Memory GUI, Capture Control,
Assistant). Each page is self-contained (inline CSS/JS) and shares the
companion's design language: navy radial gradient, glassy cards, cyan accents.

`load_html(name)` returns the page source; entry points feed it to
`webview.create_window(html=...)` exactly like companion.py does.
"""
from __future__ import annotations

import os

UI_DIR = os.path.dirname(os.path.abspath(__file__))


def load_html(name: str) -> str:
    path = os.path.join(UI_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
