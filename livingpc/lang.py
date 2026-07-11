"""App-wide language selection (English / Korean) for the unified build.

The user picks a language on the first onboarding screen; the choice is
persisted as `language = "ko"` (or "en") in config.toml so every process —
GUI, tray, assistant, background jobs — sees the same setting.

Usage:
    from livingpc.lang import T, is_ko, app_language, set_app_language
    title = T("Faerie Fire", "페어리 파이어")
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG = os.path.join(ROOT, "config.toml")
_cached: str | None = None


def app_language() -> str:
    """Current app language: "en" (default) or "ko"."""
    global _cached
    if _cached in ("en", "ko"):
        return _cached
    lang = "en"
    try:
        with open(_CONFIG, encoding="utf-8") as handle:
            match = re.search(r'^\s*language\s*=\s*"(ko|en)"', handle.read(), re.M)
            if match:
                lang = match.group(1)
    except OSError:
        pass
    _cached = lang
    return lang


def is_ko() -> bool:
    return app_language() == "ko"


def is_language_set() -> bool:
    """True once the user has explicitly chosen a language (config.toml has a
    language line). Drives whether onboarding shows the language screen."""
    try:
        with open(_CONFIG, encoding="utf-8") as handle:
            return bool(re.search(r'^\s*language\s*=\s*"(ko|en)"', handle.read(), re.M))
    except OSError:
        return False


def T(en: str, ko: str) -> str:
    """Pick the string for the current app language."""
    return ko if is_ko() else en


def set_app_language(lang: str) -> str:
    """Persist the language to config.toml and update this process's cache."""
    global _cached
    normalized = "ko" if str(lang or "").strip().lower().startswith("ko") else "en"
    try:
        text = open(_CONFIG, encoding="utf-8").read()
    except OSError:
        text = ""
    if re.search(r"^\s*language\s*=", text, re.M):
        text = re.sub(r'^\s*language\s*=.*$', f'language = "{normalized}"', text, flags=re.M)
    else:
        text = (text.rstrip() + "\n" if text.strip() else "") + f'language = "{normalized}"\n'
    with open(_CONFIG, "w", encoding="utf-8") as handle:
        handle.write(text)
    _cached = normalized
    return normalized
