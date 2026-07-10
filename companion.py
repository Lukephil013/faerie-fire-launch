"""Faerie Fire — legacy standalone Companion window.

The supported chat surface now lives inside the GUI Command Center. This
standalone window is locked by default to avoid two chat surfaces racing with
stale in-memory history. Set `legacy_companion = true` in config.toml only if
you intentionally want the old window during development.

Run:  python companion.py        (see errors live in the terminal)
  or: double-click Companion.bat (uses pythonw; errors go to companion_error.log)
Requires:  pip install pywebview
"""
from __future__ import annotations

import base64
import json
import os
import threading
import traceback
from datetime import datetime, timezone

from livingpc.config import load
from livingpc.companion import personas as personas_mod
from livingpc.diagnostics import DIAG_DIR

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "livingpc", "companion", "companion_error.log")
UI_STATE_PATH = os.path.join(DIAG_DIR, "companion_ui_state.json")


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


class Api:
    """Bridge exposed to the chat window as pywebview.api.*. Plain text chat —
    no voice input/output; see the module docstring.

    `send` runs on pywebview's own worker thread and RETURNS the reply directly,
    so Python never calls back into the page from another thread (that pattern is
    unstable on WebView2). The JS awaits the returned value.
    """

    def __init__(self):
        self.companion = None          # lazy — created on first message
        self.persona_key = "companion"
        self.cfg = load("config.toml")
        self._window = None            # set after create_window, see main()
        self._ui_state_lock = threading.Lock()
        self._companion_lock = threading.Lock()

    def _ensure(self):
        # pywebview dispatches each js_api call on its own worker thread. On
        # first load, the dashboard's command_chat_state() and the chat's
        # own first send/new_chat can land within milliseconds of each other
        # — without a lock, both threads see self.companion as None, both
        # construct a fresh Companion (each reading empty chat history), and
        # both persist the calibration opener message, producing a visible
        # duplicate "1/13" question. The lock makes construction atomic.
        if self.companion is None:
            with self._companion_lock:
                if self.companion is None:
                    from livingpc.companion.brain import Companion
                    self.companion = Companion(cfg=self.cfg, persona_key=self.persona_key)
        return self.companion

    def _respond(self, text, attachments=None) -> dict:
        """Shared pipeline: brain reply -> result dict for the UI."""
        c = None
        try:
            c = self._ensure()
            reply = c.reply(text, attachments=attachments)
            color = c.persona.color
        except Exception:
            log("respond error:\n" + traceback.format_exc())
            reply = "(couldn't reach the brain — is ANTHROPIC_API_KEY set? see companion_error.log)"
            color = personas_mod.get_persona(self.persona_key).color
        return {"user": text, "text": reply, "color": color,
                "active_chat_id": c.chat_id if c else None,
                "chats": c.list_chats() if c else [],
                "pending_proposal": c.pending_proposal() if c else None}

    # --- text path --------------------------------------------------------
    def send(self, text, attachments=None):
        return self._respond(text, attachments)

    # --- attachments (files + pasted photos) -------------------------------
    _IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".gif": "image/gif",
                    ".webp": "image/webp"}
    _MAX_IMAGE_BYTES = 4_500_000       # API limit is ~5MB per image
    _MAX_TEXT_CHARS = 40_000

    def attach_file(self):
        """Native file picker -> attachment dict for the UI to hold until send."""
        if self._window is None:
            return {"ok": False, "message": "window not ready"}
        try:
            import webview
            paths = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False)
            if not paths:
                return {"ok": False, "message": ""}   # user cancelled
            path = paths[0] if isinstance(paths, (list, tuple)) else paths
            return self._load_attachment(path)
        except Exception:
            log("attach_file error:\n" + traceback.format_exc())
            return {"ok": False, "message": "couldn't open that file"}

    def _load_attachment(self, path) -> dict:
        """Turn a path into an attachment: images pass through as base64 for
        the vision API; .docx and text-like files become extracted text."""
        name = os.path.basename(str(path))
        ext = os.path.splitext(name)[1].lower()
        with open(path, "rb") as f:
            data = f.read()
        if ext in self._IMAGE_TYPES:
            if len(data) > self._MAX_IMAGE_BYTES:
                return {"ok": False,
                        "message": f"{name} is too large for the vision API (max ~4.5MB)"}
            return {"ok": True, "attachment": {
                "kind": "image", "name": name,
                "media_type": self._IMAGE_TYPES[ext],
                "data": base64.b64encode(data).decode("ascii")}}
        if ext == ".docx":
            from livingpc.docx_text import docx_to_text
            text = docx_to_text(data)
        else:
            if b"\x00" in data[:4096]:
                return {"ok": False,
                        "message": f"{name} looks binary — attach images, .docx, or text files"}
            text = data.decode("utf-8", errors="replace")
        return {"ok": True, "attachment": {
            "kind": "text", "name": name,
            "text": text[: self._MAX_TEXT_CHARS]}}

    def chat_state(self):
        """Return the active chat and its persisted history."""
        try:
            c = self._ensure()
            return {"ok": True, "active_chat_id": c.chat_id,
                    "chats": c.list_chats(), "messages": c.history,
                    "pending_proposal": c.pending_proposal()}
        except Exception:
            log("chat_state error:\n" + traceback.format_exc())
            return {"ok": False, "chats": [], "messages": []}

    def calibration_status(self):
        """Snapshot of Soul Calibration progress for a small UI checklist."""
        try:
            return {"ok": True, **self._ensure().calibration_status()}
        except Exception:
            log("calibration_status error:\n" + traceback.format_exc())
            return {"ok": False, "sections": [], "done": 0, "total": 0, "complete": False}

    def calibration_save(self, section, attribute, value, skip=False):
        """Save one Soul Calibration answer from the standalone popout drawer."""
        try:
            return {"ok": True, **self._ensure().calibration_save(
                section, attribute, value, bool(skip))}
        except Exception:
            log("calibration_save error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not save that."}

    def calibration_reset(self):
        """Retire every saved Soul Calibration fact so all 13 resurface."""
        try:
            return {"ok": True, **self._ensure().calibration_reset()}
        except Exception:
            log("calibration_reset error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not reset calibration."}

    def calibration_synthesis(self):
        """One-off model call: intro + reflection posted into the active chat
        right after the popout finishes."""
        try:
            return self._ensure().calibration_synthesis()
        except Exception:
            log("calibration_synthesis error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not generate that."}

    def new_chat(self):
        try:
            self._ensure().new_chat()
            return self.chat_state()
        except Exception:
            log("new_chat error:\n" + traceback.format_exc())
            return {"ok": False, "chats": [], "messages": []}

    def switch_chat(self, chat_id):
        try:
            c = self._ensure()
            if not c.switch_chat(str(chat_id)):
                return {"ok": False, "message": "Chat not found."}
            return self.chat_state()
        except Exception:
            log("switch_chat error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not open chat."}

    def delete_chat(self, chat_id):
        try:
            c = self._ensure()
            if not c.delete_chat(str(chat_id)):
                return {"ok": False, "message": "Chat not found."}
            return self.chat_state()
        except Exception:
            log("delete_chat error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not delete chat."}

    def rename_chat(self, chat_id, title):
        try:
            c = self._ensure()
            if not c.rename_chat(str(chat_id), title):
                return {"ok": False, "message": "Chat not found."}
            return self.chat_state()
        except Exception:
            log("rename_chat error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not rename chat."}

    # --- project docs (filing engine; see livingpc/filing.py) ---------------
    def list_projects(self):
        """Docs in the projects folder, for the Projects tab."""
        try:
            from livingpc import filing
            docs = filing.projects_overview(filing.projects_dir_for(self.cfg))
            return {"ok": True, "docs": docs}
        except Exception:
            log("list_projects error:\n" + traceback.format_exc())
            return {"ok": False, "docs": []}

    def read_project(self, slug):
        """Full text of one project doc (read-only view)."""
        try:
            from livingpc import filing
            projects_dir = filing.projects_dir_for(self.cfg)
            safe = filing.slug(os.path.basename(str(slug)))
            path = os.path.join(projects_dir, f"{safe}.md")
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return {"ok": True, "slug": safe, "text": f.read()}
        except Exception:
            log("read_project error:\n" + traceback.format_exc())
            return {"ok": False, "slug": str(slug),
                    "text": "(couldn't read that doc — see companion_error.log)"}

    def open_projects_folder(self):
        """Open the projects folder in Explorer (created if missing)."""
        try:
            from livingpc import filing
            projects_dir = filing.projects_dir_for(self.cfg)
            os.makedirs(projects_dir, exist_ok=True)
            opener = getattr(os, "startfile", None)  # Windows-only
            if opener is None:
                return False
            opener(projects_dir)
            return True
        except Exception:
            log("open_projects_folder error:\n" + traceback.format_exc())
            return False

    # --- window control -----------------------------------------------------
    def toggle_maximize(self):
        """The window is frameless (no native title bar), so the maximize
        button in the header drives this instead. pywebview has no separate
        maximize/restore pair — toggle_fullscreen() is the standard way to
        give a chromeless window a maximize affordance."""
        if self._window is None:
            return False
        try:
            self._window.toggle_fullscreen()
            return True
        except Exception:
            log("toggle_maximize error:\n" + traceback.format_exc())
            return False

    def minimize(self):
        if self._window is None:
            return False
        try:
            self._window.minimize()
            return True
        except Exception:
            log("minimize error:\n" + traceback.format_exc())
            return False

    # --- proactive reflection --------------------------------------------
    def get_reflection(self):
        """Return a belief for the companion to volunteer back (or {} if none
        due). The UI shows it with a refine box; already marked so it won't
        repeat immediately."""
        try:
            c = self._ensure()
            return c.maybe_reflection() or {}
        except Exception:
            log("reflection error:\n" + traceback.format_exc())
            return {}

    def refine_reflection(self, inference_id, text):
        """Save the user's rewrite of a reflected belief as the new truth."""
        try:
            from livingpc.inference_review import InferenceReview
            rev = InferenceReview(self.cfg.memory_db_path)
            try:
                rev.answer("refine", int(inference_id), text)
            finally:
                rev.close()
            return True
        except Exception:
            log("refine reflection error:\n" + traceback.format_exc())
            return False

    def set_persona(self, key):
        self.persona_key = key
        if self.companion is not None:
            self.companion.set_persona(key)
        p = personas_mod.get_persona(key)
        return {"key": p.key, "color": p.color}

    def list_personas(self):
        return personas_mod.list_personas()

    def report_ui_state(self, state):
        """Persist a sanitized browser-side snapshot for diagnostics bundles."""
        if not isinstance(state, dict):
            return False
        try:
            os.makedirs(DIAG_DIR, exist_ok=True)
            payload = {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "python": {
                    "pid": os.getpid(),
                    "persona": self.persona_key,
                    "brain_initialized": self.companion is not None,
                },
                # The browser sends only layout/count diagnostics. Defensively
                # retain an allow-list so chat content can never enter a bundle.
                "ui": {key: state.get(key) for key in (
                    "messageCount", "messageShapes", "activeChatId", "chatCount",
                    "viewport", "screen", "rects", "log", "lastError", "events",
                )},
            }
            with self._ui_state_lock:
                temp_path = UI_STATE_PATH + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=True)
                os.replace(temp_path, UI_STATE_PATH)
            return True
        except Exception:
            log("ui state report error:\n" + traceback.format_exc())
            return False


def main():
    try:
        import webview
        api = Api()
        if not getattr(api.cfg, "legacy_companion", False):
            message = (
                "Standalone companion is retired. Open the Faerie Fire GUI "
                "Command Center instead (`python gui.py --view command-center`). "
                "Set legacy_companion=true only for development."
            )
            log(message)
            print(message)
            return
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "livingpc", "companion", "companion.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        asset_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "livingpc", "companion",
            "assets", "companion_avatar.jpg",
        )
        with open(asset_path, "rb") as f:
            avatar_data = base64.b64encode(f.read()).decode("ascii")
        html = html.replace(
            "{{AVATAR_DATA_URL}}", f"data:image/jpeg;base64,{avatar_data}",
        )
        # Same leafy background photo as the Memory GUI, for a consistent look
        # across windows. Best-effort: an empty data URL just leaves the plain
        # dark gradient behind if the asset is ever missing.
        background_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "livingpc", "ui",
            "assets", "backgrounds", "forest-ruins-main.jpg",
        )
        try:
            with open(background_path, "rb") as f:
                background_data = base64.b64encode(f.read()).decode("ascii")
            background_url = f"data:image/jpeg;base64,{background_data}"
        except OSError:
            background_url = ""
        html = html.replace("{{BACKGROUND_DATA_URL}}", background_url)
        window = webview.create_window(
            "Faerie Fire", html=html, js_api=api,
            width=620, height=620, min_size=(440, 420),
            frameless=True, easy_drag=False,
            on_top=True, resizable=True, background_color="#06070f",
        )
        api._window = window
        log("--- starting webview ---")
        webview.start()
    except Exception:
        log("startup error:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
