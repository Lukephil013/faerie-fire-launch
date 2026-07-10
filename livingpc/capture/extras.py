"""Extra collectors: browser history and clipboard.

Both write 'browser' / 'clipboard' events into the same event log. They poll on
their own intervals (not every tick) and use a watermark in the event-log `meta`
table so nothing is logged twice.

Privacy:
  * Clipboard capture is skipped when the foreground app is in the blocklist
    (so copying from a password manager isn't recorded), and the text is
    encrypted at rest like everything else.
  * Browser DBs are copied to a temp file before reading (the live file is locked
    while the browser runs); the copy is deleted immediately.
"""
from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

from ..storage import EventLog, now_iso


# ------------------------------------------------------------------ browsers
# Chromium stores last_visit_time as microseconds since 1601-01-01 (UTC).
_CHROMIUM_EPOCH_OFFSET = 11644473600  # seconds between 1601 and 1970


def _chromium_to_iso(microseconds: int) -> str:
    secs = microseconds / 1_000_000 - _CHROMIUM_EPOCH_OFFSET
    return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()


def _firefox_to_iso(microseconds: int) -> str:
    return datetime.fromtimestamp(microseconds / 1_000_000, tz=timezone.utc).isoformat()


def _chromium_history_paths() -> list[str]:
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return []
    bases = [
        os.path.join(local, "Google", "Chrome", "User Data"),
        os.path.join(local, "Microsoft", "Edge", "User Data"),
        os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
    ]
    paths = []
    for base in bases:
        # Default + Profile N
        for prof in glob.glob(os.path.join(base, "Default")) + \
                glob.glob(os.path.join(base, "Profile *")):
            hist = os.path.join(prof, "History")
            if os.path.exists(hist):
                paths.append(hist)
    return paths


def _firefox_history_paths() -> list[str]:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return []
    pattern = os.path.join(appdata, "Mozilla", "Firefox", "Profiles", "*", "places.sqlite")
    return [p for p in glob.glob(pattern) if os.path.exists(p)]


def _read_sqlite_copy(path: str, query: str, params=()):
    """Copy a (possibly locked) browser DB to temp and run a read query."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    try:
        shutil.copy2(path, tmp)
        conn = sqlite3.connect(tmp)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class BrowserHistoryCollector:
    """Logs newly visited URLs (title + url) since the last poll."""

    def poll(self, store: EventLog) -> int:
        n = 0
        n += self._poll_chromium(store)
        n += self._poll_firefox(store)
        return n

    def _poll_chromium(self, store: EventLog) -> int:
        n = 0
        for path in _chromium_history_paths():
            key = f"bh_chromium::{path}"
            wm = int(store.get_meta(key, "0"))
            rows = _read_sqlite_copy(
                path,
                "SELECT url, title, last_visit_time FROM urls "
                "WHERE last_visit_time > ? ORDER BY last_visit_time LIMIT 500",
                (wm,),
            )
            app = _browser_name(path)
            newest = wm
            for url, title, lvt in rows:
                if not url:
                    continue
                store.log_event("browser", app=app, window_title=title or "",
                                text_payload=f"{title or ''} — {url}",
                                ts=_chromium_to_iso(lvt))
                newest = max(newest, lvt)
                n += 1
            if newest > wm:
                store.set_meta(key, str(newest))
        return n

    def _poll_firefox(self, store: EventLog) -> int:
        n = 0
        for path in _firefox_history_paths():
            key = f"bh_firefox::{path}"
            wm = int(store.get_meta(key, "0"))
            rows = _read_sqlite_copy(
                path,
                "SELECT p.url, p.title, h.visit_date FROM moz_places p "
                "JOIN moz_historyvisits h ON h.place_id = p.id "
                "WHERE h.visit_date > ? ORDER BY h.visit_date LIMIT 500",
                (wm,),
            )
            newest = wm
            for url, title, vd in rows:
                if not url:
                    continue
                store.log_event("browser", app="firefox.exe", window_title=title or "",
                                text_payload=f"{title or ''} — {url}",
                                ts=_firefox_to_iso(vd))
                newest = max(newest, vd)
                n += 1
            if newest > wm:
                store.set_meta(key, str(newest))
        return n


def _browser_name(path: str) -> str:
    p = path.lower()
    if "edge" in p:
        return "msedge.exe"
    if "brave" in p:
        return "brave.exe"
    return "chrome.exe"


# ----------------------------------------------------------------- clipboard
def read_clipboard_text() -> str | None:
    """Return current clipboard text on Windows, else None."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    if os.name != "nt":
        return None

    CF_UNICODETEXT = 13
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u32.GetClipboardData.restype = wintypes.HANDLE
    k32.GlobalLock.restype = wintypes.LPVOID
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    if not u32.OpenClipboard(0):
        return None
    try:
        if not u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            k32.GlobalUnlock(handle)
    finally:
        u32.CloseClipboard()


class ClipboardCollector:
    """Logs clipboard text when it changes, skipping blocklisted apps."""

    MAX_LEN = 10000

    def __init__(self):
        self._last = None

    def poll(self, store: EventLog, foreground_app: str, blocklist) -> bool:
        text = read_clipboard_text()
        if not text or text == self._last:
            return False
        self._last = text
        if foreground_app in blocklist:        # e.g. password manager in front
            return False
        if len(text) > self.MAX_LEN:
            text = text[: self.MAX_LEN]
        store.log_event("clipboard", app=foreground_app, text_payload=text, ts=now_iso())
        return True
