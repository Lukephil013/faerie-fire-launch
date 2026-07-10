"""Foreground-window + idle-time backends.

WindowsBackend uses ctypes (user32/kernel32) + psutil. StubBackend lets the
service import and run on non-Windows (e.g. for development/testing) without
crashing -- it just reports a static window and zero idle time.
"""
from __future__ import annotations

import sys
from typing import Protocol


class Backend(Protocol):
    def foreground(self) -> tuple[str, str]:
        """Return (app_exe_name, window_title)."""
        ...

    def idle_seconds(self) -> float:
        """Seconds since the last mouse/keyboard input."""
        ...


class StubBackend:
    """Fallback used off-Windows. Always 'active', single fake window."""

    def foreground(self) -> tuple[str, str]:
        return ("python.exe", "StubBackend (no Windows API available)")

    def idle_seconds(self) -> float:
        return 0.0


class WindowsBackend:
    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        self._LASTINPUTINFO = LASTINPUTINFO

    def foreground(self) -> tuple[str, str]:
        ctypes = self._ctypes
        user32 = self._user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ("", "")

        # window title
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        # owning process name
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        app = self._process_name(pid.value)
        return (app, title)

    def _process_name(self, pid: int) -> str:
        try:
            import psutil

            return psutil.Process(pid).name()
        except Exception:
            return ""

    def idle_seconds(self) -> float:
        lii = self._LASTINPUTINFO()
        lii.cbSize = self._ctypes.sizeof(self._LASTINPUTINFO)
        if not self._user32.GetLastInputInfo(self._ctypes.byref(lii)):
            return 0.0
        millis = self._kernel32.GetTickCount() - lii.dwTime
        return millis / 1000.0


def get_backend() -> Backend:
    if sys.platform == "win32":
        try:
            return WindowsBackend()
        except Exception:  # pragma: no cover - defensive
            return StubBackend()
    return StubBackend()
