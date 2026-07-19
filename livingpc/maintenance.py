"""Cross-process serialization for backup, restore, and explicit Forget."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

from .config import DATA_DIR


LOCK_PATH = os.path.join(DATA_DIR, ".maintenance.lock")
_DEPTH = threading.local()


class MaintenanceBusy(RuntimeError):
    """Another process is backing up, restoring, or forgetting data."""


def _try_lock(handle) -> bool:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (BlockingIOError, OSError):
        return False


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def maintenance_lock(*, timeout: float = 60.0, path: str | None = None):
    """Hold the one maintenance lock, with safe same-thread reentrancy.

    The file contains only a sentinel byte and is retained between runs.  The
    operating-system lock, not file existence, determines ownership, so a
    crash releases it automatically without stale-PID cleanup.
    """
    current_depth = int(getattr(_DEPTH, "value", 0))
    if current_depth:
        _DEPTH.value = current_depth + 1
        try:
            yield
        finally:
            _DEPTH.value -= 1
        return

    deadline = time.monotonic() + max(0.0, float(timeout))
    lock_path = os.path.abspath(path or LOCK_PATH)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = open(lock_path, "a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        while True:
            if _try_lock(handle):
                locked = True
                break
            if time.monotonic() >= deadline:
                raise MaintenanceBusy(
                    "another Faerie Fire maintenance operation is active")
            time.sleep(0.05)
        _DEPTH.value = 1
        try:
            yield
        finally:
            _DEPTH.value = 0
    finally:
        if locked:
            try:
                _unlock(handle)
            except OSError:
                pass
        handle.close()

