"""The No/Kind-of feedback dialogue (livingpc/feedback.py + loop integration)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.config import Config
from livingpc.feedback import (StubFeedbackModel, feedback_questions,
                               submit_feedback)
from livingpc.inference import InferenceStore
from livingpc.inference_loop import build_context, build_synthesize_prompt
from livingpc.memory import MemoryStore


class TestFeedbackFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.inf = InferenceStore(self.db)
        self.cid = self.inf.add_candidate(
            "League of Legends", "You grind ranked to escape, not to master.",
            confidence=0.85)
        self.model = StubFeedbackModel()

    def tearDown(self):
        self.inf.close()
        self.tmp.cleanup()

    def test_questions_reference_the_claim(self):
        questions = feedback_questions(self.inf, self.cid, "no", self.model)
        self.assertTrue(1 <= len(questions) <= 3)
        self.assertIn("grind ranked", questions[0])

    def test_no_with_feedback_stores_lesson_and_rejects(self):
        result = submit_feedback(
            self.inf, self.cid, "no",
            "Wrong — I play for mastery. See https://op.gg/summoners/na/me",
            ["q1"], self.model)
        self.assertIn("mastery", result["lesson"])
        self.assertEqual(result["references"], ["https://op.gg/summoners/na/me"])
        self.assertEqual(self.inf.get(self.cid)["status"], "rejected")
        lessons = self.inf.lessons_for_theme("League of Legends")
        self.assertEqual(len(lessons), 1)
        self.assertIn("mastery", lessons[0])

    def test_kind_of_with_feedback_marks_partial(self):
        submit_feedback(self.inf, self.cid, "kind_of",
                        "Half right — it's escape on bad days only.", [], self.model)
        self.assertEqual(self.inf.get(self.cid)["status"], "partial")
        self.assertTrue(self.inf.lessons_for_theme("League of Legends"))

    def test_empty_text_applies_action_without_lesson(self):
        result = submit_feedback(self.inf, self.cid, "no", "", [], self.model)
        self.assertEqual(result["lesson"], "")
        self.assertEqual(self.inf.get(self.cid)["status"], "rejected")
        self.assertEqual(self.inf.lessons_for_theme("League of Legends"), [])

    def test_invalid_action_raises(self):
        with self.assertRaises(ValueError):
            submit_feedback(self.inf, self.cid, "yes", "x", [], self.model)

    def test_lessons_reach_the_synthesis_prompt(self):
        submit_feedback(self.inf, self.cid, "no",
                        "I care about macro improvement, not rank.", [], self.model)
        mem = MemoryStore(self.db)
        try:
            context = build_context(mem, self.inf)
        finally:
            mem.close()
        self.assertIn("League of Legends", context.lessons_by_theme)
        prompt = build_synthesize_prompt("League of Legends",
                                         ["plays 2h nightly"], context)
        self.assertIn("USER CORRECTIONS", prompt)
        self.assertIn("macro improvement", prompt)


class TestGuiBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.memory_db_path = os.path.join(self.tmp.name, "memory.db")
        cfg.db_path = os.path.join(self.tmp.name, "living_computer.db")
        cfg.inference_backend = "stub"
        from gui import GuiApi
        self.api = GuiApi(cfg)
        inf = InferenceStore(cfg.memory_db_path)
        self.cid = inf.add_candidate("focus", "You avoid mornings.", confidence=0.9)
        inf.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_bridge_roundtrip(self):
        q = self.api.feedback_questions(self.cid, "no")
        self.assertTrue(q["ok"])
        self.assertTrue(q["questions"])
        r = self.api.submit_feedback(self.cid, "no", "Mornings are my best hours.",
                                     q["questions"])
        self.assertTrue(r["ok"])
        self.assertIn("best hours", r["lesson"])
        self.assertEqual(self.api.state()["stack"], [])   # claim left the stack

    def test_bridge_reports_bad_id(self):
        r = self.api.submit_feedback(99999, "no", "x", [])
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
