"""Faerie Fire — real-time assistant (global hotkey + popup, pywebview).

Press the hotkey (default Ctrl+Shift+Space) anywhere: a small always-on-top box
appears. Type a question, hit Enter, and Claude answers using your CURRENT SCREEN
(image), the redacted on-screen text, and what your second brain knows about you.

The page lives in livingpc/ui/assistant.html; this file is the js_api bridge and
the Win32 hotkey loop. `ask` blocks on pywebview's worker thread and returns the
answer directly (the JS awaits the promise); the hotkey thread only calls
window.show()/hide(), which pywebview marshals to the UI thread itself.

Run:  python assistant.py        (or double-click "Ask Assistant.bat")
Esc hides the box; the hotkey brings it back.
"""
from __future__ import annotations

import threading
import traceback

from livingpc import assist
from livingpc.capture.screen import ScreenCapturer
from livingpc.config import load
from livingpc.memory import MemoryStore
from livingpc.sysinfo import get_backend
from livingpc.triage.redact import redact


# --- hotkey parsing -------------------------------------------------------
_MODS = {"ctrl": 0x0002, "control": 0x0002, "alt": 0x0001, "shift": 0x0004,
         "win": 0x0008, "super": 0x0008}
_KEYS = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B}


def parse_hotkey(spec: str):
    """'ctrl+shift+space' -> (mod_flags, virtual_key). Returns (0, None) on failure."""
    mod, vk = 0, None
    for part in spec.lower().split("+"):
        part = part.strip()
        if part in _MODS:
            mod |= _MODS[part]
        elif part in _KEYS:
            vk = _KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x70 + int(part[1:]) - 1   # F1..F12
    return mod, vk


class AssistantApi:
    """Bridge exposed to the page as pywebview.api.*"""

    def __init__(self, cfg=None):
        self.cfg = cfg or load("config.toml")
        self._window = None                # set after create_window
        self.sys_backend = get_backend()
        self.capturer = ScreenCapturer(self.cfg.blob_dir,
                                       ocr_enabled=self.cfg.ocr_enabled)

    # --- window control -----------------------------------------------------
    def hide(self) -> bool:
        if self._window is not None:
            self._window.hide()
        return True

    def show(self) -> bool:
        if self._window is not None:
            self._window.show()
            self._window.on_top = True
        return True

    # --- ask flow -------------------------------------------------------------
    def ask(self, question) -> dict:
        """Capture screen + memory context, ask Claude, return the answer.
        Blocks on the worker thread; the JS shows a spinner meanwhile."""
        question = str(question or "").strip()
        if not question:
            return {"answer": "", "note": ""}
        try:
            img = self.capturer.grab()
            app, _ = self.sys_backend.foreground()
            note, image_b64 = "", None
            if app in self.cfg.blocklist:
                note = f"(Screen not sent — “{app}” is on your blocklist.)"
            else:
                image_b64 = assist.encode_jpeg_b64(img)
            ocr = ""
            if image_b64:
                try:
                    ocr = redact(self.capturer.ocr(img) or "")
                except Exception:
                    ocr = ""
            mem = MemoryStore(self.cfg.memory_db_path)
            try:
                memories = mem.active_as_dicts()
            finally:
                mem.close()
            answer = assist.answer(
                question, image_b64, ocr, memories,
                model=self.cfg.assistant_model,
                memory_max_items=self.cfg.assistant_memory_max_items,
                memory_max_chars=self.cfg.assistant_memory_max_chars,
                memory_value_max_chars=self.cfg.assistant_memory_value_max_chars)
            return {"answer": answer, "note": note}
        except Exception as error:
            traceback.print_exc()
            return {"error": f"{type(error).__name__}: {error}"}


def _hotkey_loop(api: AssistantApi, mod: int, vk: int) -> None:
    """Win32 RegisterHotKey message loop (background thread)."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return
    user32 = ctypes.windll.user32
    WM_HOTKEY = 0x0312
    MOD_NOREPEAT = 0x4000
    if not user32.RegisterHotKey(None, 1, mod | MOD_NOREPEAT, vk):
        print("[assistant] hotkey is already in use by another app; window stays visible.")
        api.show()
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            api.show()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def main():
    import webview
    from livingpc.ui import load_html
    api = AssistantApi()
    window = webview.create_window(
        "Faerie Fire — Ask", html=load_html("assistant.html"), js_api=api,
        width=620, height=460, min_size=(420, 320),
        frameless=True, easy_drag=False, on_top=True, hidden=True,
        background_color="#06070f",
    )
    api._window = window

    def after_start():
        mod, vk = parse_hotkey(api.cfg.assistant_hotkey)
        if not vk:
            print("[assistant] could not parse hotkey; window stays visible.")
            api.show()
            return
        threading.Thread(target=_hotkey_loop, args=(api, mod, vk),
                         daemon=True).start()
        print(f"[assistant] ready. Hotkey: {api.cfg.assistant_hotkey}")

    webview.start(after_start)


if __name__ == "__main__":
    main()
