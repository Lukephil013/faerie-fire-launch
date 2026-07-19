"""In-process fallback scheduler for portable instance backups.

Windows Task Scheduler is the primary unattended trigger.  The GUI also runs
this small daemon so a missed task is caught on startup and transient failures
are retried hourly while Faerie Fire remains open.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable


RETRY_SECONDS = 60 * 60
IDLE_POLL_SECONDS = 60


@dataclass(frozen=True)
class RuntimeState:
    running: bool
    next_attempt_utc: str = ""
    last_error_code: str = ""


class BackupRuntime:
    """Check once at startup, then wait until due or an hourly retry."""

    def __init__(self, cfg, *, status_fn: Callable | None = None,
                 create_fn: Callable | None = None,
                 now_fn: Callable[[], datetime] | None = None):
        self.cfg = cfg
        self._status_fn = status_fn
        self._create_fn = create_fn
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self._next_attempt: datetime | None = None
        self._last_error_code = ""

    def _functions(self):
        if self._status_fn is None or self._create_fn is None:
            from .instance_backup import backup_status, create_instance_backup
            self._status_fn = self._status_fn or backup_status
            self._create_fn = self._create_fn or create_instance_backup
        return self._status_fn, self._create_fn

    def start(self) -> "BackupRuntime":
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="faerie-fire-instance-backup", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, float(timeout)))

    def state(self) -> RuntimeState:
        with self._guard:
            next_attempt = (self._next_attempt.isoformat()
                            if self._next_attempt is not None else "")
            running = bool(self._thread is not None and self._thread.is_alive())
            return RuntimeState(running, next_attempt, self._last_error_code)

    def check_once(self) -> bool:
        """Run one due check. Return True only when a backup was verified."""
        if not bool(getattr(self.cfg, "instance_backup_enabled", False)):
            return False
        status_fn, create_fn = self._functions()
        try:
            status = status_fn(self.cfg)
            if not bool(getattr(status, "ok", False)):
                self._schedule_retry(getattr(status, "error_code", "status_failed"))
                return False
            if not bool(getattr(status, "due", False) or getattr(status, "purge_pending", False)):
                with self._guard:
                    self._last_error_code = ""
                    self._next_attempt = None
                return False
            reason = ("privacy_purge_retry" if getattr(status, "purge_pending", False)
                      else "startup_overdue")
            result = create_fn(self.cfg, reason=reason)
            if bool(getattr(result, "ok", False)) and bool(
                    getattr(result, "verified", True)):
                with self._guard:
                    self._last_error_code = ""
                    self._next_attempt = None
                return True
            self._schedule_retry(getattr(result, "error_code", "backup_failed"))
        except Exception as error:
            # Keep only a privacy-safe class name; paths and payloads never enter
            # diagnostics through this runtime.
            self._schedule_retry(f"runtime_{type(error).__name__}")
        return False

    def _schedule_retry(self, error_code: str) -> None:
        with self._guard:
            self._last_error_code = str(error_code or "backup_failed")
            self._next_attempt = self._now() + timedelta(seconds=RETRY_SECONDS)

    def _run(self) -> None:
        while not self._stop.is_set():
            now = self._now()
            with self._guard:
                due_at = self._next_attempt
            if due_at is None or now >= due_at:
                self.check_once()
            with self._guard:
                due_at = self._next_attempt
            wait_for = IDLE_POLL_SECONDS
            if due_at is not None:
                wait_for = max(1.0, min(IDLE_POLL_SECONDS,
                                        (due_at - self._now()).total_seconds()))
            self._stop.wait(wait_for)

