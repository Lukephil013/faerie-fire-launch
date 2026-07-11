"""Active-window tracker -> focus sessions.

The lightweight backbone: each time the foreground window changes it closes the
previous session, opens a new one, and logs a 'window' event. Everything else
(screenshots, OCR) is grouped under the current session id.
"""
from __future__ import annotations

from ..storage import EventLog


class WindowTracker:
    def __init__(self, store: EventLog):
        self.store = store
        self._current: tuple[int, str, str] | None = None  # (session_id, app, title)

    @property
    def session_id(self) -> int | None:
        return self._current[0] if self._current else None

    def update(self, app: str, title: str, ts: str) -> int:
        """Ensure a session matching (app, title) is open; return its id."""
        if self._current is None or self._current[1] != app or self._current[2] != title:
            if self._current is not None:
                self.store.end_session(self._current[0], ts)
            sid = self.store.start_session(app, title, ts)
            self.store.log_event(
                "window", app=app, window_title=title, session_id=sid, ts=ts
            )
            self._current = (sid, app, title)
        return self._current[0]

    def close(self, ts: str) -> None:
        if self._current is not None:
            self.store.end_session(self._current[0], ts)
            self._current = None
