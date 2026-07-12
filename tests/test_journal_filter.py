"""Local relevance pre-filter for the journal import (livingpc/journal_filter.py)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.config import Config
from livingpc.journal_filter import filter_entries, score_entry, trim_entry
from livingpc.journal_import import StubJournalModel, import_journals
from livingpc.memory import MemoryStore

INSIGHT = ("I realized I am angry because my needs were never met, and I want "
           "to stop proving myself. The fear is about being judged.")
ADVICE = ("You should let the anger be specific. Your nervous system needs "
          "safety. You were harmed and you might consider a new therapist. "
          "You are allowed to stop. Your body remembers this pattern deeply.")


class TestFilter(unittest.TestCase):
    def _e(self, text, date="2026-06-01"):
        return {"date": date, "text": text, "source": "t", "exported_at": "2026-07-03"}

    def test_insight_scores_above_pasted_advice(self):
        self.assertGreater(score_entry(INSIGHT), score_entry(ADVICE))

    def test_korean_first_person_insight_is_not_silently_dropped(self):
        insight = ("나는 요즘 일이 두렵다는 것을 깨달았다. 내 에너지가 떨어지면 "
                   "사람들과 이야기하는 것이 더 어렵고, 이 패턴을 바꾸고 싶다.")
        self.assertGreaterEqual(score_entry(insight), 1.0)
        kept, stats = filter_entries([self._e(insight)])
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(kept[0]["text"], insight)

    def test_short_and_low_signal_dropped_insight_kept(self):
        kept, stats = filter_entries([
            self._e("ok."), self._e(INSIGHT), self._e(ADVICE + " " + ADVICE)])
        self.assertEqual(stats["dropped_short"], 1)
        self.assertEqual(stats["dropped_low_signal"], 1)
        self.assertEqual([e["text"] for e in kept], [INSIGHT])

    def test_near_duplicates_keep_first(self):
        kept, stats = filter_entries([
            self._e(INSIGHT, "2026-05-01"),
            self._e(INSIGHT + " Yes.", "2026-06-01")])
        self.assertEqual(stats["dropped_duplicate"], 1)
        self.assertEqual(kept[0]["date"], "2026-05-01")

    def test_url_only_lines_removed(self):
        kept, _ = filter_entries([self._e(INSIGHT + "\nhttps://youtube.com/watch?v=x")])
        self.assertNotIn("youtube", kept[0]["text"])

    def test_trim_keeps_head_and_tail(self):
        text = ("START " + INSIGHT + " ") * 200 + "THE-CONCLUSION"
        trimmed = trim_entry(text, 2000)
        self.assertLess(len(trimmed), 2200)
        self.assertTrue(trimmed.startswith("START"))
        self.assertIn("THE-CONCLUSION", trimmed)
        self.assertIn("trimmed for import", trimmed)

    def test_stats_char_accounting(self):
        kept, stats = filter_entries([self._e(INSIGHT)])
        self.assertEqual(stats["kept"], 1)
        self.assertGreater(stats["chars_in"], 0)
        self.assertLessEqual(stats["chars_out"], stats["chars_in"])


class TestImportIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config()
        self.cfg.memory_db_path = os.path.join(self.tmp.name, "memory.db")
        self.cfg.db_path = os.path.join(self.tmp.name, "living_computer.db")
        self.cfg.journal_dir = os.path.join(self.tmp.name, "journals")
        os.makedirs(self.cfg.journal_dir)
        with open(os.path.join(self.cfg.journal_dir, "j.md"), "w",
                  encoding="utf-8") as f:
            f.write("---\ntitle: J\nexported_at: 2026-07-03\ndefault_year: 2026\n---\n"
                    f"06/01\n{INSIGHT}\n\n06/02\nok.\n\n06/03\n{INSIGHT} Sure.\n")
        self.mem = MemoryStore(self.cfg.memory_db_path)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_filter_runs_inside_import(self):
        stats = import_journals(self.cfg, self.mem, model=StubJournalModel())
        self.assertEqual(stats["filter"]["dropped_short"], 1)
        self.assertEqual(stats["filter"]["dropped_duplicate"], 1)
        self.assertEqual(stats["added"], 1)          # only the insight entry

    def test_no_filter_passthrough(self):
        stats = import_journals(self.cfg, self.mem, model=StubJournalModel(),
                                filter_enabled=False)
        self.assertIsNone(stats["filter"])
        self.assertEqual(stats["added"], 3)


if __name__ == "__main__":
    unittest.main()
