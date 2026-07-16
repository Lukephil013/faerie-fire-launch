"""The pywebview js_api bridges behind the pretty UIs (gui, capture, assistant).

These test the Python side only — no webview window is created. Bridge classes
are importable without pywebview installed (webview is imported inside main()).
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.config import Config, load
from livingpc.inference import InferenceStore
from livingpc.memory import MemoryStore
from livingpc.ui import load_html

from gui import GuiApi
from capture_control import parse_state
from assistant import parse_hotkey


def _cfg(tmp: str) -> Config:
    cfg = Config()
    cfg.db_path = os.path.join(tmp, "living_computer.db")
    cfg.memory_db_path = os.path.join(tmp, "memory.db")
    cfg.blob_dir = os.path.join(tmp, "blobs")
    cfg.inference_backend = "stub"
    cfg.llm_backend = "stub"          # journal import + triage offline too
    cfg.notifications_enabled = False
    return cfg


class TestGuiApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        self.api = GuiApi(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_claim(self, confidence=0.9, theme="focus", statement="You lock in late at night."):
        store = InferenceStore(self.cfg.memory_db_path)
        try:
            return store.add_candidate(theme, statement, confidence=confidence)
        finally:
            store.close()

    def test_state_shape_and_gate(self):
        self._seed_claim(0.9, "focus", "Above the gate.")
        self._seed_claim(0.5, "music", "Below the gate.")
        s = self.api.state()
        self.assertEqual(sorted(s),
                         ["backend", "beliefs", "forming", "gate", "inquiries", "stack"])
        self.assertEqual(s["backend"], "stub")
        self.assertEqual([c["statement"] for c in s["stack"]], ["Above the gate."])
        self.assertIn("music", [f["theme"] for f in s["forming"]])

    def test_app_bootstrap_maps_command_center_alias(self):
        api = GuiApi(self.cfg, initial_view="command-center")
        state = api.app_bootstrap()
        self.assertTrue(state["ok"])
        self.assertEqual(state["initial_view"], "self")
        self.assertEqual(state["profile"], "personal")

    def test_onboarding_requires_an_explicit_soul_name(self):
        result = self.api.onboarding_create_soul("   ", "A meaningful purpose")

        self.assertFalse(result["ok"])
        self.assertTrue(result["message"])

    def test_leaf_workspace_bridges_wrap_workspace_views(self):
        opened_view = {"node": {"id": 7}, "phase": "shaping"}
        sent_view = {"node": {"id": 7}, "phase": "doing"}
        decided_view = {"node": {"id": 7}, "phase": "doing", "proposals": []}
        cleared_view = {"node": {"id": 7}, "messages": []}
        reopened_view = {"node": {"id": 7}, "phase": "doing", "completed": False}
        with patch("livingpc.goal_ai.open_leaf_workspace",
                   return_value=opened_view) as opened, \
             patch("livingpc.goal_ai.send_leaf_workspace",
                   return_value=sent_view) as sent, \
             patch("livingpc.goal_ai.decide_leaf_workspace_proposal",
                   return_value=decided_view) as decided, \
             patch("livingpc.goal_ai.clear_leaf_workspace_messages",
                   return_value=cleared_view) as cleared, \
             patch("livingpc.goal_ai.reopen_leaf_workspace",
                   return_value=reopened_view) as reopened:
            self.assertEqual(
                self.api.goal_leaf_workspace_open("7"),
                {"ok": True, "workspace": opened_view})
            self.assertEqual(
                self.api.goal_leaf_workspace_send(
                    "7", "Use both options", {"kind": "select", "ids": ["a", "b"]}),
                {"ok": True, "workspace": sent_view})
            self.assertEqual(
                self.api.goal_leaf_workspace_decide(
                    "7", "plan-2", "approve", {"title": "Revised plan"}),
                {"ok": True, "workspace": decided_view})
            self.assertEqual(
                self.api.goal_leaf_workspace_clear("7"),
                {"ok": True, "workspace": cleared_view})
            self.assertEqual(
                self.api.goal_leaf_workspace_reopen("7"),
                {"ok": True, "workspace": reopened_view})

        opened.assert_called_once_with(self.cfg, 7)
        sent.assert_called_once_with(
            self.cfg, 7, "Use both options",
            event={"kind": "select", "ids": ["a", "b"]})
        decided.assert_called_once_with(
            self.cfg, 7, "plan-2", "approve",
            edited_payload={"title": "Revised plan"})
        cleared.assert_called_once_with(self.cfg, 7)
        reopened.assert_called_once_with(self.cfg, 7)

    def test_leaf_workspace_bridges_reject_unsafe_arguments(self):
        with patch("livingpc.goal_ai.send_leaf_workspace") as sent, \
             patch("livingpc.goal_ai.decide_leaf_workspace_proposal") as decided:
            bad_id = self.api.goal_leaf_workspace_send(0, "hello")
            bad_event = self.api.goal_leaf_workspace_send(7, "hello", ["not", "an object"])
            bad_payload = self.api.goal_leaf_workspace_decide(
                7, "proposal-1", "approve", ["not", "an object"])
            missing_proposal = self.api.goal_leaf_workspace_decide(
                7, "", "approve")

        self.assertFalse(bad_id["ok"])
        self.assertFalse(bad_event["ok"])
        self.assertFalse(bad_payload["ok"])
        self.assertFalse(missing_proposal["ok"])
        sent.assert_not_called()
        decided.assert_not_called()

    def test_background_images_prefer_project_asset_rotation(self):
        result = self.api.background_images()
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(result["images"]), 2)
        self.assertTrue(all(path.startswith("../../assets/") for path in result["images"]))
        self.assertTrue(all("assets/backgrounds" not in path for path in result["images"]))

    def test_command_center_chat_uses_shared_companion_store(self):
        self.cfg.companion_backend = "stub"
        result = self.api.command_send("hello from command center")
        self.assertIn("hello from command center", result["text"])
        state = self.api.command_chat_state()
        self.assertTrue(state["ok"])
        self.assertEqual(state["active_chat_id"], result["active_chat_id"])
        self.assertEqual([m["role"] for m in state["messages"]], ["user", "assistant"])

        new_state = self.api.command_new_chat()
        self.assertTrue(new_state["ok"])
        self.assertNotEqual(new_state["active_chat_id"], state["active_chat_id"])

    def test_launch_profile_bootstrap_is_reported(self):
        self.cfg.profile = "launch"
        api = GuiApi(self.cfg, initial_view="inferences")
        state = api.app_bootstrap()
        self.assertEqual(state["profile"], "launch")
        self.assertEqual(state["initial_view"], "inferences")

    def test_launch_profile_forces_collectors_and_publishers_off(self):
        path = os.path.join(self.tmp.name, "config.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('profile = "launch"\n')
        cfg = load(path)
        self.assertFalse(cfg.ocr_enabled)
        self.assertFalse(cfg.browser_history_enabled)
        self.assertFalse(cfg.clipboard_enabled)
        self.assertFalse(cfg.inference_scheduler_enabled)
        self.assertFalse(cfg.triage_nightly_enabled)
        self.assertFalse(cfg.notion_sync_enabled)
        self.assertFalse(cfg.companion_lifecycle_context_enabled)

    def test_answer_yes_lands_in_beliefs(self):
        cid = self._seed_claim()
        result = self.api.answer("yes", cid)
        self.assertTrue(result["ok"])
        s = self.api.state()
        self.assertEqual(s["stack"], [])
        self.assertIn("You lock in late at night.",
                      [b["statement"] for b in s["beliefs"]])

    def test_answer_refine_stores_wording(self):
        cid = self._seed_claim()
        result = self.api.answer("refine", cid, "I chase flow after midnight.")
        self.assertTrue(result["ok"])
        statements = [b["statement"] for b in self.api.state()["beliefs"]]
        self.assertIn("I chase flow after midnight.", statements)

    def test_answer_invalid_action_reports_not_raises(self):
        cid = self._seed_claim()
        result = self.api.answer("bogus", cid)
        self.assertFalse(result["ok"])
        self.assertIn("bogus", result["message"])

    def test_memory_grouping_and_history_toggle(self):
        mem = MemoryStore(self.cfg.memory_db_path)
        first = mem.add("projects", "current", "old wording")
        mem.supersede(first, "new wording")
        mem.add("music", "practice", "korean vocab")
        mem.close()
        active = self.api.memory(False)
        cats = {g["category"]: g["facts"] for g in active}
        self.assertEqual(sorted(cats), ["music", "projects"])
        self.assertEqual([f["value"] for f in cats["projects"]], ["new wording"])
        history = self.api.memory(True)
        proj = next(g for g in history if g["category"] == "projects")
        self.assertEqual({f["status"] for f in proj["facts"]},
                         {"active", "superseded"})

    def test_memory_forget_bridge_removes_fact(self):
        self.cfg.notion_sync_enabled = False
        mem = MemoryStore(self.cfg.memory_db_path)
        memory_id = mem.add("private", "note", "forget me")
        mem.close()
        result = self.api.memory_forget(memory_id)
        self.assertTrue(result["ok"])
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            self.assertIsNone(mem.get(memory_id))
        finally:
            mem.close()

    def test_core_profile_bridge_round_trip(self):
        saved = self.api.core_profile_save([{
            "section": "Current Reality",
            "attribute": "current work situation",
            "value": "I have a current job and replacement income matters.",
            "priority": 96,
        }])
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["saved"], 1)
        self.assertIn("replacement income", saved["block"])

        state = self.api.core_profile_state()
        self.assertTrue(state["ok"])
        self.assertEqual(state["facts"][0]["section"], "Current Reality")
        self.assertEqual(state["facts"][0]["attribute"], "current work situation")
        self.assertIn("current job", state["facts"][0]["value"])

        cleared = self.api.core_profile_save([{
            "section": "Current Reality",
            "attribute": "current work situation",
            "value": "",
            "delete": True,
        }])
        self.assertTrue(cleared["ok"])
        self.assertEqual(cleared["deleted"], 1)
        self.assertEqual(cleared["facts"], [])

    def test_core_profile_change_marks_goal_ai_dirty(self):
        from livingpc.goal_ai import GoalAgentStore
        from livingpc.goals import GoalStore

        goals = GoalStore(self.cfg.memory_db_path)
        try:
            root = goals.create("overgoal", "Work")
        finally:
            goals.close()
        agents = GoalAgentStore(self.cfg.memory_db_path)
        try:
            agents.ensure_agents()
            agents.conn.execute(
                "UPDATE goal_agent_state SET dirty=0,dirty_reason=NULL")
            agents.conn.commit()
        finally:
            agents.close()

        saved = self.api.core_profile_save([{
            "section": "Core Identity",
            "attribute": "other essential context",
            "value": "My humor and sense of beauty matter.",
            "priority": 94,
        }])
        self.assertTrue(saved["ok"])
        self.assertGreaterEqual(saved["goal_ai_dirtied"], 2)

        agents = GoalAgentStore(self.cfg.memory_db_path)
        try:
            state = agents.state(root)
            self.assertTrue(state["dirty"])
            self.assertEqual(state["dirty_reason"], "core profile changed")
        finally:
            agents.close()

    def test_database_rescue_bridge_reports_and_unlocks(self):
        mem = MemoryStore(self.cfg.memory_db_path)
        try:
            mem.add("system", "probe", "private payload should not be shown")
        finally:
            mem.close()

        status = self.api.database_status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["memory"]["exists"])
        self.assertIn("files", status["memory"])
        self.assertNotIn("private payload", str(status))

        rescued = self.api.database_unlock()
        self.assertIn("memory", rescued)
        self.assertIn("events", rescued)
        self.assertNotIn("private payload", str(rescued))

    def test_run_inference_stub_returns_summary(self):
        summary = self.api.run_inference()
        self.assertNotIn("error", summary)
        self.assertIn("created", summary)
        self.assertIn("evidence_added", summary)

    def test_respond_suggestion_triggers_notion_sync_for_its_curiosity(self):
        """A suggestion belongs to a curiosity — resolving it (accept/dismiss)
        should nudge that curiosity's Notion page to resync too, same as
        answering a question does, so the page reflects the latest state
        without waiting for the next 12h background pass."""
        from livingpc.curiosity import CuriosityStore

        store = CuriosityStore(self.cfg.memory_db_path)
        cid = store.add_curiosity("get fit", "fitness")
        item_id = store.add_item(cid, "suggestion", "Try a 10-minute walk today.")
        store.close()

        calls = []
        self.api._sync_curiosity_notion_quietly = (
            lambda mem, inf, st, curiosity_id, model: calls.append(curiosity_id))

        result = self.api.curiosity_respond_suggestion(item_id, "tried")
        self.assertTrue(result["ok"])
        self.assertEqual(calls, [cid])

    def test_respond_suggestion_skips_sync_for_unknown_item(self):
        calls = []
        self.api._sync_curiosity_notion_quietly = (
            lambda mem, inf, st, curiosity_id, model: calls.append(curiosity_id))
        result = self.api.curiosity_respond_suggestion(999999, "tried")
        self.assertFalse(result["ok"])
        self.assertEqual(calls, [])

    def test_curiosity_metric_profile_approval_and_checkin(self):
        from livingpc.curiosity import CuriosityStore

        store = CuriosityStore(self.cfg.memory_db_path)
        cid = store.add_curiosity("support my mental health", "Mental health")
        store.close()
        state = self.api.curiosity_state()
        curiosity = next(row for row in state["curiosities"] if row["id"] == cid)
        profile = curiosity["metric_profile"]
        self.assertEqual(profile["status"], "draft")
        approved = self.api.curiosity_metric_approve(
            cid, profile["dimensions"], profile["state_metrics"])
        self.assertTrue(approved["ok"])
        self.assertEqual(approved["profile"]["status"], "approved")

        result = self.api.curiosity_metric_checkin(
            cid, {"energy": 4, "mood": 3}, {"regulation": 4}, "private words")
        self.assertTrue(result["ok"])
        self.assertEqual(result["snapshot"]["total_xp"], 5)
        self.assertNotIn("private words", str(result))

    def test_curiosity_journal_start_creates_seeded_investigation(self):
        result = self.api.curiosity_journal_start(
            "Current version: the old social dread premise does not apply.",
            label="social now")
        self.assertTrue(result["ok"])
        state = self.api.curiosity_state()
        row = next(c for c in state["curiosities"]
                   if c["id"] == result["curiosity_id"])
        self.assertEqual(row["label"], "social now")
        self.assertTrue(any("old social dread premise" in item["answer"]
                            for item in row["resolved"]))

    def test_curiosity_state_reports_total_counts_beyond_recent_preview(self):
        from livingpc.curiosity import CuriosityStore

        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            cid = store.add_curiosity("Why do I dread social plans?", "Social dread")
            for idx in range(12):
                item_id = store.add_item(cid, "question", f"Question {idx}?")
                store.mark_answered(item_id, f"Answer {idx}.", None)
        finally:
            store.close()
        state = self.api.curiosity_state()
        row = next(c for c in state["curiosities"] if c["id"] == cid)
        self.assertEqual(len(row["resolved"]), 10)
        self.assertEqual(row["item_counts"]["answered"], 12)
        self.assertEqual(row["item_counts"]["resolved"], 12)

    def test_curiosity_state_shows_attached_goal_path(self):
        from livingpc.curiosity import CuriosityStore
        from livingpc.goals import GoalStore

        curiosity = CuriosityStore(self.cfg.memory_db_path)
        goals = GoalStore(self.cfg.memory_db_path)
        try:
            cid = curiosity.add_curiosity(
                "why do I dislike repetitive exercise", "Exercise fit")
            root = goals.create("overgoal", "Physical Health")
            branch = goals.create("subgoal", "Find sustainable exercise",
                                  parent_id=root)
            goals.link_curiosity(branch, cid)
        finally:
            goals.close()
            curiosity.close()
        state = self.api.curiosity_state()
        row = next(c for c in state["curiosities"] if c["id"] == cid)
        self.assertEqual(row["attached_goals"][0]["goal_id"], branch)
        self.assertEqual(row["attached_goals"][0]["path"],
                         ["Actualized Self", "Physical Health",
                          "Find sustainable exercise"])

    def test_curiosity_state_keeps_classification_history(self):
        from livingpc.curiosity import CuriosityStore

        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            cid = store.add_curiosity(
                "Why do I dread meeting new people?", "Social dread")
        finally:
            store.close()
        result = self.api.curiosity_classify(cid)
        self.assertTrue(result["ok"])
        proposal = result["proposals"][0]
        dismissed = self.api.curiosity_classification_proposal(
            proposal["id"], "dismiss")
        self.assertTrue(dismissed["ok"])
        state = self.api.curiosity_state()
        row = next(c for c in state["curiosities"] if c["id"] == cid)
        self.assertEqual(row["classification_proposals"], [])
        self.assertEqual(row["classification_history"][0]["status"], "dismissed")

    def test_classification_refine_stores_context_and_dismisses_stale_proposal(self):
        from livingpc.curiosity import CuriosityStore

        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            cid = store.add_curiosity(
                "Explore where to go with my career.", "Work")
        finally:
            store.close()
        result = self.api.curiosity_classify(cid)
        self.assertTrue(result["ok"])
        proposal_id = result["proposals"][0]["id"]
        refined = self.api.curiosity_classification_refine(
            proposal_id,
            "I already have a job; proposals must treat income changes as constrained.")
        self.assertTrue(refined["ok"])
        state = self.api.curiosity_state()
        row = next(c for c in state["curiosities"] if c["id"] == cid)
        self.assertTrue(any(p["id"] == proposal_id and p["status"] == "dismissed"
                            for p in row["classification_history"]))
        self.assertIn("already have a job", row["classification_contexts"][0]["note"])

    def test_investigation_classification_can_keep_investigating(self):
        from livingpc.curiosity import CuriosityStore

        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            cid = store.add_curiosity(
                "Why do I dread meeting new people?", "Social dread")
        finally:
            store.close()
        result = self.api.curiosity_classify(cid)
        self.assertTrue(result["ok"])
        proposal = result["proposals"][0]
        self.assertEqual(proposal["type"], "keep_investigating")

    def test_approving_keep_investigating_adds_followup_question(self):
        from livingpc.curiosity import CuriosityStore

        store = CuriosityStore(self.cfg.memory_db_path)
        try:
            cid = store.add_curiosity(
                "Why do I dread meeting new people?", "Social dread")
        finally:
            store.close()
        result = self.api.curiosity_classify(cid)
        self.assertTrue(result["ok"])
        proposal = result["proposals"][0]
        self.assertEqual(proposal["type"], "keep_investigating")
        applied = self.api.curiosity_classification_proposal(
            proposal["id"], "approve")
        self.assertTrue(applied["ok"])
        self.assertIsNotNone(applied["created_question_id"])
        state = self.api.curiosity_state()
        row = next(c for c in state["curiosities"] if c["id"] == cid)
        self.assertTrue(row["open_questions"])

    def test_investigation_classification_can_create_branch_and_attach(self):
        from livingpc.curiosity import CuriosityStore
        from livingpc.goals import GoalStore

        curiosity = CuriosityStore(self.cfg.memory_db_path)
        goals = GoalStore(self.cfg.memory_db_path)
        try:
            root = goals.create("overgoal", "Mental Health")
            cid = curiosity.add_curiosity(
                "Why do I dread meeting new people?", "Social dread")
            item = curiosity.add_item(cid, "question", "What feels threatening?")
            curiosity.mark_answered(
                item, "I feel trapped performing and unsure how to exit.", None)
        finally:
            goals.close()
            curiosity.close()
        result = self.api.curiosity_classify(cid)
        self.assertTrue(result["ok"])
        proposal = next(p for p in result["proposals"] if p["status"] == "open")
        self.assertEqual(proposal["type"], "create_branch")
        applied = self.api.curiosity_classification_proposal(
            proposal["id"], "approve")
        self.assertTrue(applied["ok"])
        tree = applied["tree"]
        mental = next(x for x in tree["children"] if x["id"] == root)
        branch = next(x for x in mental["children"]
                      if x["title"] == "Reduce social threat response")
        self.assertEqual(branch["curiosities"][0]["id"], cid)
        self.assertIsNotNone(branch["origin"])
        self.assertEqual(branch["origin"]["source_kind"], "investigation")
        self.assertEqual(branch["origin"]["source_id"], str(cid))
        self.assertEqual(branch["origin"]["source_proposal_id"], proposal["id"])
        self.assertEqual(branch["origin"]["source_label"], "Social dread")
        self.assertIn("Created from Investigation", branch["origin"]["summary"])
        self.assertIn("Why do I dread meeting new people?", branch["origin"]["summary"])
        self.assertIn("What feels threatening?", branch["origin"]["detail"])
        self.assertIn("I feel trapped performing", branch["origin"]["detail"])

    def test_tried_suggestion_awards_practice_xp_once(self):
        from livingpc.curiosity import CuriosityStore
        from livingpc.curiosity_metrics import MetricStore

        store = CuriosityStore(self.cfg.memory_db_path)
        cid = store.add_curiosity("get fit", "fitness")
        item_id = store.add_item(cid, "suggestion", "Try a 10-minute walk today.")
        store.close()
        self.api._sync_curiosity_notion_quietly = lambda *args: None
        self.assertTrue(self.api.curiosity_respond_suggestion(item_id, "tried")["ok"])
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            xp = metrics.conn.execute(
                "SELECT xp FROM curiosity_metric_event WHERE curiosity_id=?", (cid,)
            ).fetchall()
            self.assertEqual([row[0] for row in xp], [20])
        finally:
            metrics.close()

    def test_tagged_practice_changes_mastery_only_with_explicit_outcome(self):
        from livingpc.curiosity import CuriosityStore
        from livingpc.curiosity_metrics import MetricStore

        store = CuriosityStore(self.cfg.memory_db_path)
        cid = store.add_curiosity("get fit", "fitness")
        item_id = store.add_item(
            cid, "suggestion", "Try a planned movement block.",
            metric_event_type="practice", metric_dimension_slug="consistency")
        store.close()
        self.api._sync_curiosity_notion_quietly = lambda *args: None
        self.assertTrue(self.api.curiosity_respond_suggestion(item_id, "tried", 8)["ok"])
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            row = metrics.conn.execute(
                "SELECT dimension_slug,observed_score,xp FROM curiosity_metric_event "
                "WHERE curiosity_id=?", (cid,)).fetchone()
            self.assertEqual(tuple(row), ("consistency", 80.0, 20))
        finally:
            metrics.close()

    def test_only_tagged_assessment_answer_awards_metric_xp_and_mastery_evidence(self):
        from livingpc.curiosity import CuriosityStore
        from livingpc.curiosity_metrics import MetricStore

        store = CuriosityStore(self.cfg.memory_db_path)
        cid = store.add_curiosity("get fit", "fitness")
        metrics = MetricStore(self.cfg.memory_db_path)
        profile = metrics.ensure_profile(store.get_curiosity(cid))
        metrics.approve_profile(cid, dimensions=profile.dimensions,
                                state_metrics=profile.state_metrics)
        metrics.close()
        tagged = store.add_item(
            cid, "question", "Rate your consistency today", confidence=.9,
            metric_event_type="assessment", metric_dimension_slug="consistency",
            response_type="rating")
        plain = store.add_item(cid, "question", "What felt meaningful?", confidence=.9)
        store.close()
        self.api._sync_curiosity_notion_quietly = lambda *args: None
        self.assertTrue(self.api.curiosity_answer(tagged, "I followed the plan.", 8)["ok"])
        self.assertTrue(self.api.curiosity_answer(plain, "The walk outside.")["ok"])
        metrics = MetricStore(self.cfg.memory_db_path)
        try:
            events = metrics.conn.execute(
                "SELECT event_type,dimension_slug,observed_score,xp "
                "FROM curiosity_metric_event WHERE curiosity_id=?", (cid,)).fetchall()
            self.assertEqual([tuple(row) for row in events],
                             [("assessment", "consistency", 80.0, 10)])
        finally:
            metrics.close()


class TestImportBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        self.cfg.journal_dir = os.path.join(self.tmp.name, "journals")
        self.api = GuiApi(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ingest_adds_front_matter_and_previews(self):
        r = self.api.ingest_file("notes.md", "06/16\nI realized I want out.\n", 2025)
        self.assertTrue(r["ok"])
        self.assertEqual((r["entries"], r["dated"]), (1, 1))
        self.assertEqual(r["from"], "2025-06-16")
        path = os.path.join(self.cfg.journal_dir, r["file"])
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("default_year: 2025", content)

    def test_ingest_keeps_existing_front_matter_and_dedupes_names(self):
        src = "---\ntitle: X\ndefault_year: 2024\n---\n03/01\nentry text here longer.\n"
        first = self.api.ingest_file("x.md", src, 2026)
        second = self.api.ingest_file("x.md", src, 2026)
        self.assertNotEqual(first["file"], second["file"])
        self.assertEqual(first["from"], "2024-03-01")   # 2024 wins over drop-year

    def test_ingest_rejects_bad_input(self):
        self.assertFalse(self.api.ingest_file("a.pdf", "x", 2026)["ok"])
        self.assertFalse(self.api.ingest_file("a.md", "   ", 2026)["ok"])
        r = self.api.ingest_file("a.doc", "x", 2026)
        self.assertFalse(r["ok"])
        self.assertIn("save as .docx", r["message"])

    def test_ingest_docx_base64(self):
        import base64
        import io
        import zipfile
        xml = ('<?xml version="1.0"?>'
               '<w:document xmlns:w="http://schemas.openxmlformats.org/'
               'wordprocessingml/2006/main"><w:body>'
               '<w:p><w:r><w:t>06/16</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>I realized I want out of the loop.</w:t></w:r></w:p>'
               '</w:body></w:document>')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("word/document.xml", xml)
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        r = self.api.ingest_file("diary.docx", payload, 2025, "base64")
        self.assertTrue(r["ok"])
        self.assertTrue(r["file"].endswith(".md"))     # converted
        self.assertEqual((r["dated"], r["from"]), (1, "2025-06-16"))
        bad = self.api.ingest_file("junk.docx", "bm90IGEgemlw", 2025, "base64")
        self.assertFalse(bad["ok"])

    def test_journal_files_and_reset_detection(self):
        self.api.ingest_file("old.md", "01/05\nan old dated insight entry.\n", 2025)
        listing = self.api.journal_files()
        self.assertEqual(len(listing["files"]), 1)
        self.assertFalse(listing["needs_reset"])        # never imported yet
        mem = MemoryStore(self.cfg.memory_db_path)
        mem.set_meta("journal_import_watermark", "2026-06")
        mem.close()
        self.assertTrue(self.api.journal_files()["needs_reset"])

    def test_remove_staged_file(self):
        r = self.api.ingest_file("bye.md", "06/01\nsome dated entry text here.\n", 2026)
        self.assertTrue(r["ok"])
        # mark as imported so we can check the record is cleaned too
        mem = MemoryStore(self.cfg.memory_db_path)
        mem.set_meta("journal_imported_files",
                     '{"bye.md": {"hash": "x", "at": "2026-07-03"}}')
        mem.close()
        result = self.api.remove_journal_file("bye.md")
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.journal_files()["files"], [])
        self.assertFalse(self.api.remove_journal_file("bye.md")["ok"])   # gone
        self.assertFalse(self.api.remove_journal_file("../evil.md")["ok"])
        mem = MemoryStore(self.cfg.memory_db_path)
        self.assertNotIn("bye.md", mem.get_meta("journal_imported_files"))
        mem.close()

    def test_run_journal_import_stub_import(self):
        self.api.ingest_file("j.md",
                             "06/01\nI realized I am angry because my needs were "
                             "never met, and I want to stop proving myself.\n", 2026)
        r = self.api.run_journal_import()
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["added"], 1)

    def test_run_journal_import_rejects_removed_dry_run(self):
        r = self.api.run_journal_import(dry_run=True)
        self.assertFalse(r["ok"])
        self.assertIn("dry-run preview was removed", r["message"])


class TestCaptureParseState(unittest.TestCase):
    def test_states(self):
        self.assertEqual(parse_state("... screen capturing NOW ..."), "capturing")
        self.assertEqual(parse_state("service running, quiet"), "quiet")
        self.assertEqual(parse_state("holding the lock: NO"), "stopped")
        self.assertEqual(parse_state("???"), "unknown")
        self.assertEqual(parse_state(""), "unknown")


class TestAssistantHotkey(unittest.TestCase):
    def test_parse_hotkey(self):
        self.assertEqual(parse_hotkey("ctrl+shift+space"), (0x2 | 0x4, 0x20))
        self.assertEqual(parse_hotkey("ctrl+alt+f"), (0x2 | 0x1, ord("F")))
        self.assertEqual(parse_hotkey("win+f5"), (0x8, 0x74))
        self.assertEqual(parse_hotkey("nonsense"), (0, None))


class TestUiPages(unittest.TestCase):
    def test_pages_load_and_reference_the_bridge(self):
        for name in ("memory.html", "capture.html", "assistant.html"):
            html = load_html(name)
            self.assertGreater(len(html), 1000, name)
            self.assertIn("pywebview", html, name)
            self.assertIn("</html>", html, name)

    def test_memory_page_uses_forest_glass_theme(self):
        html = load_html("memory.html")
        self.assertIn("--glass-strong:", html)
        self.assertIn('input:not([type="checkbox"]), textarea, select', html)
        self.assertIn("backdrop-filter:blur(8px)", html)
        self.assertNotIn("--card:rgba(21,26,58,.78)", html)

    def test_memory_page_includes_metric_profile_and_checkin_controls(self):
        html = load_html("memory.html")
        self.assertIn("metric-approve", html)
        self.assertIn("curiosity_metric_checkin", html)
        self.assertIn('select class="metric-score"', html)
        self.assertIn("Unable / depleted", html)
        self.assertIn("Average", html)
        self.assertIn("Excellent", html)
        self.assertIn('class="metric-prompt" value="" placeholder=', html)
        self.assertIn("curiosity_metric_publish", html)
        self.assertIn("metric-preview", html)
        self.assertIn("cur-rating", html)
        self.assertIn("cur-outcome", html)
        self.assertNotIn("btn-dry", html)
        self.assertNotIn("Preview import (dry run)", html)

    def test_investigations_tab_is_open_card_board(self):
        html = load_html("memory.html")
        self.assertIn('id="view-curiosity"', html)
        self.assertIn('id="cur-switcher"', html)
        self.assertIn('id="cur-list"', html)
        self.assertLess(html.index('id="cur-list"'), html.index('id="cur-switcher"'))
        self.assertIn("cur-card-grid", html)
        self.assertIn("cur-board-search", html)
        self.assertIn("cur-board-filters", html)
        self.assertIn("Needs answer", html)
        self.assertIn("Ready to classify", html)
        self.assertIn("CUR_BOARD_LIMIT=12", html)
        self.assertNotIn("Create investigation from journal", html)
        self.assertNotIn('id="cur-journal"', html)
        self.assertNotIn('id="cur-generate-all-btn"', html)
        self.assertNotIn('id="refresh-cur"', html)

    def test_growth_page_living_map_shell_is_present(self):
        html = load_html("memory.html")
        self.assertIn('data-view="goals" data-group="growth">Growth</div>', html)
        self.assertIn("TO DO", html)
        self.assertIn("To do:", html)
        self.assertNotIn('id="refresh-goals"', html)
        self.assertNotIn("Growth opens as your living map", html)
        self.assertNotIn("Nothing urgent here", html)
        self.assertIn("Edit Details", html)
        self.assertNotIn("Map view", html)
        self.assertNotIn("List view", html)
        self.assertIn("goal-map-main", html)
        self.assertIn("goal-focus-panel", html)
        self.assertIn("What are we trying to answer?", html)
        self.assertIn("Submit answers & review node", html)
        self.assertIn("goal-focus-answer", html)
        self.assertIn("focus_answer", html)
        self.assertIn("goal-list-view", html)
        self.assertIn("goal-ai-strip", html)
        self.assertIn("goal-edit-drawer", html)
        self.assertIn("growth-map-overlay", html)
        self.assertNotIn("Expand map", html)
        self.assertIn("goal_update", html)
        self.assertIn("goal_move", html)
        self.assertIn("goal_link_curiosity", html)
        self.assertIn("goal_mastery_record", html)
        self.assertIn("max-width:none", html)
        self.assertIn("min-height:calc(100vh - 86px)", html)
        self.assertIn("height:max(720px,calc(100vh - 126px))", html)

    def test_command_center_chat_shell_exists(self):
        html = load_html("memory.html")
        self.assertIn("Command Center", html)
        self.assertIn("command-center-shell", html)
        self.assertIn("grid-template-columns:176px minmax(0,1fr)", html)
        self.assertIn("left:14px; bottom:18px", html)
        self.assertIn("left:58px; bottom:18px", html)
        self.assertNotIn("your second brain", html)
        self.assertIn("height:max(860px,calc(100vh - 44px))", html)
        self.assertIn("command-center-shell.no-sidebar", html)
        self.assertIn("self-panel self-identity-card", html)
        self.assertIn("overflow:visible", html)
        self.assertIn("cc-chat-list", html)
        self.assertIn("cc-input", html)
        self.assertIn("command_send", html)
        self.assertIn("command_new_chat", html)
        self.assertIn("command_delete_chat", html)
        self.assertIn("command_attach_file", html)
        self.assertNotIn("selfRadarSvg", html)
        self.assertNotIn("saveSelfPortraitPrefs", html)

    def test_self_page_core_controls_exist(self):
        html = load_html("memory.html")
        self.assertIn("self-soul-calibration", html)
        self.assertIn("soul-calibration-drawer", html)
        self.assertIn("command_calibration_status", html)
        self.assertIn("settings-cog", html)
        self.assertIn("Open Timeline", html)
        self.assertIn("Open Import", html)
        self.assertIn("command_calibration_save", html)
        self.assertIn("command_calibration_synthesis", html)
        self.assertIn("self-database-rescue", html)
        self.assertIn("database_unlock", html)
        self.assertIn("cur-quick-answer", html)
        self.assertIn("curiosity_answer(item.id, text, rating?parseFloat(rating.value):null,", html)
        self.assertIn("withDbRetry", html)
        self.assertIn("bindConstellationPanZoom", html)


if __name__ == "__main__":
    unittest.main()
