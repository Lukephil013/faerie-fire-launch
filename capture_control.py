"""Control panel for Faerie Fire capture scripts (pywebview).

Wraps the common start/stop/reset/diagnostics operations and shows live status
plus the diagnostic log in one window, styled like the rest of the app.

The page lives in livingpc/ui/capture.html; this file is the js_api bridge.
Bridge calls run on pywebview worker threads and RETURN their output — the JS
awaits promises and polls for status/log updates, so Python never pushes into
the page from another thread.

Run: python capture_control.py   (or double-click "Capture Control.bat")
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading

from livingpc.diagnostics import APP_DIR, DIAG_LOG, log_diag
from livingpc.service import STOP_PATH

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def parse_state(status_text: str) -> str:
    """Map capture_status.py output to a coarse state for the header pill."""
    lower = (status_text or "").lower()
    if "screen capturing now" in lower:
        return "capturing"
    if "running" in lower:
        return "quiet"
    if "holding the lock: no" in lower:
        return "stopped"
    return "unknown"


class CaptureApi:
    """Bridge exposed to the page as pywebview.api.*"""

    def __init__(self):
        self._diag_line_count = 0
        self._diag_lock = threading.Lock()

    # --- helpers ------------------------------------------------------------
    def _pythonw(self) -> str:
        if os.name != "nt":
            return sys.executable
        candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        return candidate if os.path.exists(candidate) else sys.executable

    def _run(self, label: str, args: list[str], timeout: int = 90) -> str:
        """Run a command to completion and return its combined output."""
        log_diag("control", f"running {label}")
        try:
            proc = subprocess.run(
                args, cwd=APP_DIR, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout, creationflags=CREATE_NO_WINDOW,
            )
            out = proc.stdout or "(no output)"
            return f"{out}\nexit code: {proc.returncode}"
        except subprocess.TimeoutExpired:
            return f"Timed out after {timeout}s and was stopped."
        except Exception as error:
            return f"{label} failed: {type(error).__name__}: {error}"

    def _run_py(self, name: str, timeout: int = 90) -> str:
        path = os.path.join(APP_DIR, name)
        if not os.path.exists(path):
            return f"Missing: {path}"
        return self._run(name, [sys.executable, path], timeout=timeout)

    def _spawn(self, script: str, label: str) -> str:
        exe = self._pythonw()
        try:
            subprocess.Popen(
                [exe, script], cwd=APP_DIR, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
            log_diag("control", f"{label} exe={exe}")
            return f"Started {script} with pythonw."
        except Exception as error:
            log_diag("control", f"{label} failed error={error}")
            return f"Start failed: {type(error).__name__}: {error}"

    # --- status + log (polled by the page) -----------------------------------
    def status(self) -> dict:
        out = self._run("capture_status.py",
                        [sys.executable, os.path.join(APP_DIR, "capture_status.py")],
                        timeout=20)
        text = out.rsplit("\nexit code:", 1)[0]
        return {"text": text, "state": parse_state(text)}

    def log_tail(self) -> str:
        """New diagnostic-log lines since the last poll (first poll: last 80)."""
        try:
            if not os.path.exists(DIAG_LOG):
                return ""
            with open(DIAG_LOG, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            with self._diag_lock:
                if self._diag_line_count == 0:
                    new = lines[-80:]
                else:
                    new = lines[self._diag_line_count:]
                self._diag_line_count = len(lines)
            return "".join(new)
        except Exception as error:
            return f"Could not read diagnostic log: {error}\n"

    # --- actions --------------------------------------------------------------
    def start(self) -> str:
        return self._spawn("tray.py", "started tray")

    def stop(self) -> str:
        try:
            with open(STOP_PATH, "w", encoding="utf-8") as f:
                f.write("stop")
            log_diag("control", f"stop signal written stop_path={STOP_PATH}")
            return f"Stop signal written: {STOP_PATH}"
        except Exception as error:
            log_diag("control", f"stop failed error={error}")
            return f"Stop failed: {type(error).__name__}: {error}"

    def reset(self) -> str:
        return self._run_py("reset_capture.py")

    def collect_diagnostics(self) -> str:
        return self._run_py("collect_diagnostics.py")

    def companion_bundle(self) -> str:
        return self._run_py("collect_companion_diagnostics.py")

    def force_stop(self) -> str:
        return self._run("Force Stop All pythonw",
                         ["taskkill", "/F", "/IM", "pythonw.exe"])

    def open_memory_gui(self) -> str:
        return self._spawn("gui.py", "opened memory gui")

    def open_diagnostics(self) -> bool:
        path = os.path.join(APP_DIR, "diagnostics")
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])
        return True


def main():
    import webview
    from livingpc.ui import load_html
    webview.create_window(
        "Faerie Fire — Capture Control", html=load_html("capture.html"),
        js_api=CaptureApi(), width=980, height=680, min_size=(760, 520),
        background_color="#06070f",
    )
    webview.start()


if __name__ == "__main__":
    main()
