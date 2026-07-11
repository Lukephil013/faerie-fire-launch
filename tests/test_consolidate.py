"""Memory consolidation — dedupe + pruning (livingpc/consolidate.py)."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.consolidate import consolidate, find_duplicates, report
from livingpc.inference import InferenceStore
from livingpc.memory import MemoryStore


def _old_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestConsolidate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(self.db)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_exact_duplicates_merge_newest_survives(self):
        old_id = self.mem.add("projects", "current", "Faerie Fire")
        new_id = self.mem.add("projects", "current", "faerie  fire")  # same, normalized
        result = consolidate(self.mem)
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["merges"], [(old_id, new_id)])
        closed = self.mem.get(old_id)
        self.assertEqual(closed["status"], "superseded")
        self.assertIsNotNone(closed["valid_to"])
        refs = json.loads(closed["source_refs"])
        self.assertEqual(refs[-1]["consolidated_into"], new_id)
        survivor = self.mem.get(new_id)
        self.assertEqual(survivor["status"], "active")

    def test_near_duplicates_merge_on_token_overlap(self):
        self.mem.add("music", "practice", "studies Korean vocabulary every evening")
        self.mem.add("music", "practice", "studies Korean vocabulary every single evening")
        groups = find_duplicates(self.mem, similarity=0.75)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["duplicates"]), 1)

    def test_distinct_values_are_never_merged(self):
        self.mem.add("projects", "active", "Faerie Fire memory engine")
        self.mem.add("projects", "active", "Korean study Anki deck")
        result = consolidate(self.mem)
        self.assertEqual(result["merged"], 0)
        self.assertEqual(len(self.mem.active()), 2)

    def test_different_attributes_are_separate_groups(self):
        self.mem.add("habits", "morning", "coffee first thing")
        self.mem.add("habits", "evening", "coffee first thing")
        self.assertEqual(find_duplicates(self.mem), [])

    def test_superseded_facts_are_ignored(self):
        first = self.mem.add("projects", "current", "old wording")
        self.mem.supersede(first, "new wording")
        self.mem.add("projects", "current", "old wording")  # matches a CLOSED fact only
        result = consolidate(self.mem)
        self.assertEqual(result["merged"], 0)

    def test_stale_rejections_pruned_recent_kept(self):
        self.mem.add_rejection("statement", "projects", "recent no")
        self.mem.conn.execute(
            "INSERT INTO rejected (kind, category, label, created_at) VALUES (?,?,?,?)",
            ("statement", "projects", "ancient no", _old_iso(120)))
        self.mem.conn.commit()
        result = consolidate(self.mem, rejection_retention_days=90)
        self.assertEqual(result["pruned_rejections"], 1)
        self.assertEqual(self.mem.count_rejections(), 1)

    def test_stale_evidence_pruned_and_zero_disables(self):
        inf = InferenceStore(self.db)   # creates the evidence table in the same db
        inf.add_evidence("focus", "recent observation")
        inf.conn.execute(
            "INSERT INTO evidence (theme, observation, weight, created_at) VALUES (?,?,1.0,?)",
            ("focus", "ancient observation", _old_iso(365)))
        inf.conn.commit()
        inf.close()
        untouched = consolidate(self.mem, evidence_retention_days=0)
        self.assertEqual(untouched["pruned_evidence"], 0)
        result = consolidate(self.mem, evidence_retention_days=180)
        self.assertEqual(result["pruned_evidence"], 1)

    def test_no_evidence_table_is_fine(self):
        result = consolidate(self.mem, evidence_retention_days=180)
        self.assertEqual(result["pruned_evidence"], 0)

    def test_dry_run_changes_nothing(self):
        old_id = self.mem.add("projects", "current", "same thing")
        self.mem.add("projects", "current", "same thing")
        self.mem.conn.execute(
            "INSERT INTO rejected (kind, category, label, created_at) VALUES (?,?,?,?)",
            ("statement", "x", "ancient", _old_iso(120)))
        self.mem.conn.commit()
        result = consolidate(self.mem, dry_run=True)
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["pruned_rejections"], 1)
        self.assertEqual(self.mem.get(old_id)["status"], "active")   # untouched
        self.assertEqual(self.mem.count_rejections(), 1)
        self.assertEqual(result["active_after"], result["active_before"])

    def test_report_counts(self):
        self.mem.add("projects", "current", "a")
        self.mem.add("music", "practice", "b")
        sizes = report(self.mem)
        self.assertEqual(sizes["active"], 2)
        self.assertEqual(sizes["per_category"], {"projects": 1, "music": 1})


class TestGuiBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from livingpc.config import Config
        cfg = Config()
        cfg.memory_db_path = os.path.join(self.tmp.name, "memory.db")
        cfg.db_path = os.path.join(self.tmp.name, "living_computer.db")
        from gui import GuiApi
        self.api = GuiApi(cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_consolidate_now_merges_duplicates(self):
        mem = MemoryStore(self.api.cfg.memory_db_path)
        try:
            old_id = mem.add("projects", "current", "Faerie Fire")
            new_id = mem.add("projects", "current", "faerie  fire")
        finally:
            mem.close()
        result = self.api.consolidate_now()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["merges"], [(old_id, new_id)])
        mem = MemoryStore(self.api.cfg.memory_db_path)
        try:
            self.assertEqual(mem.get(old_id)["status"], "superseded")
            self.assertEqual(mem.get(new_id)["status"], "active")
        finally:
            mem.close()

    def test_consolidate_now_on_empty_memory_is_a_no_op(self):
        result = self.api.consolidate_now()
        self.assertTrue(result["ok"])
        self.assertEqual(result["merged"], 0)


if __name__ == "__main__":
    unittest.main()
