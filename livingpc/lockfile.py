"""Single-instance lock so only one capture process runs at a time.

A simple PID lock file: if the file exists and its PID is still alive, another
instance owns it. Otherwise the lock is stale and we take it. This keeps a
background daemon, a manual run, and the GUI's capture from all double-writing.
"""
from __future__ import annotations

import os


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)       # signal 0 = existence check
            return True
        except OSError:
            return False
        except Exception:
            return True           # be conservative if unsure


def is_running(path: str) -> bool:
    """True if a live process currently holds the lock at `path`."""
    if not os.path.exists(path):
        return False
    try:
        pid = int(open(path).read().strip() or "0")
    except Exception:
        return False
    return _pid_alive(pid)


class InstanceLock:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.acquired = False

    def acquire(self) -> bool:
        """Return True if we got the lock, False if another live instance holds it."""
        if is_running(self.path):
            return False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                f.write(str(os.getpid()))
            self.acquired = True
            return True
        except OSError:
            return False

    def release(self) -> None:
        if self.acquired and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass
        self.acquired = False
