"""Native, companion-style workspace for bounded Faerie agents.

Only opaque mode/row IDs cross the process boundary. Private content is loaded
inside the window from the same encrypted stores and is never placed on the
command line or in diagnostics.
"""
from __future__ import annotations

import os
import sys

from livingpc.config import load
from livingpc.diagnostics import log_diag


MODES = {"inference", "goal-agent", "goal-planner", "goal-harvest"}


class AgentWindowApi:
    def __init__(self, mode: str, entity_id: int, cfg=None):
        if mode not in MODES:
            raise ValueError("unsupported agent window mode")
        self.mode = mode
        self.entity_id = int(entity_id)
        self.cfg = cfg or load("config.toml")
        self._window = None

    def state(self) -> dict:
        try:
            if self.mode == "inference":
                from livingpc.inference import InferenceStore
                store = InferenceStore(self.cfg.memory_db_path)
                try:
                    inquiry = store.inquiry(self.entity_id)
                    if not inquiry:
                        raise ValueError("investigation not found")
                    return {"ok": True, "mode": self.mode, "inquiry": inquiry}
                finally:
                    store.close()
            if self.mode == "goal-agent":
                from livingpc.goal_ai import GoalAgentStore
                from livingpc.goals import GoalStore
                goals = GoalStore(self.cfg.memory_db_path)
                agents = GoalAgentStore(self.cfg.memory_db_path, ensure=False)
                try:
                    node = goals.get(self.entity_id)
                    if not node:
                        raise ValueError("goal node not found")
                    return {"ok": True, "mode": self.mode, "node": node,
                            "agent": agents.node_view(self.entity_id)}
                finally:
                    agents.close(); goals.close()
            if self.mode == "goal-harvest":
                from livingpc.goal_ai import GoalAgentStore
                from livingpc.goals import GoalStore
                agents = GoalAgentStore(self.cfg.memory_db_path, ensure=False)
                goals = GoalStore(self.cfg.memory_db_path)
                try:
                    harvest = agents.harvest(self.entity_id)
                    return {"ok": True, "mode": self.mode, "harvest": harvest,
                            "node": goals.get(harvest["source_node_id"])}
                finally:
                    goals.close(); agents.close()
            from livingpc.goals import GoalStore
            store = GoalStore(self.cfg.memory_db_path)
            try:
                return {"ok": True, "mode": self.mode,
                        "session": store.plan_session(self.entity_id)}
            finally:
                store.close()
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def send(self, text: str) -> dict:
        try:
            if self.mode == "inference":
                from livingpc.inference import InferenceStore
                from livingpc.inference_inquiry import reply_to_inquiry
                from livingpc.memory import MemoryStore
                inf = InferenceStore(self.cfg.memory_db_path)
                mem = MemoryStore(self.cfg.memory_db_path)
                try:
                    reply_to_inquiry(self.cfg, inf, mem, self.entity_id, text)
                finally:
                    mem.close(); inf.close()
            elif self.mode == "goal-agent":
                from livingpc.goal_ai import chat_with_goal_agent
                chat_with_goal_agent(self.cfg, self.entity_id, text)
            elif self.mode == "goal-planner":
                from livingpc.goals import GoalStore, continue_planning, get_goal_planner
                store = GoalStore(self.cfg.memory_db_path)
                try:
                    continue_planning(store, get_goal_planner(self.cfg), self.entity_id, text)
                finally:
                    store.close()
            else:
                from livingpc.goal_ai import revise_goal_harvest
                revise_goal_harvest(self.cfg, self.entity_id, text)
            return self.state()
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def prepare(self) -> dict:
        """Prepare an editable commit draft for workflows that require synthesis."""
        if self.mode != "goal-planner":
            return self.state()
        try:
            from livingpc.goals import GoalStore, get_goal_planner, summarize_plan
            store = GoalStore(self.cfg.memory_db_path)
            try:
                summarize_plan(store, get_goal_planner(self.cfg), self.entity_id)
            finally:
                store.close()
            return self.state()
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def commit(self, payload=None) -> dict:
        payload = dict(payload or {})
        try:
            if self.mode == "inference":
                from livingpc.inference import InferenceStore
                store = InferenceStore(self.cfg.memory_db_path)
                try:
                    canonical_id = store.resolve_inquiry(
                        self.entity_id, str(payload.get("outcome") or "accepted"),
                        str(payload.get("statement") or ""))
                    return {"ok": True, "canonical_id": canonical_id}
                finally:
                    store.close()
            if self.mode == "goal-agent":
                from livingpc.goal_ai import decide_proposal, promote_memory_candidate
                applied, errors = [], []
                for proposal_id in payload.get("proposal_ids") or []:
                    result = decide_proposal(self.cfg, int(proposal_id), "approve")
                    (applied if result.get("ok") else errors).append(
                        int(proposal_id) if result.get("ok") else result.get("message", "failed"))
                for candidate_id in payload.get("memory_ids") or []:
                    result = promote_memory_candidate(self.cfg, int(candidate_id), "save")
                    (applied if result.get("ok") else errors).append(
                        int(candidate_id) if result.get("ok") else result.get("message", "failed"))
                return {"ok": not errors, "applied": applied, "errors": errors}
            if self.mode == "goal-harvest":
                from livingpc.goal_ai import GoalAgentStore
                store = GoalAgentStore(self.cfg.memory_db_path)
                try:
                    harvest = store.commit_harvest(
                        self.entity_id, dict(payload.get("draft") or {}))
                    return {"ok": True, "harvest": harvest}
                finally:
                    store.close()
            from livingpc.goals import GoalStore
            store = GoalStore(self.cfg.memory_db_path)
            try:
                if payload.get("draft") is not None:
                    session = store.plan_session(self.entity_id)
                    store.set_plan_draft(self.entity_id, dict(payload["draft"]),
                                         summary=session["summary"], ready=True)
                return {"ok": True, **store.commit_plan(self.entity_id)}
            finally:
                store.close()
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def toggle_expand(self) -> dict:
        """Toggle between the compact default size and a large working size."""
        try:
            if self._window is None:
                return {"ok": False, "message": "window not ready"}
            import webview
            if getattr(self, "_expanded", False):
                self._window.resize(680, 720)
                self._expanded = False
            else:
                screen = webview.screens[0]
                self._window.resize(min(1150, screen.width - 60),
                                    min(1000, screen.height - 60))
                self._expanded = True
            return {"ok": True, "expanded": self._expanded}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def minimize(self) -> dict:
        """Minimize without ending the bounded agent session."""
        try:
            if self._window is None:
                return {"ok": False, "message": "window not ready"}
            self._window.minimize()
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}

    def close(self) -> bool:
        if self._window is not None:
            self._window.destroy()
        return True

    def clipboard_read(self) -> dict:
        try:
            import tkinter as tk
            root = tk.Tk(); root.withdraw(); root.update()
            try:
                value = root.clipboard_get()
            finally:
                root.destroy()
            return {"ok": True, "text": str(value or "")}
        except Exception:
            return {"ok": True, "text": ""}

    def clipboard_write(self, text) -> dict:
        try:
            import tkinter as tk
            root = tk.Tk(); root.withdraw()
            root.clipboard_clear(); root.clipboard_append(str(text or "")); root.update()
            root.destroy()
            return {"ok": True}
        except Exception as error:
            return {"ok": False, "message": f"{type(error).__name__}: {error}"}


def main(argv=None):
    try:
        args = list(argv or sys.argv[1:])
        if len(args) != 2 or args[0] not in MODES:
            raise SystemExit("usage: agent_window.py <inference|goal-agent|goal-planner|goal-harvest> <id>")
        import webview
        from livingpc.ui import UI_DIR
        api = AgentWindowApi(args[0], int(args[1]))
        window = webview.create_window(
            "Faerie Agent", url=os.path.join(UI_DIR, "agent_window.html"), js_api=api,
            width=680, height=720, min_size=(500, 520), frameless=True,
            easy_drag=False, on_top=True, resizable=True, text_select=True,
            background_color="#06070f")
        api._window = window
        webview.start()
    except Exception as error:
        log_diag("agent-window", f"startup failed error={type(error).__name__}: {error}")
        raise


if __name__ == "__main__":
    main()
