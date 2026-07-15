"""Faerie Fire — legacy standalone Companion window.

The supported chat surface now lives inside the GUI Command Center. This
standalone window is locked by default to avoid two chat surfaces racing with
stale in-memory history. Set `legacy_companion = true` in config.toml only if
you intentionally want the old window during development.

NOTE (unified build): although the standalone window is retired, this module's
`Api` class IS the chat bridge the GUI Command Center imports lazily
(`from companion import Api as CompanionApi` in gui.py). Do not delete this
file — removing it breaks Command Center chat with
"ModuleNotFoundError: No module named 'companion'".

Run:  python companion.py        (see errors live in the terminal)
Requires:  pip install pywebview
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
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
        self._browser_service = None

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
        browser_state = self.browser_state()
        return {"user": text, "text": reply, "color": color,
                "active_chat_id": c.chat_id if c else None,
                "proposals_enabled": c.proposals_enabled if c else True,
                "chats": c.list_chats() if c else [],
                "pending_proposal": c.pending_proposal() if c else None,
                "pending_proposals": c.pending_proposals() if c else [],
                "browser_tasks": browser_state.get("tasks", [])}

    # --- text path --------------------------------------------------------
    def send(self, text, attachments=None):
        return self._respond(text, attachments)

    # --- attachments (files + pasted photos) -------------------------------
    _IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg", ".gif": "image/gif",
                    ".webp": "image/webp"}
    _MAX_IMAGE_BYTES = 4_500_000       # API limit is ~5MB per image
    _MAX_TEXT_CHARS = 40_000
    _MAX_DROP_BYTES = 20_000_000       # bound base64 transfer through WebView2

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
        """Turn a path into an attachment.

        Images pass through as base64 for vision. Documents and spreadsheets
        are extracted locally before any content is sent to the model.
        """
        name = os.path.basename(str(path))
        ext = os.path.splitext(name)[1].lower()
        if ext in self._IMAGE_TYPES:
            with open(path, "rb") as f:
                data = f.read()
            if len(data) > self._MAX_IMAGE_BYTES:
                return {"ok": False,
                        "message": f"{name} is too large for the vision API (max ~4.5MB)"}
            return {"ok": True, "attachment": {
                "kind": "image", "name": name,
                "media_type": self._IMAGE_TYPES[ext],
                "data": base64.b64encode(data).decode("ascii")}}
        try:
            from livingpc.context_attachment import extract_document
            extracted = extract_document(str(path))
        except Exception as error:
            return {"ok": False, "message": f"Could not read {name}: {error}"}
        return {"ok": True, "attachment": {
            "kind": "text", "name": extracted["name"],
            "media_type": extracted["media_type"],
            "text": extracted["text"][: self._MAX_TEXT_CHARS],
            "char_count": min(extracted["char_count"], self._MAX_TEXT_CHARS),
            "truncated": bool(extracted.get("truncated") or
                              extracted["char_count"] > self._MAX_TEXT_CHARS)}}

    def attach_dropped_file(self, name, media_type, data_base64) -> dict:
        """Load a browser-dropped file through the native attachment path.

        WebView file objects do not reliably expose a usable Windows path, so
        the page sends bounded base64 bytes. A private temporary file preserves
        support for PDF/Word/Excel extractors that require a path; it is always
        removed before this call returns.
        """
        safe_name = os.path.basename(str(name or "dropped-file")).strip()
        if not safe_name:
            safe_name = "dropped-file"
        try:
            payload = base64.b64decode(str(data_base64 or ""), validate=True)
        except Exception:
            return {"ok": False, "message": f"Could not read {safe_name}: invalid file data"}
        if not payload:
            return {"ok": False, "message": f"{safe_name} is empty"}
        if len(payload) > self._MAX_DROP_BYTES:
            return {"ok": False,
                    "message": f"{safe_name} is too large to drop here (max 20MB)"}
        suffix = os.path.splitext(safe_name)[1].lower()
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                    prefix="faerie-chat-drop-", suffix=suffix, delete=False) as handle:
                path = handle.name
                handle.write(payload)
            result = self._load_attachment(path)
            if result.get("ok") and result.get("attachment"):
                result["attachment"]["name"] = safe_name
                if media_type and not result["attachment"].get("media_type"):
                    result["attachment"]["media_type"] = str(media_type)
            return result
        except Exception as error:
            return {"ok": False, "message": f"Could not read {safe_name}: {error}"}
        finally:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def chat_state(self):
        """Return the active chat and its persisted history."""
        try:
            c = self._ensure()
            browser_state = self.browser_state()
            return {"ok": True, "active_chat_id": c.chat_id,
                    "proposals_enabled": c.proposals_enabled,
                    "chats": c.list_chats(), "messages": c.history,
                    "pending_proposal": c.pending_proposal(),
                    "pending_proposals": c.pending_proposals(),
                    "browser_tasks": browser_state.get("tasks", [])}
        except Exception:
            log("chat_state error:\n" + traceback.format_exc())
            return {"ok": False, "chats": [], "messages": []}

    def approve_proposal(self, index):
        """Apply one card from a multi-proposal response without a model call."""
        try:
            c = self._ensure()
            c.approve_proposal(int(index))
            return self.chat_state()
        except Exception:
            log("approve_proposal error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not approve that proposal."}

    def dismiss_proposal(self, index):
        """Remove one pending proposal without applying it."""
        try:
            c = self._ensure()
            if not c.dismiss_proposal(int(index)):
                return {"ok": False, "message": "That proposal is no longer pending."}
            return self.chat_state()
        except Exception:
            log("dismiss_proposal error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not dismiss that proposal."}

    def commands(self):
        try:
            return {"ok": True, "commands": self._ensure().available_commands()}
        except Exception:
            log("commands error:\n" + traceback.format_exc())
            return {"ok": False, "commands": []}

    # --- guarded browser form assistant -----------------------------------
    def _browser(self):
        if not getattr(self.cfg, "browser_assistant_enabled", True):
            return None
        if self._browser_service is None:
            from livingpc.browser_assistant import BrowserAssistant
            self._browser_service = BrowserAssistant(self.cfg)
        return self._browser_service

    def browser_state(self):
        try:
            service = self._browser()
            return service.state() if service else {"ok": True, "tasks": [], "permissions": []}
        except Exception:
            log("browser_state error:\n" + traceback.format_exc())
            return {"ok": False, "tasks": [], "permissions": [],
                    "message": "Browser assistance is unavailable."}

    def browser_approve_domain(self, task_id):
        return self._browser_action("approve_domain", task_id)

    def browser_open(self, task_id):
        return self._browser_action("open", task_id)

    def browser_scan(self, task_id):
        try:
            service = self._browser()
            if not service:
                return {"ok": False, "message": "Browser assistance is disabled."}
            companion = self._ensure()
            service.scan_and_plan(str(task_id), companion._skill_ctx()["llm"])
            return service.state()
        except Exception as error:
            log("browser_scan error:\n" + traceback.format_exc())
            return {"ok": False, "message": f"Could not scan that form: {type(error).__name__}"}

    def browser_fill(self, task_id):
        return self._browser_action("fill", task_id)

    def browser_cancel(self, task_id):
        return self._browser_action("close", task_id, completed=False)

    def browser_finish(self, task_id):
        return self._browser_action("close", task_id, completed=True)

    def browser_revoke(self, origin):
        return self._browser_action("revoke", str(origin))

    def _browser_action(self, name, *args, **kwargs):
        try:
            service = self._browser()
            if not service:
                return {"ok": False, "message": "Browser assistance is disabled."}
            getattr(service, name)(*args, **kwargs)
            return service.state()
        except Exception as error:
            log(f"browser_{name} error:\n" + traceback.format_exc())
            return {"ok": False, "message": f"Browser action failed: {type(error).__name__}"}

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

    def new_chat(self, proposals_enabled=True):
        try:
            self._ensure().new_chat(bool(proposals_enabled))
            return self.chat_state()
        except Exception:
            log("new_chat error:\n" + traceback.format_exc())
            return {"ok": False, "chats": [], "messages": []}

    def set_chat_proposals_enabled(self, enabled):
        try:
            c = self._ensure()
            if not c.set_proposals_enabled(bool(enabled)):
                return {"ok": False, "message": "Chat not found."}
            return self.chat_state()
        except Exception:
            log("set_chat_proposals_enabled error:\n" + traceback.format_exc())
            return {"ok": False, "message": "Could not change proposal mode."}

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
