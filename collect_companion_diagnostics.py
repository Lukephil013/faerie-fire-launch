"""Create an information-rich diagnostics bundle for the companion UI.

The bundle includes cropped companion-window screenshots and its rendered
conversation. It excludes the activity databases, clipboard history, OCR
payloads, and full-desktop screenshots.
"""
from __future__ import annotations

import ctypes
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import time
import zipfile
from ctypes import wintypes
from datetime import datetime, timezone

from PIL import ImageGrab

from livingpc.diagnostics import APP_DIR, DIAG_DIR


UI_STATE_PATH = os.path.join(DIAG_DIR, "companion_ui_state.json")
COMPANION_LOG = os.path.join(APP_DIR, "livingpc", "companion", "companion_error.log")
SOURCE_PATHS = [
    os.path.join(APP_DIR, "companion.py"),
    os.path.join(APP_DIR, "livingpc", "companion", "companion.html"),
    os.path.join(
        APP_DIR, "livingpc", "companion", "assets", "companion_avatar.jpg",
    ),
]


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _tail(path: str, max_lines: int = 600) -> str:
    if not os.path.exists(path):
        return f"missing: {path}\n"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-max_lines:])
    except Exception as ex:
        return f"could not read {path}: {type(ex).__name__}: {ex}\n"


def _file_info(path: str) -> dict:
    if not os.path.exists(path):
        return {"path": path, "missing": True}
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    stat = os.stat(path)
    return {
        "path": path,
        "bytes": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sha256": digest,
    }


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def _companion_processes() -> list[dict]:
    try:
        import psutil
    except Exception as ex:
        return [{"error": f"psutil unavailable: {ex}"}]
    all_rows = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "cwd", "status"]):
        try:
            args = proc.info.get("cmdline") or []
            all_rows.append({**proc.info, "cmdline_args": args, "cmdline": " ".join(args)})
        except Exception:
            continue
    companions = [
        row
        for row in all_rows
        if row.get("name", "").lower() in {"python.exe", "pythonw.exe"}
        and any(os.path.basename(arg).lower() == "companion.py" for arg in row["cmdline_args"][1:])
    ]
    companion_pids = {row["pid"] for row in companions}
    children = [row for row in all_rows if row.get("ppid") in companion_pids]
    for row in companions + children:
        row.pop("cmdline_args", None)
    return companions + children


def _windows() -> list[dict]:
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    records = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value
        if title != "Faerie Fire":
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, len(class_buf))
        dpi = user32.GetDpiForWindow(hwnd) if hasattr(user32, "GetDpiForWindow") else None
        records.append(
            {
                "hwnd": int(hwnd),
                "pid": pid.value,
                "title": title,
                "class": class_buf.value,
                "visible": True,
                "minimized": bool(user32.IsIconic(hwnd)),
                "dpi": dpi,
                "rect": {
                    "left": rect.left,
                    "top": rect.top,
                    "right": rect.right,
                    "bottom": rect.bottom,
                    "width": rect.right - rect.left,
                    "height": rect.bottom - rect.top,
                },
            }
        )
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return records


def _capture_frames(bundle_dir: str, windows: list[dict]) -> list[dict]:
    if not windows:
        return [{"error": "No visible window titled 'Faerie Fire' was found."}]
    companion_pids = {
        row["pid"]
        for row in _companion_processes()
        if isinstance(row, dict) and "companion.py" in row.get("cmdline", "").lower()
    }
    target = next(
        (w for w in windows if w["pid"] in companion_pids and not w["minimized"]),
        next((w for w in windows if not w["minimized"]), windows[0]),
    )
    rect = target["rect"]
    if target["minimized"] or rect["width"] <= 1 or rect["height"] <= 1:
        return [{"error": "The companion window is minimized or has an invalid size."}]
    bbox = (rect["left"], rect["top"], rect["right"], rect["bottom"])
    frames = []
    for index in range(1, 4):
        name = f"companion_frame_{index}.png"
        path = os.path.join(bundle_dir, name)
        try:
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
            image.save(path, "PNG")
            frames.append({"file": name, "width": image.width, "height": image.height})
        except Exception as ex:
            frames.append({"file": name, "error": f"{type(ex).__name__}: {ex}"})
        if index < 3:
            time.sleep(0.5)
    return frames


def main() -> None:
    os.makedirs(DIAG_DIR, exist_ok=True)
    bundle_dir = os.path.join(DIAG_DIR, "companion_bundle_" + _stamp())
    os.makedirs(bundle_dir, exist_ok=True)

    windows = _windows()
    frames = _capture_frames(bundle_dir, windows)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "privacy": {
            "includes_companion_conversation": True,
            "includes_companion_window_images": True,
            "includes_full_desktop": False,
            "includes_activity_databases": False,
            "includes_clipboard_or_ocr_payloads": False,
        },
        "runtime": {
            "python": sys.version,
            "executable": sys.executable,
            "platform": platform.platform(),
            "pywebview": _package_version("pywebview"),
            "pillow": _package_version("Pillow"),
            "psutil": _package_version("psutil"),
        },
        "windows": windows,
        "frames": frames,
        "processes": _companion_processes(),
        "sources": [_file_info(path) for path in SOURCE_PATHS],
        "ui_state_present": os.path.exists(UI_STATE_PATH),
    }
    _write(os.path.join(bundle_dir, "summary.json"), json.dumps(summary, indent=2))
    _write(os.path.join(bundle_dir, "companion_error_tail.log"), _tail(COMPANION_LOG))

    if os.path.exists(UI_STATE_PATH):
        shutil.copy2(UI_STATE_PATH, os.path.join(bundle_dir, "companion_ui_state.json"))
    else:
        _write(
            os.path.join(bundle_dir, "companion_ui_state_missing.txt"),
            "Restart the companion so its browser-side diagnostic reporter can initialize.\n",
        )

    for source in SOURCE_PATHS:
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(bundle_dir, os.path.basename(source)))

    zip_path = bundle_dir + ".zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(os.listdir(bundle_dir)):
            archive.write(os.path.join(bundle_dir, name), arcname=name)
    shutil.rmtree(bundle_dir, ignore_errors=True)

    print("Companion diagnostics bundle created:")
    print(zip_path)
    print("")
    print("It includes the companion conversation and cropped companion screenshots.")


if __name__ == "__main__":
    main()
