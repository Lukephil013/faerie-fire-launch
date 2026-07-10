"""Faerie Fire — system-tray background app.

Runs always-on capture in a background thread and shows a tray icon (near the
clock): green = capturing, grey = paused. Left-click opens the Command Center;
right-click menu lets you pause/resume, open Capture Control, or quit.

This is the recommended always-on entry point (replaces bare run.py for the
background daemon). Single-instance: a second launch just exits.

Run:  pythonw tray.py     (or via Start Background Capture.bat)
Requires:  pip install pystray pillow
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback

from livingpc.config import load
from livingpc.service import run as run_capture
from livingpc.lockfile import InstanceLock
from livingpc.diagnostics import log_diag

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "livingpc", "companion", "companion_error.log")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[tray] " + msg + "\n")
    except Exception:
        pass
    log_diag("tray", msg)


def _icon_image(running: bool):
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ring = (70, 236, 255, 255) if running else (120, 130, 160, 255)
    d.ellipse((6, 6, 58, 58), fill=ring)
    d.ellipse((20, 20, 44, 44), fill=(8, 10, 24, 255))      # hollow core
    d.ellipse((28, 28, 36, 36), fill=ring)                  # spark
    return im


class Tray:
    def __init__(self):
        self.cfg = load("config.toml")
        self.profile = getattr(self.cfg, "profile", "personal")
        self.capture_enabled = self.profile != "launch"
        self.stop = threading.Event()
        self.pause = threading.Event()
        self.icon = None
        log_diag("tray", f"initialized root={ROOT} profile={self.profile}")

    def _run_capture_thread(self):
        try:
            run_capture(self.cfg, log=lambda *a: None,
                        stop_event=self.stop, pause_event=self.pause)
        finally:
            log_diag("tray", "capture thread exited")
            if self.icon is not None:
                try:
                    self.icon.stop()
                except Exception:
                    pass
                threading.Thread(target=self._force_exit_if_stuck, daemon=True).start()

    def _force_exit_if_stuck(self):
        time.sleep(3.0)
        log_diag("tray", "forcing process exit after capture thread stopped")
        os._exit(0)

    def _start_inference_scheduler(self):
        """Fire the inference loop on a cadence (+ nightly deep pass) in the
        background. Never fatal — a scheduler hiccup must not take down capture."""
        if not self.capture_enabled:
            log_diag("tray", "launch profile: scheduler disabled")
            return
        if not getattr(self.cfg, "inference_scheduler_enabled", True):
            log_diag("tray", "inference scheduler disabled by config")
            return
        try:
            from livingpc.inference_scheduler import InferenceScheduler
            scheduler = InferenceScheduler(self.cfg, log=log)
            threading.Thread(target=scheduler.run, args=(self.stop,),
                             daemon=True).start()
            log_diag("tray", "inference scheduler thread started")
        except Exception:
            log("inference scheduler failed to start:\n" + traceback.format_exc())

    def _migrate_encryption(self):
        """Encrypt legacy plaintext before any background writer starts."""
        if not self.capture_enabled:
            log_diag("tray", "launch profile: encryption migration skipped")
            return
        from livingpc import crypto
        if not crypto.enabled():
            log_diag("tray", "at-rest encryption unavailable or explicitly disabled")
            return
        from encrypt_db import encrypt_existing
        counts = encrypt_existing(self.cfg)
        log_diag(
            "tray", "encryption migration "
            f"events={counts['event_fields']} memory={counts['memory_fields']} "
            f"blobs={counts['blobs']}",
        )

    def _launch(self, script, *args):
        try:
            exe = "pythonw" if os.name == "nt" else sys.executable
            subprocess.Popen([exe, script, *args], cwd=ROOT)
            log_diag("tray", f"launched script={script} args={args} exe={exe} cwd={ROOT}")
        except Exception:
            log("launch failed:\n" + traceback.format_exc())

    # menu actions
    def _toggle_pause(self, icon, item):
        if self.pause.is_set():
            self.pause.clear()
        else:
            self.pause.set()
        paused = self.pause.is_set()
        log_diag("tray", "pause toggled state=" + ("paused" if paused else "capturing"))
        icon.icon = _icon_image(running=not paused)
        icon.title = "Faerie Fire — " + ("paused" if paused else "capturing")

    def _open_gui(self, icon, item):
        self._launch("gui.py", "--view", "command-center")

    def _open_capture_control(self, icon, item):
        self._launch("capture_control.py")

    def _quit(self, icon, item):
        self.stop.set()
        log_diag("tray", "quit requested")
        icon.stop()

    def _build_menu(self):
        import pystray
        items = [pystray.MenuItem("Open Command Center", self._open_gui, default=True)]
        if self.capture_enabled:
            items.extend([
                pystray.MenuItem(
                    "Pause capture", self._toggle_pause,
                    checked=lambda item: self.pause.is_set()),
                pystray.MenuItem("Open Capture Control", self._open_capture_control),
            ])
        items.append(pystray.MenuItem("Quit Faerie Fire", self._quit))
        return pystray.Menu(*items)

    def run(self):
        self._migrate_encryption()
        self._start_inference_scheduler()
        try:
            import pystray  # noqa: F401
        except Exception:
            # No tray library — still run capture, just without the icon.
            if not self.capture_enabled:
                log("pystray not installed; launch profile has no capture loop to run.")
                log_diag("tray", "launch profile without pystray; exiting")
                return
            log("pystray not installed; capturing without a tray icon. "
                "Run 'pip install pystray' for the icon.")
            log_diag("tray", "running capture without pystray")
            self._run_capture_thread()
            return
        import pystray
        self.icon = pystray.Icon(
            "FaerieFire", _icon_image(self.capture_enabled),
            "Faerie Fire — " + ("capturing" if self.capture_enabled else "launch profile"),
            menu=self._build_menu())
        if not self.capture_enabled:
            log_diag("tray", "launch profile tray running without capture")
            self.icon.run()
            return
        # capture runs in a background thread; tray UI blocks the main thread
        log_diag("tray", "starting capture thread")
        t = threading.Thread(target=self._run_capture_thread, daemon=True)
        t.start()
        log_diag("tray", "tray icon running")
        self.icon.run()


def main():
    lock = InstanceLock(os.path.join(ROOT, "tray.lock"))
    if not lock.acquire():
        log("tray already running — exiting.")
        return
    log_diag("tray", f"tray lock acquired path={os.path.join(ROOT, 'tray.lock')}")
    try:
        Tray().run()
    except Exception:
        log("tray crashed:\n" + traceback.format_exc())
    finally:
        lock.release()
        log_diag("tray", f"tray lock released path={os.path.join(ROOT, 'tray.lock')}")


if __name__ == "__main__":
    main()
