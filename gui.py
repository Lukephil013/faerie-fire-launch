"""Faerie Fire — the desktop app (pywebview).

Two views, one window, same design language as the companion:
  * Inferences — hypotheses above the confidence gate open persistent Address
    conversations; directed investigations use relevant evidence and only an
    explicit user decision creates a canonical belief.
  * Memory     — what the second brain currently believes, grouped by category,
    searchable, with a history toggle for superseded facts.

Facts are auto-committed by the nightly triage (confident ones only); there's no
manual fact-approval or scheduling here — the background daemon handles cadence.

The page lives in livingpc/ui/memory.html; this file is the js_api bridge.
Every bridge call opens its own stores (pywebview invokes js_api methods on
worker threads, and SQLite connections are thread-bound), and long calls simply
block and return — Python never pushes into the page from another thread.

Run: python gui.py   (or double-click "Memory GUI.bat")
"""
from __future__ import annotations

import base64
import argparse
import os
import subprocess
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timedelta

from livingpc import crypto, onboarding
from livingpc.config import load
from livingpc.diagnostics import log_diag
from livingpc.lang import T, app_language, is_language_set, set_app_language
from livingpc.inference_review import InferenceReview
from livingpc.memory import MemoryStore

# Populate ANTHROPIC_API_KEY from onboarding storage (if any) before anything
# else touches it — every backend module falls back to this env var, so this
# one call is the entire integration point for a launch-profile stored key.
onboarding.apply_stored_key()


def _normalize_initial_view(value: str | None) -> str:
    """Map public entrypoint names onto the internal view ids used by memory.html."""
    view = str(value or "self").strip().lower().replace("_", "-")
    aliases = {
        "command-center": "self",
        "command": "self",
        "dashboard": "self",
        "self": "self",
        "investigations": "curiosity",
        "curiosities": "curiosity",
        "growth": "goals",
    }
    view = aliases.get(view, view)
    allowed = {
        "self", "inferences", "clarify", "memory", "timeline",
        "import", "curiosity", "goals",
    }
    return view if view in allowed else "self"


class GuiApi:
    """Bridge exposed to the page as pywebview.api.*"""

    def __init__(self, cfg=None, initial_view: str = "self"):
        self.cfg = cfg or load("config.toml")
        self.gate = float(getattr(self.cfg, "inference_surface_confidence", 0.80))
        self.initial_view = _normalize_initial_view(initial_view)
        # Keep pywebview's native Window private. pywebview 5.2 recursively
        # scans public js_api attributes before startup, and a public Window
        # object can make it evaluate JS before the main window exists.
        self._window = None
        self._command_companion = None

    def app_bootstrap(self) -> dict:
        """Small public UI bootstrap: profile + initial view only, no payloads."""
        log_diag("gui_bootstrap", f"app_bootstrap called cwd={os.getcwd()}")
        try:
            profile = getattr(self.cfg, "profile", "personal")
            result = {
                "ok": True,
                "profile": profile,
                "initial_view": self.initial_view,
                "backend": getattr(self.cfg, "companion_backend", "?"),
                "inference_backend": getattr(self.cfg, "inference_backend", "?"),
                # Unified build: the UI enables the Korean layer when "ko".
                "language": app_language(),
                "language_set": is_language_set(),
                # Onboarding only exists for launch profile — a personal install
                # always manages its own key via the environment, as before.
                "needs_onboarding": profile == "launch" and not onboarding.is_complete(),
            }
            log_diag("gui_bootstrap", f"app_bootstrap ok profile={profile}")
            return result
        except Exception as error:
            log_diag("gui_bootstrap",
                     f"app_bootstrap FAILED: {type(error).__name__}: {error}\n"
                     + traceback.format_exc())
            return {
                "ok": False,
                "profile": getattr(self.cfg, "profile", "personal"),
                "initial_view": self.initial_view,
                "backend": "?", "inference_backend": "?",
                "needs_onboarding": False,
                "bootstrap_error": f"{type(error).__name__}: {error}",
            }

    def app_usage_summary(self) -> dict:
        """Today's model usage (calls + estimated cost) for the Settings drawer,
        so one shared key across machines doesn't turn into a surprise bill."""
        try:
            from livingpc.llm_usage import daily_summary
            return {"ok": True, **daily_summary()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def app_set_language(self, lang) -> dict:
        """Persist the UI/model language ("en" or "ko"); picked at onboarding
        and changeable later. The JS layer applies it live; Python strings and
        the window title pick it up fully on next launch."""
        try:
            return {"ok": True, "language": set_app_language(str(lang or ""))}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    # --- onboarding (launch profile first run) -----------------------------
    def onboarding_status(self) -> dict:
        return {
            "ok": True,
            "has_key": bool(os.environ.get("ANTHROPIC_API_KEY")) or onboarding.has_stored_key(),
            "complete": onboarding.is_complete(),
        }

    def onboarding_validate_key(self, key) -> dict:
        ok, message = onboarding.validate_api_key(str(key or ""))
        return {"ok": ok, "message": message}

    def onboarding_save_key(self, key) -> dict:
        ok, message = onboarding.validate_api_key(str(key or ""))
        if not ok:
            return {"ok": False, "message": message}
        try:
            onboarding.save_api_key(str(key))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def onboarding_create_soul(self, title, purpose) -> dict:
        """The 'create-your-Soul moment': name and describe the umbrella node
        that already auto-exists (GoalStore._ensure_root creates it as
        'Actualized Self' on first open), then seed one starter investigation."""
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            title = str(title or "").strip() or T("Actualized Self", "실현된 나")
            purpose = str(purpose or "").strip()
            store.update(store.root_id, title=title, description=purpose)
            investigation_id = onboarding.seed_example_investigation(
                self.cfg.memory_db_path, soul_title=title, soul_purpose=purpose)
            onboarding.mark_complete()
            return {"ok": True, "tree": store.tree(), "investigation_id": investigation_id}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def onboarding_skip(self) -> dict:
        """Escape hatch: mark onboarding done without a key so the app is at
        least reachable (Command Center chat will surface the missing-key
        error itself when actually used)."""
        onboarding.mark_complete()
        return {"ok": True}

    def _command_api(self):
        """Lazy embedded companion bridge for the Command Center."""
        if self._command_companion is None:
            from companion import Api as CompanionApi
            api = CompanionApi()
            api.cfg = self.cfg
            api._window = self._window
            self._command_companion = api
        return self._command_companion

    def command_chat_state(self) -> dict:
        return self._command_api().chat_state()

    def command_send(self, text, attachments=None) -> dict:
        return self._command_api().send(text, attachments or [])

    def command_new_chat(self) -> dict:
        return self._command_api().new_chat()

    def command_switch_chat(self, chat_id) -> dict:
        return self._command_api().switch_chat(chat_id)

    def command_delete_chat(self, chat_id) -> dict:
        return self._command_api().delete_chat(chat_id)

    def command_rename_chat(self, chat_id, title) -> dict:
        return self._command_api().rename_chat(chat_id, title)

    _BACKGROUND_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    def background_images(self) -> dict:
        """List project background art, in folder order, so
        cards (pursuits, investigations) can each get a distinct image and
        naturally pick up anything dropped in later."""
        try:
            from livingpc.ui import UI_DIR
            project_assets = os.path.abspath(os.path.join(UI_DIR, "..", "..", "assets"))
            ui_backgrounds = os.path.join(UI_DIR, "assets", "backgrounds")
            sources = [
                (project_assets, "../../assets/"),
                (ui_backgrounds, "assets/backgrounds/"),
            ]
            for folder, prefix in sources:
                if not os.path.isdir(folder):
                    continue
                names = sorted(
                    name for name in os.listdir(folder)
                    if os.path.splitext(name)[1].lower() in self._BACKGROUND_IMAGE_TYPES
                )
                if names:
                    return {"ok": True, "images": [prefix + name for name in names]}
            return {"ok": True, "images": []}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}", "images": []}

    def command_calibration_status(self) -> dict:
        return self._command_api().calibration_status()

    def command_calibration_save(self, section, attribute, value, skip=False) -> dict:
        return self._command_api().calibration_save(section, attribute, value, skip)

    def command_calibration_reset(self) -> dict:
        return self._command_api().calibration_reset()

    def command_calibration_synthesis(self) -> dict:
        return self._command_api().calibration_synthesis()

    def command_attach_file(self) -> dict:
        api = self._command_api()
        api._window = self._window
        return api.attach_file()

    def _validate_context_attachment_owner(self, owner_kind, owner_key) -> tuple[str, str]:
        kind, key = str(owner_kind or "").strip(), str(owner_key or "").strip()
        if kind == "soul_calibration":
            from livingpc import soul_calibration
            valid = {soul_calibration.field_key(field) for field in soul_calibration.FIELDS}
            if key not in valid:
                raise ValueError("Soul Calibration question not found")
        elif kind in {"curiosity", "curiosity_item"}:
            from livingpc.curiosity import CuriosityStore
            store = CuriosityStore(self.cfg.memory_db_path)
            try:
                row = (store.get_curiosity(int(key)) if kind == "curiosity"
                       else store.get_item(int(key)))
            finally:
                store.close()
            if row is None:
                raise ValueError("Investigation context owner not found")
            key = str(int(key))
        else:
            raise ValueError("unsupported context attachment owner")
        return kind, key

    def context_attachment_add(self, owner_kind, owner_key) -> dict:
        """Pick one document, extract it locally, and encrypt it for one owner."""
        try:
            kind, key = self._validate_context_attachment_owner(owner_kind, owner_key)
            if self._window is None:
                raise ValueError("window not ready")
            import webview
            paths = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False)
            if not paths:
                return {"ok": False, "message": ""}
            path = paths[0] if isinstance(paths, (list, tuple)) else paths
            from livingpc.context_attachment import ContextAttachmentStore
            store = ContextAttachmentStore(self.cfg.memory_db_path)
            try:
                attachment = store.add_document(kind, key, str(path))
            finally:
                store.close()
            return {"ok": True, "attachment": attachment}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def context_attachment_remove(self, attachment_id, owner_kind, owner_key) -> dict:
        try:
            kind, key = self._validate_context_attachment_owner(owner_kind, owner_key)
            from livingpc.context_attachment import ContextAttachmentStore
            store = ContextAttachmentStore(self.cfg.memory_db_path)
            try:
                removed = store.remove(int(attachment_id), kind, key)
            finally:
                store.close()
            return {"ok": True, "removed": removed}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    # --- self-portrait (image + ambient effect on the Command Center dash) -
    _PORTRAIT_ANIMATIONS = {
        "still", "sunshafts", "motes", "fireflies", "breeze", "rain", "dreamlight", "living",
    }
    _PORTRAIT_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                             ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
    _PORTRAIT_MAX_BYTES = 8_000_000

    def _portrait_dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.cfg.memory_db_path))

    def _portrait_image_path(self, mem: MemoryStore) -> str | None:
        stored = mem.get_meta("portrait_image_path")
        if stored and os.path.isfile(stored):
            return stored
        return None

    def self_portrait_state(self) -> dict:
        try:
            mem = MemoryStore(self.cfg.memory_db_path)
            try:
                animation = mem.get_meta("portrait_animation", "still") or "still"
                path = self._portrait_image_path(mem)
            finally:
                mem.close()
            image_data_url = None
            if path:
                ext = os.path.splitext(path)[1].lower()
                media_type = self._PORTRAIT_IMAGE_TYPES.get(ext, "image/jpeg")
                with open(path, "rb") as handle:
                    image_data_url = (f"data:{media_type};base64,"
                                       + base64.b64encode(handle.read()).decode("ascii"))
            return {"ok": True, "animation": animation, "image_data_url": image_data_url}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def self_portrait_set_animation(self, style) -> dict:
        style = str(style or "").strip().lower()
        if style not in self._PORTRAIT_ANIMATIONS:
            return {"ok": False, "message": "unknown ambient style"}
        try:
            mem = MemoryStore(self.cfg.memory_db_path)
            try:
                mem.set_meta("portrait_animation", style)
            finally:
                mem.close()
            return {"ok": True, "animation": style}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def self_portrait_upload(self) -> dict:
        if self._window is None:
            return {"ok": False, "message": "window not ready"}
        try:
            import webview
            paths = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=False,
                                                      file_types=("Images (*.png;*.jpg;*.jpeg;*.gif;*.webp)",))
            if not paths:
                return {"ok": False, "message": ""}  # user cancelled
            path = paths[0] if isinstance(paths, (list, tuple)) else paths
            ext = os.path.splitext(str(path))[1].lower()
            if ext not in self._PORTRAIT_IMAGE_TYPES:
                return {"ok": False, "message": "Pick a PNG, JPG, GIF, or WEBP image."}
            with open(path, "rb") as handle:
                data = handle.read()
            if len(data) > self._PORTRAIT_MAX_BYTES:
                return {"ok": False, "message": "That image is too large (max ~8MB)."}
            portrait_dir = self._portrait_dir()
            os.makedirs(portrait_dir, exist_ok=True)
            # Remove any previous portrait file under a different extension so
            # they don't silently pile up in data/.
            for known_ext in self._PORTRAIT_IMAGE_TYPES:
                stale = os.path.join(portrait_dir, "portrait" + known_ext)
                if known_ext != ext and os.path.isfile(stale):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
            saved_path = os.path.join(portrait_dir, "portrait" + ext)
            with open(saved_path, "wb") as handle:
                handle.write(data)
            mem = MemoryStore(self.cfg.memory_db_path)
            try:
                mem.set_meta("portrait_image_path", saved_path)
            finally:
                mem.close()
            media_type = self._PORTRAIT_IMAGE_TYPES[ext]
            image_data_url = f"data:{media_type};base64," + base64.b64encode(data).decode("ascii")
            return {"ok": True, "image_data_url": image_data_url}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def self_today_focus(self, force=False) -> dict:
        """Which active Leaves are most worth today's attention, per the model
        (cached once/day; pass force=True to recompute now)."""
        from livingpc.goals import GoalStore
        from livingpc.today_focus import get_today_focus
        goals = GoalStore(self.cfg.memory_db_path)
        try:
            tree = goals.tree()
        finally:
            goals.close()
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            return get_today_focus(self.cfg, mem, tree, force=bool(force))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()

    def open_agent_window(self, mode, entity_id) -> dict:
        """Open a bounded agent in its own native window; args contain no private text."""
        try:
            mode = str(mode)
            if mode not in {"inference", "goal-agent", "goal-planner", "goal-harvest"}:
                raise ValueError("unsupported agent window mode")
            entity_id = int(entity_id)
            from agent_window import AgentWindowApi
            preflight = AgentWindowApi(mode, entity_id, self.cfg).state()
            if not preflight.get("ok", False):
                return {"ok": False, "message": preflight.get("message") or "Agent unavailable."}
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "agent_window.py")
            executable = sys.executable
            if os.name == "nt" and executable.lower().endswith("python.exe"):
                pythonw = executable[:-10] + "pythonw.exe"
                if os.path.isfile(pythonw):
                    executable = pythonw
            kwargs = {"cwd": os.path.dirname(script)}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.Popen([executable, script, mode, str(entity_id)], **kwargs)
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    # --- database rescue ----------------------------------------------------
    def database_status(self) -> dict:
        from livingpc.db_rescue import database_status
        try:
            return database_status(self.cfg)
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def database_unlock(self) -> dict:
        from livingpc.db_rescue import rescue_databases
        try:
            return rescue_databases(self.cfg)
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    # --- inferences ---------------------------------------------------------
    def state(self) -> dict:
        """Everything the Inferences view renders, in one call."""
        review = InferenceReview(self.cfg.memory_db_path)
        try:
            return {
                "gate": self.gate,
                "backend": getattr(self.cfg, "inference_backend", "?"),
                "stack": review.stack(gate=self.gate),
                "forming": review.forming(gate=self.gate),
                "beliefs": review.confirmed(),
                "inquiries": review.store.open_inquiries(),
            }
        finally:
            review.close()

    def answer(self, action, inference_id, text=None) -> dict:
        review = InferenceReview(self.cfg.memory_db_path)
        try:
            review.answer(action, int(inference_id), text)
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            review.close()

    def inference_inquiry_start(self, prompt=None, inference_id=None) -> dict:
        """Start/resume Address or begin an explicitly directed investigation."""
        from livingpc.inference import InferenceStore
        from livingpc.inference_inquiry import start_inquiry
        inf = InferenceStore(self.cfg.memory_db_path)
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            if inference_id is not None:
                row = inf.get(int(inference_id))
                if row is None:
                    raise ValueError("inference not found")
                actual_prompt = inf._dict(row)["statement"]
                result = start_inquiry(
                    self.cfg, inf, mem, kind="address", prompt=actual_prompt,
                    inference_id=int(inference_id))
            else:
                result = start_inquiry(
                    self.cfg, inf, mem, kind="directed", prompt=str(prompt or ""))
            return {"ok": True, "inquiry": result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()

    def inference_inquiry_get(self, inquiry_id) -> dict:
        from livingpc.inference import InferenceStore
        inf = InferenceStore(self.cfg.memory_db_path)
        try:
            inquiry = inf.inquiry(int(inquiry_id))
            return ({"ok": True, "inquiry": inquiry} if inquiry else
                    {"ok": False, "message": "inquiry not found"})
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            inf.close()

    def inference_inquiry_reply(self, inquiry_id, text) -> dict:
        from livingpc.inference import InferenceStore
        from livingpc.inference_inquiry import reply_to_inquiry
        inf = InferenceStore(self.cfg.memory_db_path)
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            result = reply_to_inquiry(
                self.cfg, inf, mem, int(inquiry_id), str(text or ""))
            return {"ok": True, "inquiry": result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()

    def inference_inquiry_resolve(self, inquiry_id, outcome, statement=None) -> dict:
        from livingpc.inference import InferenceStore
        inf = InferenceStore(self.cfg.memory_db_path)
        try:
            canonical_id = inf.resolve_inquiry(
                int(inquiry_id), str(outcome), str(statement or ""))
            return {"ok": True, "canonical_id": canonical_id}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            inf.close()

    # --- No / Kind-of feedback dialogue --------------------------------------
    def feedback_questions(self, inference_id, action) -> dict:
        """The model's follow-up questions after a No/Kind-of. Blocks; JS awaits."""
        from livingpc.feedback import feedback_questions, get_feedback_model
        from livingpc.inference import InferenceStore
        inf = InferenceStore(self.cfg.memory_db_path)
        try:
            questions = feedback_questions(
                inf, int(inference_id), str(action), get_feedback_model(self.cfg))
            return {"ok": True, "questions": questions}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            inf.close()

    def submit_feedback(self, inference_id, action, text, questions=None) -> dict:
        """Analyze the user's explanation, store the lesson, apply the action."""
        from livingpc.feedback import get_feedback_model, submit_feedback
        from livingpc.inference import InferenceStore
        inf = InferenceStore(self.cfg.memory_db_path)
        try:
            result = submit_feedback(
                inf, int(inference_id), str(action), str(text or ""),
                list(questions or []), get_feedback_model(self.cfg))
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            inf.close()

    def run_inference(self) -> dict:
        """Blocks on pywebview's worker thread; the JS awaits the promise."""
        started = time.monotonic()
        log_diag("inference", f"GUI inference run started "
                 f"backend={self.cfg.inference_backend}")
        review = InferenceReview(self.cfg.memory_db_path)
        try:
            summary = review.run_now(self.cfg)
            log_diag("inference", f"GUI inference run done "
                     f"created={summary.get('created')} "
                     f"elapsed={time.monotonic() - started:.1f}s")
            return summary
        except Exception as error:
            log_diag("inference", f"GUI inference run failed "
                     f"error={type(error).__name__}: {error}\n{traceback.format_exc()}")
            return {"error": f"{type(error).__name__}: {error}"}
        finally:
            review.close()

    # --- journal drop + import ------------------------------------------------
    def _journal_dir(self) -> str:
        import os
        path = getattr(self.cfg, "journal_dir", "data/notion")
        if not os.path.isabs(path):
            from livingpc.config import APP_DIR
            path = os.path.join(APP_DIR, path)
        os.makedirs(path, exist_ok=True)
        return path

    def ingest_file(self, name, content, default_year, encoding="text") -> dict:
        """Save a dropped file into the journal folder, adding front matter
        (with the chosen default_year) when the file has none. .docx arrives as
        base64 and is converted to text; saved as .md either way. Returns a
        parse preview so the UI can show what the importer would see."""
        import base64
        import os
        import re as _re
        from datetime import date
        from livingpc.journal_import import parse_entries, parse_front_matter
        name = os.path.basename(str(name or "dropped.md"))
        stem, ext = os.path.splitext(name)
        ext = ext.lower()
        if ext == ".doc":
            return {"ok": False, "message": "legacy .doc isn't supported — open it "
                    "in Word and save as .docx, then drop again"}
        if ext not in (".md", ".txt", ".docx"):
            return {"ok": False, "message": f"only .md / .txt / .docx ({ext} not supported)"}
        if ext == ".docx":
            from livingpc.docx_text import docx_to_text
            try:
                content = docx_to_text(base64.b64decode(str(content or "")))
            except Exception as error:
                return {"ok": False, "message": f"could not read .docx: {error}"}
            ext = ".md"   # converted; store as markdown
        else:
            content = str(content or "")
        if not content.strip():
            return {"ok": False, "message": "file is empty"}
        meta, _ = parse_front_matter(content)
        if not meta:
            content = ("---\n"
                       f"title: {stem}\n"
                       f"exported_at: {date.today().isoformat()}\n"
                       f"default_year: {int(default_year)}\n"
                       "---\n" + content)
        safe = _re.sub(r"[^A-Za-z0-9._ -]", "_", stem) or "dropped"
        target = os.path.join(self._journal_dir(), safe + ext.lower())
        n = 1
        while os.path.exists(target):
            n += 1
            target = os.path.join(self._journal_dir(), f"{safe}-{n}{ext.lower()}")
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        meta2, body = parse_front_matter(content)
        entries = parse_entries(body, int(meta2.get("default_year") or default_year))
        dated = [e["date"] for e in entries if e["date"]]
        return {"ok": True, "file": os.path.basename(target),
                "entries": len(entries), "dated": len(dated),
                "from": min(dated) if dated else None,
                "to": max(dated) if dated else None}

    def journal_files(self) -> dict:
        """Everything staged in the journal folder + the import watermark +
        per-file committed status (imported / changed / new)."""
        import os
        from livingpc.journal_import import (WATERMARK_KEY, imported_file_status,
                                             load_journals, validate_dates)
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            watermark = mem.get_meta(WATERMARK_KEY)
            statuses = imported_file_status(mem, self._journal_dir())
        finally:
            mem.close()
        files = []
        earliest = None
        for j in load_journals(self._journal_dir()):
            dated = [e["date"] for e in j["entries"] if e["date"]]
            if dated:
                earliest = min(earliest or dated[0], min(dated))
            files.append({"source": j["source"], "file": j.get("file"),
                          "entries": len(j["entries"]),
                          "dated": len(dated),
                          "status": statuses.get(j.get("file"), "new"),
                          "warnings": validate_dates(j),
                          "from": min(dated) if dated else None,
                          "to": max(dated) if dated else None})
        needs_reset = bool(watermark and earliest and earliest[:7] <= watermark)
        return {"files": files, "watermark": watermark, "needs_reset": needs_reset,
                "folder": self._journal_dir()}

    def remove_journal_file(self, filename) -> dict:
        """Delete one staged file (the staged copy only — never touches
        memory or the original document). Also forgets its imported-hash."""
        import json as _json
        import os
        from livingpc.journal_import import IMPORTED_FILES_KEY
        name = os.path.basename(str(filename or ""))
        if not name or name.startswith("."):
            return {"ok": False, "message": "bad filename"}
        path = os.path.join(self._journal_dir(), name)
        if not os.path.isfile(path):
            return {"ok": False, "message": f"not staged: {name}"}
        os.remove(path)
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            record = _json.loads(mem.get_meta(IMPORTED_FILES_KEY) or "{}")
            if name in record:
                del record[name]
                mem.set_meta(IMPORTED_FILES_KEY, _json.dumps(record))
        except ValueError:
            pass
        finally:
            mem.close()
        return {"ok": True, "removed": name}

    def run_journal_import(self, dry_run=False, reset=False, deep=False) -> dict:
        """Run the chronological import (blocks; JS shows a spinner). Fires a
        desktop toast when done — imports can take minutes, so you can tab away."""
        if dry_run:
            return {
                "ok": False,
                "message": "Journal dry-run preview was removed. Import when you are ready.",
            }
        from livingpc.journal_import import get_journal_model, import_journals
        from livingpc.notify import import_summary, notify
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            stats = import_journals(
                self.cfg, mem, model=get_journal_model(self.cfg),
                journal_dir=self._journal_dir(),
                dry_run=False, reset=bool(reset), deep=bool(deep))
            notify(*import_summary(stats), cfg=self.cfg)
            if getattr(self.cfg, "clarify_scan_after_import", True):
                self._scan_clarifications_quietly(mem)
            return {"ok": True, **stats}
        except Exception as error:
            notify("Journal import failed", f"{type(error).__name__}: {error}",
                   cfg=self.cfg)
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()

    # --- memory hygiene ---------------------------------------------------------
    def consolidate_now(self) -> dict:
        """Run the dedupe/prune hygiene pass on demand (Import tab) — the same
        pass the nightly daemon runs, just triggered manually right after a
        big journal drop so near-duplicate memories merge before Clarify scans
        them, instead of waiting for the next nightly cycle."""
        from livingpc.consolidate import consolidate
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            result = consolidate(
                mem, similarity=float(getattr(self.cfg, "consolidate_value_similarity", 0.85)),
                rejection_retention_days=int(
                    getattr(self.cfg, "consolidate_rejection_retention_days", 90)),
                evidence_retention_days=int(
                    getattr(self.cfg, "consolidate_evidence_retention_days", 180)))
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()

    # --- clarifying questions -------------------------------------------------
    def _scan_clarifications_quietly(self, mem) -> None:
        """Best-effort post-import scan — never lets a clarify hiccup (e.g. no
        API key) fail the import itself."""
        from livingpc.clarify import ClarifyStore, get_clarify_model, scan
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            scan(mem, store, get_clarify_model(self.cfg),
                limit=int(getattr(self.cfg, "clarify_scan_limit", 20)),
                min_plausible_age=float(getattr(self.cfg, "clarify_min_plausible_age", 2.0)))
        except Exception as error:
            log_diag("clarify", f"post-import scan failed: "
                     f"{type(error).__name__}: {error}")
        finally:
            store.close()

    def clarify_state(self) -> dict:
        """Open questions + a short recent-resolved history for the Clarify tab.
        Each open item is enriched with the memory's current (live, decrypted)
        value, since it may have changed since the question was queued."""
        from livingpc.clarify import (BIRTH_DATE_META_KEY, GRADE_YEAR_MAP_META_KEY,
                                      ClarifyStore, _load_grade_year_map)
        mem = MemoryStore(self.cfg.memory_db_path)
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            open_items = store.open_items()
            for item in open_items:
                row = mem.get(item["memory_id"]) if item["memory_id"] is not None else None
                item["value"] = crypto.dec(row["value"]) if row is not None else ""
            return {"open": open_items, "resolved": store.resolved(limit=10),
                    "stats": store.stats(),
                    "birth_date": mem.get_meta(BIRTH_DATE_META_KEY),
                    "grade_chart_count": len(_load_grade_year_map(mem))}
        finally:
            mem.close()
            store.close()

    def clarify_set_birth_date(self, date_str) -> dict:
        """Set the birth date used for age-plausibility checks (Clarify tab).
        Pass an empty string to clear it (disables that check). Also
        rechecks every currently-open date-only clarification against the
        new birth date, since some may now resolve immediately rather than
        sitting stale in the tab until the next unrelated scan()."""
        import re
        from livingpc.clarify import BIRTH_DATE_META_KEY, ClarifyStore, recheck_open_date_clarifications
        date_str = str(date_str or "").strip()
        if date_str and not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return {"ok": False, "message": "use YYYY-MM-DD"}
        mem = MemoryStore(self.cfg.memory_db_path)
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            mem.set_meta(BIRTH_DATE_META_KEY, date_str)
            resolved = recheck_open_date_clarifications(
                mem, store,
                min_plausible_age=float(getattr(self.cfg, "clarify_min_plausible_age", 2.0)))
            return {"ok": True, "resolved": resolved}
        finally:
            mem.close()
            store.close()

    def clarify_set_grade_chart(self, text) -> dict:
        """Store the user's own grade -> school-year chart (Clarify tab) —
        exact ground truth used instead of the generic age-range guess
        wherever a grade in it comes up. Empty text clears it. Also rechecks
        every currently-open date-only clarification against the new chart,
        so saving a fuller/corrected chart resolves questions that were only
        wrong because the chart wasn't there yet, right away."""
        from livingpc.clarify import ClarifyStore, recheck_open_date_clarifications, set_grade_year_chart
        mem = MemoryStore(self.cfg.memory_db_path)
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            count = set_grade_year_chart(mem, str(text or ""))
            if (text or "").strip() and count == 0:
                return {"ok": False, "grades": 0,
                        "message": "couldn't recognize any grades in that — "
                                   "each line needs a grade name and a "
                                   "YYYY-YYYY year range"}
            resolved = recheck_open_date_clarifications(
                mem, store,
                min_plausible_age=float(getattr(self.cfg, "clarify_min_plausible_age", 2.0)))
            return {"ok": True, "grades": count, "resolved": resolved}
        finally:
            mem.close()
            store.close()

    def clarify_scan(self) -> dict:
        """Scan active memory for hedged/anachronistic/implausible-age values
        not yet asked about. Blocks; JS shows a spinner (small model calls,
        one per new flag found)."""
        from livingpc.clarify import ClarifyStore, get_clarify_model, scan
        mem = MemoryStore(self.cfg.memory_db_path)
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            created = scan(mem, store, get_clarify_model(self.cfg),
                           limit=int(getattr(self.cfg, "clarify_scan_limit", 20)),
                           min_plausible_age=float(
                               getattr(self.cfg, "clarify_min_plausible_age", 2.0)))
            return {"ok": True, "created": created}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            store.close()

    def clarify_answer(self, clarification_id, text) -> dict:
        """The user answered a clarifying question — resolve + supersede."""
        from livingpc.clarify import ClarifyStore, answer, get_clarify_model
        mem = MemoryStore(self.cfg.memory_db_path)
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            result = answer(mem, store, int(clarification_id), str(text or ""),
                            get_clarify_model(self.cfg))
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            store.close()

    def clarify_dismiss(self, clarification_id) -> dict:
        from livingpc.clarify import ClarifyStore, dismiss
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            dismiss(store, int(clarification_id))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def clarify_dismiss_many(self, ids) -> dict:
        """Bulk dismiss several open clarifications at once (Clarify tab
        multi-select) — best-effort, so one bad id doesn't block the rest."""
        from livingpc.clarify import ClarifyStore, dismiss_many
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            result = dismiss_many(store, [int(i) for i in (ids or [])])
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def clarify_answer_many(self, ids, text) -> dict:
        """Apply the SAME answer text to several selected clarifications at
        once (e.g. a cluster of near-identical hedges from one import)."""
        from livingpc.clarify import ClarifyStore, answer_many, get_clarify_model
        mem = MemoryStore(self.cfg.memory_db_path)
        store = ClarifyStore(self.cfg.memory_db_path)
        try:
            result = answer_many(mem, store, [int(i) for i in (ids or [])],
                                 str(text or ""), get_clarify_model(self.cfg))
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            store.close()

    # --- curiosity --------------------------------------------------------------
    def _sync_curiosity_notion_quietly(self, mem, inf, store, curiosity_id, model) -> None:
        """Best-effort Notion mirror — never lets a sync hiccup (missing
        token, network error) surface to the user or interrupt the curiosity
        flow itself. See livingpc/notion_sync.py."""
        try:
            from livingpc.notion_sync import sync_curiosity_to_notion
            sync_curiosity_to_notion(self.cfg, mem, inf, store, int(curiosity_id), model)
        except Exception as error:
            log_diag("notion", f"quiet sync failed curiosity_id={curiosity_id}: "
                     f"{type(error).__name__}: {error}")

    def curiosity_state(self) -> dict:
        """Everything the Curiosity tab renders: every non-archived curiosity
        with its open items split by kind, plus a short resolved history."""
        from livingpc.curiosity import CuriosityStore
        from livingpc.curiosity_metrics import MetricStore
        from livingpc.inference import InferenceStore
        from livingpc.context_attachment import ContextAttachmentStore
        store = CuriosityStore(self.cfg.memory_db_path)
        metrics = MetricStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        documents = ContextAttachmentStore(self.cfg.memory_db_path)
        try:
            goal_titles = {}
            goal_types = {}
            goal_parents = {}
            if store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_node'"
            ).fetchone():
                for goal in store.conn.execute(
                    "SELECT id,parent_id,node_type,title FROM goal_node"
                ).fetchall():
                    goal_titles[int(goal["id"])] = crypto.dec(goal["title"]) or ""
                    goal_types[int(goal["id"])] = goal["node_type"]
                    goal_parents[int(goal["id"])] = goal["parent_id"]

            def goal_path(goal_id: int) -> list[str]:
                path = []
                current = int(goal_id)
                seen = set()
                while current and current not in seen:
                    seen.add(current)
                    title = goal_titles.get(current)
                    if title:
                        path.append(title)
                    current = int(goal_parents[current]) if goal_parents.get(current) else 0
                return list(reversed(path))

            attached_by_curiosity = {}
            if goal_titles and store.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_curiosity_link'"
            ).fetchone():
                for link in store.conn.execute(
                    "SELECT goal_id,curiosity_id,created_at FROM goal_curiosity_link "
                    "ORDER BY created_at,goal_id,curiosity_id"
                ).fetchall():
                    goal_id = int(link["goal_id"])
                    attached_by_curiosity.setdefault(int(link["curiosity_id"]), []).append({
                        "goal_id": goal_id,
                        "goal_title": goal_titles.get(goal_id, ""),
                        "goal_type": goal_types.get(goal_id, ""),
                        "path": goal_path(goal_id),
                        "created_at": link["created_at"],
                    })
            curiosities = []
            for row in store.list_curiosities():
                if row["status"] == "archived":
                    continue
                open_items = store.open_items(row["id"])
                for item in open_items:
                    item["context_attachments"] = documents.list(
                        "curiosity_item", item["id"])
                # The Recently-resolved panel is collapsible and shows the full
                # Q&A history when expanded, so fetch everything practical.
                resolved_items = store.resolved(row["id"], limit=500)
                for item in resolved_items:
                    item["context_attachments"] = documents.list(
                        "curiosity_item", item["id"])
                # Reading the board must not trigger a model call or create a
                # tracking rubric.  Drafting is an explicit user decision.
                profile = (metrics.get_profile(row["id"])
                           if getattr(self.cfg, "curiosity_metrics_enabled", True) else None)
                snapshot = metrics.latest_snapshot(row["id"]) if profile else None
                preview = None
                if profile and profile.status == "approved" and snapshot:
                    from livingpc.curiosity_metrics import render_dashboard
                    chart_dir = os.path.join(os.path.dirname(self.cfg.memory_db_path),
                                             "notion_charts")
                    path, _ = render_dashboard(
                        profile, snapshot, metrics.history(row["id"], 30), chart_dir)
                    with open(path, "rb") as handle:
                        preview = "data:image/png;base64," + base64.b64encode(
                            handle.read()).decode("ascii")
                curiosities.append({
                    **row,
                    "open_questions": [i for i in open_items if i["kind"] == "question"],
                    "open_suggestions": [i for i in open_items if i["kind"] == "suggestion"],
                    "resolved": resolved_items,
                    "item_counts": store.item_counts(row["id"]),
                    "classification_proposals": store.classification_proposals(row["id"]),
                    "classification_history": store.classification_proposals(row["id"], status=None),
                    "classification_contexts": store.classification_contexts(row["id"]),
                    "syntheses": store.synthesis_history(row["id"], limit=12),
                    "synthesis_due": store.synthesis_due(row["id"]),
                    "person_model_proposals": inf.person_proposals(row["id"]),
                    "person_model_reconciled_synthesis_ids": [
                        item["id"] for item in store.synthesis_history(row["id"], limit=12)
                        if inf.person_reconciliation_run(item["id"])],
                    "metric_profile": asdict(profile) if profile else None,
                    "metric_snapshot": asdict(snapshot) if snapshot else None,
                    "metric_history": ([asdict(item) for item in metrics.history(row["id"], 7)]
                                       if profile else []),
                    "metric_preview": preview,
                    "interaction_preferences": store.interaction_preference_block(row["id"]),
                    "attached_goals": attached_by_curiosity.get(row["id"], []),
                    "context_attachments": documents.list("curiosity", row["id"]),
                })
            archived = store.list_curiosities(status="archived")
            return {
                "curiosities": curiosities,
                "archived": archived,
                "investigation_candidates": store.visible_candidates(limit=2),
                "stats": store.stats(),
                "global_xp": metrics.global_xp(),
                "checkin_hour": getattr(self.cfg, "curiosity_checkin_hour", 21),
                "interval_minutes": getattr(self.cfg, "curiosity_interval_minutes", 720),
            }
        finally:
            documents.close()
            inf.close()
            metrics.close()
            store.close()

    def curiosity_classify(self, curiosity_id) -> dict:
        from livingpc.curiosity import CuriosityStore, classify_curiosity, get_curiosity_model
        from livingpc.inference import InferenceStore
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            result = classify_curiosity(
                self.cfg, mem, inf, store, int(curiosity_id),
                get_curiosity_model(self.cfg, usage_category="manual"))
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()
            inf.close()
            mem.close()

    def curiosity_candidate_suggest(self) -> dict:
        from livingpc.curiosity import (
            CuriosityStore, get_curiosity_model, suggest_investigation_candidates,
        )
        from livingpc.goals import GoalStore
        from livingpc.inference import InferenceStore
        store = CuriosityStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        goals = GoalStore(self.cfg.memory_db_path)
        try:
            candidates = suggest_investigation_candidates(
                store, inf, goals,
                get_curiosity_model(self.cfg, usage_category="manual"))
            return {"ok": True, "candidates": candidates}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            goals.close()
            inf.close()
            store.close()

    def curiosity_candidate_action(self, candidate_id, action, payload=None) -> dict:
        from livingpc.curiosity import (
            CuriosityStore, defer_candidate_until, get_curiosity_model,
            start_investigation_candidate,
        )
        from livingpc.inference import InferenceStore
        store = CuriosityStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            action = str(action or "")
            if action == "start":
                model = get_curiosity_model(self.cfg, usage_category="manual")
                result = start_investigation_candidate(
                    mem, inf, store, int(candidate_id), model,
                    sensitive_permission=bool(
                        isinstance(payload, dict) and payload.get("sensitive_permission")))
                self._sync_curiosity_notion_quietly(
                    mem, inf, store, result["curiosity_id"], model)
                return {"ok": True, **result}
            candidate = store.decide_candidate(
                int(candidate_id), action,
                payload=dict(payload) if isinstance(payload, dict) else None,
                note=f"User chose {action} for this suggested Investigation",
                defer_until=defer_candidate_until(14) if action == "defer" else None)
            return {"ok": True, "candidate": candidate}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()
            store.close()

    def curiosity_synthesize(self, curiosity_id) -> dict:
        from livingpc.curiosity import (
            CuriosityStore, get_curiosity_model, synthesize_curiosity,
        )
        from livingpc.inference import InferenceStore
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            synthesis = synthesize_curiosity(
                mem, inf, store, int(curiosity_id),
                get_curiosity_model(self.cfg, usage_category="manual"))
            return {"ok": True, "synthesis": synthesis}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()
            inf.close()
            mem.close()

    def curiosity_synthesis_decide(self, synthesis_id, action, payload=None,
                                   note="") -> dict:
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            synthesis = store.decide_synthesis(
                int(synthesis_id), str(action or ""),
                payload=dict(payload) if isinstance(payload, dict) else None,
                note=str(note or ""))
            return {"ok": True, "synthesis": synthesis}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def curiosity_person_reconcile(self, synthesis_id) -> dict:
        from livingpc.curiosity import (
            CuriosityStore, get_curiosity_model, reconcile_synthesis,
        )
        from livingpc.inference import InferenceStore
        store = CuriosityStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        try:
            proposals = reconcile_synthesis(
                inf, store, int(synthesis_id),
                get_curiosity_model(self.cfg, usage_category="manual"))
            return {"ok": True, "proposals": proposals}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            inf.close()
            store.close()

    def curiosity_person_proposal(self, proposal_id, action, payload=None,
                                  note="") -> dict:
        from livingpc.inference import InferenceStore
        inf = InferenceStore(self.cfg.memory_db_path)
        try:
            proposal = inf.decide_person_proposal(
                int(proposal_id), str(action or ""),
                payload=dict(payload) if isinstance(payload, dict) else None,
                note=str(note or ""))
            return {"ok": True, "proposal": proposal}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            inf.close()

    def curiosity_classification_proposal(self, proposal_id, action) -> dict:
        from livingpc.curiosity import decide_classification_proposal
        try:
            return decide_classification_proposal(
                self.cfg, int(proposal_id), str(action))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def curiosity_classification_refine(self, proposal_id, note) -> dict:
        from livingpc.curiosity import refine_classification_proposal
        return refine_classification_proposal(
            self.cfg, int(proposal_id), str(note or ""))

    def curiosity_metric_approve(self, curiosity_id, dimensions=None,
                                 state_metrics=None) -> dict:
        from livingpc.curiosity_metrics import MetricStore
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            profile = metrics.approve_profile(
                int(curiosity_id), dimensions=dimensions, state_metrics=state_metrics)
            return {"ok": True, "profile": asdict(profile)}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            metrics.close()

    def curiosity_metric_draft(self, curiosity_id) -> dict:
        """Explicitly draft one local reviewable mastery profile.

        The bounded Q&A context is sent only because the user asked to create
        tracking for this Investigation; simply opening the board never does.
        """
        from livingpc.curiosity import CuriosityStore
        from livingpc.curiosity_metrics import MetricStore
        store = CuriosityStore(self.cfg.memory_db_path)
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            curiosity = store.get_curiosity(int(curiosity_id))
            if not curiosity:
                raise ValueError("investigation not found")
            answered = [item for item in store.items_for_curiosity(int(curiosity_id))
                        if item.get("status") == "answered"][-8:]
            context = "\n".join(
                f"Q: {item.get('text', '')}\nA: {item.get('answer', '')}" for item in answered)
            profile = metrics.ensure_profile({
                **curiosity, "allow_model_draft": True, "metric_context": context,
            })
            if not profile:
                raise ValueError("could not draft a mastery profile")
            return {"ok": True, "profile": asdict(profile)}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            metrics.close()
            store.close()

    def curiosity_metric_checkin(self, curiosity_id, state=None, growth=None,
                                 note="") -> dict:
        """Store raw notes locally and return only calculated, display-safe data."""
        from livingpc.curiosity_metrics import MetricStore
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            metrics.record_checkin(
                int(curiosity_id), dict(state or {}), dict(growth or {}), str(note or ""))
            snapshot = metrics.build_snapshot(
                int(curiosity_id), datetime.now().astimezone().date().isoformat())
            return {"ok": True, "snapshot": asdict(snapshot)}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            metrics.close()

    def curiosity_metric_history(self, curiosity_id, limit=30) -> dict:
        from livingpc.curiosity_metrics import MetricStore
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            rows = metrics.history(int(curiosity_id), min(90, max(1, int(limit))))
            return {"ok": True, "history": [asdict(row) for row in rows]}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            metrics.close()

    def curiosity_metric_publish(self, curiosity_id) -> dict:
        """Explicitly approve one calibrated curiosity for its Notion dashboard."""
        from livingpc.curiosity import CuriosityStore, get_curiosity_model
        from livingpc.curiosity_metrics import MetricStore
        from livingpc.inference import InferenceStore
        from livingpc.notion_sync import sync_curiosity_to_notion
        metrics = MetricStore(self.cfg.memory_db_path)
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            profile = metrics.approve_publication(int(curiosity_id))
            result = sync_curiosity_to_notion(
                self.cfg, mem, inf, store, int(curiosity_id),
                get_curiosity_model(self.cfg))
            if not result.get("ok"):
                return {"ok": False, "message": result.get("message", "Notion publish failed"),
                        "profile": asdict(profile)}
            return {"ok": True, "profile": asdict(metrics.get_profile(int(curiosity_id))),
                    **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            metrics.close(); mem.close(); inf.close(); store.close()

    def curiosity_metric_unpublish(self, curiosity_id) -> dict:
        """Stop future metric dashboard updates without deleting Notion content."""
        from livingpc.curiosity_metrics import MetricStore
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            return {"ok": True, "profile": asdict(
                metrics.revoke_publication(int(curiosity_id)))}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            metrics.close()

    def curiosity_set(self, directive, label=None, make_greatest=False) -> dict:
        """Create a new curiosity from a directive and immediately generate
        its first round of items. Blocks; JS shows a spinner."""
        from livingpc.curiosity import CuriosityStore, get_curiosity_model, set_curiosity
        from livingpc.inference import InferenceStore
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            limit = (int(getattr(self.cfg, "curiosity_scan_limit_greatest", 5))
                    if make_greatest
                    else int(getattr(self.cfg, "curiosity_scan_limit_background", 2)))
            model = get_curiosity_model(self.cfg)
            result = set_curiosity(
                mem, inf, store, str(directive or ""), model,
                label=(str(label).strip() if label else None),
                make_greatest=bool(make_greatest), limit=limit)
            self._sync_curiosity_notion_quietly(mem, inf, store, result["curiosity_id"], model)
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()
            store.close()

    def curiosity_journal_start(self, journal_text, label=None,
                                make_greatest=False) -> dict:
        """Create an Investigation from a large current-state journal dump."""
        from livingpc.curiosity import (
            CuriosityStore, get_curiosity_model, set_curiosity_from_journal)
        from livingpc.inference import InferenceStore
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            limit = (int(getattr(self.cfg, "curiosity_scan_limit_greatest", 5))
                    if make_greatest
                    else int(getattr(self.cfg, "curiosity_scan_limit_background", 2)))
            model = get_curiosity_model(self.cfg)
            result = set_curiosity_from_journal(
                mem, inf, store, str(journal_text or ""), model,
                label=(str(label).strip() if label else None),
                make_greatest=bool(make_greatest), limit=limit)
            self._sync_curiosity_notion_quietly(mem, inf, store, result["curiosity_id"], model)
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()
            store.close()

    def curiosity_rename(self, curiosity_id, label) -> dict:
        from livingpc.curiosity import CuriosityStore, get_curiosity_model
        from livingpc.inference import InferenceStore
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            curiosity = store.rename(int(curiosity_id), str(label or ""))
            self._sync_curiosity_notion_quietly(
                mem, inf, store, int(curiosity_id), get_curiosity_model(self.cfg))
            return {"ok": True, "curiosity": curiosity}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()
            store.close()

    def curiosity_set_greatest(self, curiosity_id, on=True) -> dict:
        from livingpc.curiosity import CuriosityStore, set_greatest
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            set_greatest(store, int(curiosity_id), bool(on))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def curiosity_pause(self, curiosity_id) -> dict:
        from livingpc.curiosity import CuriosityStore, pause_curiosity
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            pause_curiosity(store, int(curiosity_id))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def curiosity_archive(self, curiosity_id) -> dict:
        from livingpc.curiosity import CuriosityStore, archive_curiosity
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            archive_curiosity(store, int(curiosity_id))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def curiosity_reactivate(self, curiosity_id) -> dict:
        from livingpc.curiosity import CuriosityStore, reactivate_curiosity
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            reactivate_curiosity(store, int(curiosity_id))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def curiosity_generate_more(self, curiosity_id=None) -> dict:
        """Manual 'Generate more' — one round for a single curiosity if an id
        is given, otherwise one round for every active curiosity."""
        from livingpc.curiosity import (CuriosityStore, generate_items, get_curiosity_model,
                                        run_all_active)
        from livingpc.inference import InferenceStore
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        model = get_curiosity_model(self.cfg)
        q_floor = float(getattr(self.cfg, "curiosity_question_min_confidence", 0.70))
        s_floor = float(getattr(self.cfg, "curiosity_suggestion_min_confidence", 0.80))
        max_open = int(getattr(self.cfg, "curiosity_max_open_per_curiosity", 6))
        try:
            if curiosity_id is not None:
                row = store.get_curiosity(int(curiosity_id))
                limit = (int(getattr(self.cfg, "curiosity_scan_limit_greatest", 5))
                        if row and row["is_greatest"]
                        else int(getattr(self.cfg, "curiosity_scan_limit_background", 2)))
                created = generate_items(
                    mem, inf, store, int(curiosity_id), model, limit=limit,
                    question_min_confidence=q_floor, suggestion_min_confidence=s_floor,
                    max_open=max_open)
                self._sync_curiosity_notion_quietly(mem, inf, store, int(curiosity_id), model)
            else:
                created = run_all_active(
                    mem, inf, store, model,
                    greatest_limit=int(getattr(self.cfg, "curiosity_scan_limit_greatest", 5)),
                    background_limit=int(getattr(self.cfg, "curiosity_scan_limit_background", 2)),
                    question_min_confidence=q_floor, suggestion_min_confidence=s_floor,
                    max_open=max_open)
                for row in store.list_curiosities(status="active"):
                    self._sync_curiosity_notion_quietly(mem, inf, store, row["id"], model)
            return {"ok": True, "created": created}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()
            store.close()

    def curiosity_answer(self, item_id, text, rating=None, answer_confidence=None,
                         question_fit=None) -> dict:
        from livingpc.curiosity import CuriosityStore, answer_item, get_curiosity_model
        from livingpc.inference import InferenceStore
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            model = get_curiosity_model(self.cfg)
            result = answer_item(mem, store, int(item_id), str(text or ""), model,
                                 rating=None if rating is None else float(rating))
            store.record_interaction_feedback(
                int(item_id), answer_confidence=answer_confidence, question_fit=question_fit)
            if (result.get("metric_event_type") == "assessment" and
                    result.get("metric_dimension_slug") and result.get("rating") is not None):
                from livingpc.curiosity_metrics import MetricStore
                metrics = MetricStore(self.cfg.memory_db_path)
                try:
                    metrics.record_event(
                        result["curiosity_id"], "assessment",
                        f"curiosity_item:{int(item_id)}:answered",
                        dimension_slug=result["metric_dimension_slug"],
                        observed_score=max(0.0, min(10.0, result["rating"])) * 10.0,
                        confidence=0.8)
                    profile = metrics.get_profile(result["curiosity_id"])
                    if profile and profile.status == "approved":
                        metrics.build_snapshot(
                            result["curiosity_id"], datetime.now().astimezone().date().isoformat())
                finally:
                    metrics.close()
            self._sync_curiosity_notion_quietly(mem, inf, store, result["curiosity_id"], model)
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()
            store.close()

    def curiosity_dismiss(self, item_id) -> dict:
        from livingpc.curiosity import CuriosityStore, dismiss_item
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            dismiss_item(store, int(item_id))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def curiosity_respond_suggestion(self, item_id, action, outcome_rating=None) -> dict:
        from livingpc.curiosity import CuriosityStore, get_curiosity_model, respond_suggestion
        from livingpc.inference import InferenceStore
        mem = MemoryStore(self.cfg.memory_db_path)
        inf = InferenceStore(self.cfg.memory_db_path)
        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            item = store.get_item(int(item_id))
            curiosity_id = item["curiosity_id"] if item is not None else None
            respond_suggestion(store, int(item_id), str(action or ""))
            if curiosity_id is not None and str(action or "") == "tried":
                from livingpc.curiosity_metrics import MetricStore
                metrics = MetricStore(self.cfg.memory_db_path)
                try:
                    metrics.record_event(
                        curiosity_id, "practice",
                        f"curiosity_item:{int(item_id)}:tried",
                        dimension_slug=(item["metric_dimension_slug"]
                                        if outcome_rating is not None else None),
                        observed_score=(max(0.0, min(10.0, float(outcome_rating))) * 10.0
                                        if outcome_rating is not None else None),
                        confidence=0.6)
                    profile = metrics.get_profile(curiosity_id)
                    if profile and profile.status == "approved":
                        metrics.build_snapshot(
                            curiosity_id, datetime.now().astimezone().date().isoformat())
                finally:
                    metrics.close()
            if curiosity_id is not None:
                model = get_curiosity_model(self.cfg)
                self._sync_curiosity_notion_quietly(mem, inf, store, curiosity_id, model)
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()
            inf.close()
            store.close()

    # --- goals / Actualized Self --------------------------------------------
    def goal_state(self) -> dict:
        from livingpc.curiosity import CuriosityStore
        from livingpc.goal_ai import (
            GoalAgentStore, goal_relevance_view, relevance_due_nodes,
        )
        from livingpc.goals import GoalStore
        goals = GoalStore(self.cfg.memory_db_path)
        agents = GoalAgentStore(self.cfg.memory_db_path, ensure=False)
        curiosities = CuriosityStore(self.cfg.memory_db_path)
        try:
            tree = goals.tree()
            stale_days = int(getattr(self.cfg, "goal_relevance_stale_days", 30))

            def enrich(node):
                node["relevance"] = goal_relevance_view(
                    goals, agents, node["id"], stale_days=stale_days)
                for child in node.get("children", []):
                    enrich(child)

            enrich(tree)
            return {"ok": True, "tree": tree,
                    "curiosities": curiosities.list_curiosities(),
                    "relevance_due": relevance_due_nodes(
                        goals, agents, stale_days=stale_days)}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            curiosities.close()
            agents.close()
            goals.close()

    def goal_relevance_review(self, goal_id) -> dict:
        from livingpc.goal_ai import review_goal_relevance
        try:
            return review_goal_relevance(self.cfg, int(goal_id))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def reflection_cadence_state(self) -> dict:
        from livingpc.reflection_cadence import ReflectionCadenceStore
        store = ReflectionCadenceStore(self.cfg.memory_db_path)
        try:
            return {"ok": True, **store.snapshot()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def reflection_cadence_feedback(self, event_id, action, usefulness=None,
                                    burden=None) -> dict:
        from livingpc.reflection_cadence import ReflectionCadenceStore
        store = ReflectionCadenceStore(self.cfg.memory_db_path)
        try:
            return store.feedback(
                int(event_id), str(action or "acted"), usefulness=usefulness,
                burden=burden,
                snooze_base_days=int(getattr(self.cfg, "reflection_snooze_base_days", 3)),
                ignore_suppress_days=int(getattr(
                    self.cfg, "reflection_ignore_suppress_days", 30)))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_gardening_proposal(self, proposal_id, action, payload=None,
                                rationale="") -> dict:
        from livingpc.goal_ai import decide_gardening_proposal
        try:
            return decide_gardening_proposal(
                self.cfg, int(proposal_id), str(action or ""),
                payload=dict(payload) if isinstance(payload, dict) else None,
                rationale=str(rationale or ""))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def goal_experiment_outcome(self, goal_id, payload=None) -> dict:
        from livingpc.goals import record_experiment_outcome
        try:
            if not isinstance(payload, dict):
                raise ValueError("outcome details are required")
            return record_experiment_outcome(self.cfg, int(goal_id), dict(payload))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def goal_origin_backfill(self) -> dict:
        from livingpc.curiosity import CuriosityStore
        from livingpc.goals import GoalStore
        goals = GoalStore(self.cfg.memory_db_path)
        curiosities = CuriosityStore(self.cfg.memory_db_path)
        try:
            count = goals.backfill_missing_origins()
            return {"ok": True, "backfilled": count, "tree": goals.tree(),
                    "curiosities": curiosities.list_curiosities()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            curiosities.close()
            goals.close()

    def goal_ai_state(self, goal_id=None) -> dict:
        from livingpc.goal_ai import GoalAgentStore, relevance_due_nodes
        from livingpc.goals import GoalStore
        from livingpc.inference_scheduler import LAST_GOAL_AI_KEY
        from livingpc.storage import EventLog
        store = GoalAgentStore(self.cfg.memory_db_path, ensure=False)
        goals = GoalStore(self.cfg.memory_db_path)
        try:
            now = datetime.now().astimezone()
            hour = int(getattr(self.cfg, "inference_nightly_hour", 20))
            next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            events = EventLog(self.cfg.db_path)
            try:
                last_run = events.get_meta(LAST_GOAL_AI_KEY)
            finally:
                events.close()
            overview = store.overview(1440.0)
            relevance = relevance_due_nodes(
                goals, store,
                stale_days=int(getattr(self.cfg, "goal_relevance_stale_days", 30)))
            overview["relevance_due"] = len(relevance)
            overview.setdefault("queues", {})["relevance"] = relevance
            overview["schedule"] = {
                "mode": "daily_dirty", "hour": hour,
                "last_run_at": last_run, "next_run_at": next_run.isoformat(),
            }
            from livingpc.llm_usage import daily_summary
            overview["usage"] = daily_summary()
            result = {"ok": True, "overview": overview}
            if goal_id is not None:
                result["agent"] = store.node_view(int(goal_id))
            return result
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            goals.close()
            store.close()

    def goal_ai_review(self, goal_id, subtree=False) -> dict:
        from livingpc.goal_ai import run_goal_agent, run_goal_subtree
        try:
            result = (run_goal_subtree(self.cfg, int(goal_id)) if subtree else
                      run_goal_agent(self.cfg, int(goal_id), manual=True))
            return {"ok": True, **result}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def goal_ai_chat(self, goal_id, text) -> dict:
        from livingpc.goal_ai import chat_with_goal_agent
        try:
            return {"ok": True, **chat_with_goal_agent(
                self.cfg, int(goal_id), str(text or ""))}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def goal_ai_question(self, question_id, action, answer="") -> dict:
        from livingpc.goal_ai import GoalAgentStore, summarize_goal_answer
        store = GoalAgentStore(self.cfg.memory_db_path)
        try:
            if action == "answer":
                row = store.conn.execute(
                    "SELECT node_id FROM goal_agent_question WHERE id=? AND status='open'",
                    (int(question_id),)).fetchone()
                if not row:
                    raise ValueError("open GoalAI question not found")
                exact = str(answer or "")
                summary = summarize_goal_answer(self.cfg, int(row["node_id"]), exact)
                node_id = store.answer_question(int(question_id), exact, summary)
            elif action == "dismiss":
                node_id = store.dismiss_question(int(question_id))
            elif action == "reopen":
                node_id = store.reopen_question(int(question_id))
            else:
                raise ValueError("unknown question action")
            return {"ok": True, "agent": store.node_view(node_id)}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_ai_proposal(self, proposal_id, action, payload=None, rationale="") -> dict:
        from livingpc.goal_ai import decide_proposal
        try:
            return decide_proposal(
                self.cfg, int(proposal_id), str(action),
                payload=None if payload is None else dict(payload),
                rationale=str(rationale or ""))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def goal_ai_memory(self, candidate_id, action) -> dict:
        from livingpc.goal_ai import promote_memory_candidate
        try:
            return promote_memory_candidate(self.cfg, int(candidate_id), str(action))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def goal_ai_harvest_start(self, goal_id) -> dict:
        from livingpc.goal_ai import start_goal_harvest
        try:
            return {"ok": True, "harvest": start_goal_harvest(self.cfg, int(goal_id))}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def goal_ai_description(self, goal_id) -> dict:
        from livingpc.goal_ai import generate_goal_description
        try:
            return {"ok": True,
                    "description": generate_goal_description(self.cfg, int(goal_id))}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def goal_create(self, node_type, title, parent_id=None, description="", notes="",
                    priority="normal", due_date=None) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            goal_id = store.create(
                str(node_type), str(title or ""),
                parent_id=None if parent_id is None else int(parent_id),
                description=str(description or ""), notes=str(notes or ""),
                priority=str(priority or "normal"), due_date=due_date)
            return {"ok": True, "goal_id": goal_id, "tree": store.tree()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_update(self, goal_id, changes=None) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            return {"ok": True, "goal": store.update(int(goal_id), **dict(changes or {})),
                    "tree": store.tree()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_move(self, goal_id, parent_id, position=None) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            store.move(int(goal_id), int(parent_id),
                       None if position is None else int(position))
            return {"ok": True, "tree": store.tree()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_link_curiosity(self, goal_id, curiosity_id, linked=True) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            if linked:
                store.link_curiosity(int(goal_id), int(curiosity_id))
            else:
                store.unlink_curiosity(int(goal_id), int(curiosity_id))
            return {"ok": True, "tree": store.tree()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_add_evidence(self, goal_id, source_kind, source_id=None, label="") -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            evidence_id = store.add_evidence(
                int(goal_id), str(source_kind), source_id, str(label or ""))
            return {"ok": True, "evidence_id": evidence_id, "tree": store.tree()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_mastery_enable(self, goal_id, dimensions=None) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            profile = store.enable_mastery(int(goal_id), list(dimensions or []))
            return {"ok": True, "mastery": profile, "tree": store.tree()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_mastery_record(self, goal_id, dimension_slug, score,
                            confidence=0.8, source_kind="manual", source_id=None) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            store.record_mastery(int(goal_id), str(dimension_slug), float(score),
                                 float(confidence), str(source_kind), source_id)
            return {"ok": True, "mastery": store.mastery(int(goal_id)),
                    "tree": store.tree()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_plan_start(self, suggestion_item_id, target_parent_id=None) -> dict:
        from livingpc.goals import GoalStore, get_goal_planner, start_planning
        store = GoalStore(self.cfg.memory_db_path)
        try:
            session = start_planning(
                store, get_goal_planner(self.cfg), int(suggestion_item_id),
                None if target_parent_id is None else int(target_parent_id))
            return {"ok": True, "session": session}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_plan_reply(self, session_id, answer) -> dict:
        from livingpc.goals import GoalStore, continue_planning, get_goal_planner
        store = GoalStore(self.cfg.memory_db_path)
        try:
            session = continue_planning(
                store, get_goal_planner(self.cfg), int(session_id), str(answer or ""))
            return {"ok": True, "session": session}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_plan_summarize(self, session_id) -> dict:
        from livingpc.goals import GoalStore, get_goal_planner, summarize_plan
        store = GoalStore(self.cfg.memory_db_path)
        try:
            return {"ok": True, "session": summarize_plan(
                store, get_goal_planner(self.cfg), int(session_id))}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_plan_update_draft(self, session_id, draft) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            session = store.plan_session(int(session_id))
            store.set_plan_draft(int(session_id), dict(draft or {}),
                                 summary=session["summary"], ready=True)
            return {"ok": True, "session": store.plan_session(int(session_id))}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_plan_commit(self, session_id) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            return {"ok": True, **store.commit_plan(int(session_id)), "tree": store.tree()}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_plan_abandon(self, session_id) -> dict:
        from livingpc.goals import GoalStore
        store = GoalStore(self.cfg.memory_db_path)
        try:
            store.abandon_plan(int(session_id))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    def goal_export_notion(self, goal_id) -> dict:
        """Explicit one-shot subtree export; goals are never background-synced."""
        from livingpc.goals import GoalStore
        from livingpc.notion_sync import export_goal_to_notion
        store = GoalStore(self.cfg.memory_db_path)
        try:
            return export_goal_to_notion(self.cfg, store, int(goal_id))
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            store.close()

    # --- core profile / Soul Calibration ----------------------------------
    def core_profile_state(self) -> dict:
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            return {"ok": True, "facts": mem.core_profile_facts(limit=80),
                    "block": mem.core_profile_block(max_facts=50)}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()

    def core_profile_save(self, facts=None) -> dict:
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            saved = 0
            deleted = 0
            for fact in list(facts or []):
                if not isinstance(fact, dict):
                    continue
                section = str(fact.get("section") or "Core")
                attribute = str(fact.get("attribute") or "note")
                value = str(fact.get("value") or "").strip()
                if not value and bool(fact.get("delete")):
                    deleted += mem.retire_core_profile_fact_key(
                        section, attribute, commit=False)
                    continue
                if not value:
                    continue
                mem.upsert_core_profile_fact(
                    section,
                    attribute,
                    value,
                    priority=int(fact.get("priority") or 50),
                    source_kind=str(fact.get("source_kind") or "soul_calibration"),
                    source_id=None if fact.get("source_id") is None
                    else str(fact.get("source_id")),
                    commit=False,
                )
                saved += 1
            mem.conn.commit()
            dirtied = 0
            if saved or deleted:
                try:
                    has_goals = mem.conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='goal_node'").fetchone()
                    if has_goals:
                        from livingpc.goal_ai import GoalAgentStore
                        agents = GoalAgentStore(self.cfg.memory_db_path)
                        try:
                            rows = agents.conn.execute(
                                "SELECT id FROM goal_node WHERE status='active'"
                            ).fetchall()
                            for row in rows:
                                agents.mark_dirty(
                                    int(row["id"]), ancestors=False,
                                    reason="core profile changed")
                                dirtied += 1
                        finally:
                            agents.close()
                except Exception as error:
                    log_diag("core-profile",
                             f"goalai dirty failed error={type(error).__name__}")
            return {"ok": True, "saved": saved,
                    "deleted": deleted,
                    "goal_ai_dirtied": dirtied,
                    "facts": mem.core_profile_facts(limit=80),
                    "block": mem.core_profile_block(max_facts=50)}
        except Exception as error:
            mem.conn.rollback()
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}
        finally:
            mem.close()

    # --- memory -------------------------------------------------------------
    def memory(self, show_superseded=False) -> list[dict]:
        """Facts grouped by category: [{category, facts: [...]}, ...]."""
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            if show_superseded:
                rows = mem.conn.execute(
                    "SELECT * FROM memory ORDER BY category, attribute, valid_from"
                ).fetchall()
            else:
                rows = mem.active()
            groups: dict[str, list[dict]] = {}
            for r in rows:
                cat = r["category"] or "(uncategorized)"
                groups.setdefault(cat, []).append({
                    "id": r["id"], "attribute": r["attribute"],
                    "value": crypto.dec(r["value"]),
                    "status": r["status"], "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                })
            return [{"category": c, "facts": f} for c, f in groups.items()]
        finally:
            mem.close()

    def memory_forget(self, memory_id: int) -> dict:
        """Explicit destructive forget, including backups and enabled mirrors."""
        try:
            from livingpc.forget import forget_memory
            return {"ok": True, **forget_memory(self.cfg, int(memory_id))}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def generate_daily_report(self) -> dict:
        """On-demand markdown report of what was added today. Writes to
        reports/daily/ and opens it."""
        try:
            from livingpc.activity_report import save_daily_report
            path, markdown = save_daily_report(self.cfg)
            self._open_path(path)
            return {"ok": True, "path": path, "markdown": markdown}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def generate_full_report(self) -> dict:
        """On-demand markdown report mapping out everything in the database,
        all time. Writes to reports/ and opens it."""
        try:
            from livingpc.activity_report import save_full_report
            path, markdown = save_full_report(self.cfg)
            self._open_path(path)
            return {"ok": True, "path": path, "markdown": markdown}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def open_reports_folder(self) -> dict:
        try:
            from livingpc.activity_report import reports_dir
            self._open_path(reports_dir(self.cfg))
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    @staticmethod
    def _open_path(path) -> None:
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", path])


def main(argv=None):
    import os
    import webview
    from livingpc.ui import UI_DIR
    parser = argparse.ArgumentParser(description="Open the Faerie Fire GUI.")
    parser.add_argument("--view", default="command-center",
                        help="Initial view, e.g. command-center, growth, investigations")
    args = parser.parse_args(argv)
    log_diag("gui_startup", f"main() starting cwd={os.getcwd()} argv={argv}")
    try:
        api = GuiApi(initial_view=args.view)
        log_diag("gui_startup", f"GuiApi ready profile={getattr(api.cfg,'profile','?')}")
        window = webview.create_window(
            T("Faerie Fire", "페어리 파이어"), url=os.path.join(UI_DIR, "memory.html"), js_api=api,
            width=1500, height=900, min_size=(1024, 720),
            frameless=False, easy_drag=False, resizable=True,
            background_color="#06070f",
        )
        log_diag("gui_startup", "window created, entering webview.start()")
    except Exception:
        log_diag("gui_startup", "STARTUP FAILED:\n" + traceback.format_exc())
        raise
    api._window = window
    # NOTE: tried html= (blank window — likely WebView2's NavigateToString
    # size limit) and http_server=True (same freeze recurred) as fixes for
    # the js_api-binding race here; neither resolved it cleanly, so this is
    # back to the plain, original call pending real diagnostic data from a
    # console run. See "Undo Last Faerie Fire Change.bat" in bats/ if a
    # future change needs rolling back without waiting on me.
    webview.start()


if __name__ == "__main__":
    main()
