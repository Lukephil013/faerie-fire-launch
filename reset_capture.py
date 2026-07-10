"""Reset Faerie Fire capture processes and start a fresh tray-owned capture.

This is narrower than the "Force Stop" button in Capture Control: it only targets
Python processes whose command line is this project's tray.py or run.py.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from livingpc.diagnostics import APP_DIR, log_diag
from livingpc.service import LOCK_PATH, STOP_PATH


TRAY_LOCK_PATH = os.path.join(APP_DIR, "tray.lock")


def _is_capture_process(proc) -> bool:
    try:
        cmdline = proc.cmdline()
        cwd = proc.cwd()
        name = proc.name().lower()
    except Exception:
        return False
    if name not in {"python.exe", "pythonw.exe"}:
        return False
    if os.path.abspath(cwd) != os.path.abspath(APP_DIR):
        return False
    scripts = {os.path.basename(arg).lower() for arg in cmdline[1:]}
    return bool(scripts & {"tray.py", "run.py"})


def _kill_existing() -> list[int]:
    killed: list[int] = []
    try:
        import psutil
    except Exception as ex:
        print(f"Could not inspect processes: {type(ex).__name__}: {ex}")
        return killed

    me = os.getpid()
    procs = []
    for proc in psutil.process_iter(["pid"]):
        if proc.pid == me:
            continue
        if _is_capture_process(proc):
            procs.append(proc)

    for proc in procs:
        try:
            print(f"Stopping pid {proc.pid}: {' '.join(proc.cmdline())}")
            proc.terminate()
            killed.append(proc.pid)
        except Exception as ex:
            print(f"Could not terminate pid {proc.pid}: {type(ex).__name__}: {ex}")

    gone, alive = psutil.wait_procs(procs, timeout=5)
    for proc in alive:
        try:
            print(f"Force-stopping pid {proc.pid}")
            proc.kill()
        except Exception as ex:
            print(f"Could not kill pid {proc.pid}: {type(ex).__name__}: {ex}")
    if alive:
        psutil.wait_procs(alive, timeout=3)
    return killed


def _remove_runtime_files() -> None:
    for path in (LOCK_PATH, STOP_PATH, TRAY_LOCK_PATH):
        try:
            if os.path.exists(path):
                os.remove(path)
                print(f"Removed {path}")
        except OSError as ex:
            print(f"Could not remove {path}: {ex}")


def _pythonw() -> str:
    if os.name != "nt":
        return sys.executable
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else sys.executable


def main() -> None:
    log_diag("reset", "reset requested")
    killed = _kill_existing()
    _remove_runtime_files()
    time.sleep(0.5)
    exe = _pythonw()
    subprocess.Popen([exe, "tray.py"], cwd=APP_DIR)
    log_diag("reset", f"started fresh tray exe={exe} killed={killed}")
    print("")
    print("Started fresh tray-owned capture.")
    print(r"Open bats\Capture Control.bat in a few seconds; lock owner should be tray.py.")


if __name__ == "__main__":
    main()
