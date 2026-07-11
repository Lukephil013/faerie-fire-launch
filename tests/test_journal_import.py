"""Chronological journal backfill (livingpc/journal_import.py)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.config import Config
from livingpc.journal_import import (
    UNDATED_BATCH, WATERMARK_KEY, _apply_statement, batch_by_month,
    import_journals, load_journals, parse_entries, parse_front_matter,
    StubJournalModel,
)
from livingpc.memory import MemoryStore

SAMPLE = """---
title: Test Journal
exported_at: 2026-07-02
default_year: 2026
---
Standing notes before any date. These are current-state thoughts.

06/16
Angry today, but named it and moved through it.

6/8
Rejection is painful; move through it, not around it.

05/24
I am angry that my dreams had to compete with survival.

06/2/2026
Explicit year marker entry.

06/29/29
Degenerate marker should not become 2029.
"""


def _cfg(tmp: str) -> Config:
    cfg = Config()
    cfg.memory_db_path = os.path.join(tmp, "memory.db")
    cfg.db_path = os.path.join(tmp, "living_computer.db")
    cfg.journal_dir = os.path.join(tmp, "journals")
    cfg.journal_filter_enabled = False   # these tests exercise import mechanics;
    return cfg                           # the filter has its own test module


class TestParsing(unittest.TestCase):
    def test_front_matter(self):
        meta, body = parse_front_matter(SAMPLE)
        self.assertEqual(meta["title"], "Test Journal")
        self.assertEqual(meta["default_year"], "2026")
        self.assertTrue(body.startswith("Standing notes"))

    def test_entries_dates_and_preamble(self):
        _, body = parse_front_matter(SAMPLE)
        entries = parse_entries(body, 2026)
        dates = [e["date"] for e in entries]
        self.assertEqual(dates, [None, "2026-06-16", "2026-06-08",
                                 "2026-05-24", "2026-06-02", "2026-06-29"])
        self.assertIn("Standing notes", entries[0]["text"])
        self.assertIn("named it", entries[1]["text"])

    def test_degenerate_two_digit_year_falls_back(self):
        entries = parse_entries("06/29/29\nnot twenty-twenty-nine", 2026)
        self.assertEqual(entries[0]["date"], "2026-06-29")

    def test_explicit_four_digit_year_always_trusted(self):
        entries = parse_entries("07/03/2026\na note added years later", 2021)
        self.assertEqual(entries[0]["date"], "2026-07-03")

    def test_validate_dates_catches_the_failure_modes(self):
        from livingpc.journal_import import validate_dates
        def _j(entries, exported="2026-07-03"):
            return {"source": "t", "exported_at": exported, "entries": entries}
        def _e(date, text="a reasonable entry text"):
            return {"date": date, "text": text}
        self.assertEqual(validate_dates(_j([_e("2026-05-01")])), [])
        w = validate_dates(_j([_e("2000-05-01")]))
        self.assertTrue(any("default_year" in x for x in w))
        w = validate_dates(_j([_e("2031-01-01")], exported="2026-07-03"),
                           today="2026-07-03")
        self.assertTrue(any("future" in x for x in w))
        w = validate_dates(_j([_e("2025-12-20"), _e("2025-01-05")]))
        self.assertTrue(any("rollover" in x for x in w))
        w = validate_dates(_j([_e(None), _e(None), _e("2026-05-01")]))
        self.assertTrue(any("undated" in x for x in w))
        w = validate_dates(_j([_e(None, "A history. Written on 05/27/21 ok")]))
        self.assertTrue(any("prose dates" in x for x in w))
        w = validate_dates({"source": "t", "exported_at": "",
                            "entries": [_e("2026-01-01")]})
        self.assertTrue(any("front matter" in x for x in w))

    def test_invalid_dates_are_text(self):
        entries = parse_entries("13/40\nnot a date, just text", 2026)
        self.assertEqual(entries[0]["date"], None)
        self.assertIn("13/40", entries[0]["text"])

    def test_batching_oldest_first_undated_last(self):
        journals = [{"source": "t", "exported_at": "2026-07-02",
                     "entries": parse_entries(parse_front_matter(SAMPLE)[1], 2026)}]
        batches = batch_by_month(journals)
        self.assertEqual([b[0] for b in batches],
                         ["2026-05", "2026-06", UNDATED_BATCH])
        june = dict(batches)["2026-06"]
        self.assertEqual([e["date"] for e in june],
                         ["2026-06-02", "2026-06-08", "2026-06-16", "2026-06-29"])


class TestApplyStatement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryStore(os.path.join(self.tmp.name, "memory.db"))

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def _stmt(self, value, date, category="goals", attribute="escape plan"):
        return {"category": category, "attribute": attribute,
                "value": value, "confidence": 0.9, "date": date}

    def test_add_then_supersede_builds_trajectory(self):
        active = self.mem.active_as_dicts()
        outcome, row = _apply_statement(
            self.mem, self._stmt("wants out via content", "2026-04-10"),
            "2026-07-02", active)
        self.assertEqual(outcome, "added")
        active.append(row)
        outcome, _ = _apply_statement(
            self.mem, self._stmt("wants out via AI projects", "2026-06-10"),
            "2026-07-02", active)
        self.assertEqual(outcome, "superseded")
        history = self.mem.history("goals", "escape plan")
        self.assertEqual([r["status"] for r in history], ["superseded", "active"])
        self.assertEqual(history[0]["valid_to"], "2026-06-10")   # dated, not today
        self.assertEqual(history[1]["valid_from"], "2026-06-10")

    def test_duplicate_and_stale_are_skipped(self):
        active = self.mem.active_as_dicts()
        _, row = _apply_statement(
            self.mem, self._stmt("wants out via AI projects", "2026-06-10"),
            "2026-07-02", active)
        active.append(row)
        outcome, _ = _apply_statement(
            self.mem, self._stmt("wants out via  AI projects", "2026-06-12"),
            "2026-07-02", active)
        self.assertEqual(outcome, "duplicate")
        outcome, _ = _apply_statement(
            self.mem, self._stmt("wants out via content", "2026-03-01"),
            "2026-07-02", active)
        self.assertEqual(outcome, "stale")
        self.assertEqual(len(self.mem.active()), 1)


class TestImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        os.makedirs(self.cfg.journal_dir)
        with open(os.path.join(self.cfg.journal_dir, "test-journal.md"),
                  "w", encoding="utf-8") as f:
            f.write(SAMPLE)
        self.mem = MemoryStore(self.cfg.memory_db_path)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def test_stub_import_commits_dated_facts_and_watermark(self):
        stats = import_journals(self.cfg, self.mem, model=StubJournalModel())
        self.assertEqual(stats["added"], 5)          # 5 dated entries
        self.assertEqual(self.mem.get_meta(WATERMARK_KEY), "2026-06")
        by_date = {r["valid_from"] for r in self.mem.active()}
        self.assertIn("2026-05-24", by_date)
        self.assertIn("2026-06-16", by_date)

    def test_rerun_resumes_at_watermark(self):
        import_journals(self.cfg, self.mem, model=StubJournalModel())
        again = import_journals(self.cfg, self.mem, model=StubJournalModel())
        self.assertEqual(again["added"], 0)
        self.assertEqual([m["month"] for m in again["months"]], [UNDATED_BATCH])

    def test_dry_run_commits_nothing(self):
        stats = import_journals(self.cfg, self.mem, model=StubJournalModel(),
                                dry_run=True)
        self.assertGreater(stats["added"], 0)        # would-commit count
        self.assertEqual(len(self.mem.active()), 0)
        self.assertIsNone(self.mem.get_meta(WATERMARK_KEY))

    def test_dry_run_reports_duplicates_against_memory(self):
        import_journals(self.cfg, self.mem, model=StubJournalModel())   # real
        stats = import_journals(self.cfg, self.mem, model=StubJournalModel(),
                                dry_run=True, reset=True)
        self.assertEqual(stats["added"], 0)          # everything already known
        self.assertEqual(stats["duplicate"], 5)
        self.assertEqual(len(self.mem.active()), 5)  # unchanged

    def test_confidence_gate(self):
        class LowModel(StubJournalModel):
            def propose(self, month, block, catalog):
                return [{**s, "confidence": 0.2}
                        for s in super().propose(month, block, catalog)]
        stats = import_journals(self.cfg, self.mem, model=LowModel())
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["low_confidence"], 5)

    def test_model_dates_outside_batch_are_clamped(self):
        class WildDateModel(StubJournalModel):
            def propose(self, month, block, catalog):
                out = super().propose(month, block, catalog)
                for s in out:
                    s["date"] = "1999-01-01"          # invented date
                return out
        stats = import_journals(self.cfg, self.mem, model=WildDateModel(),
                                only_month="2026-05")
        self.assertEqual(stats["added"], 1)
        dates = {r["valid_from"] for r in self.mem.active()}
        self.assertEqual(dates, {"2026-05-24"})       # clamped to batch max

    def test_only_month_filter(self):
        stats = import_journals(self.cfg, self.mem, model=StubJournalModel(),
                                only_month="2026-05")
        self.assertEqual([m["month"] for m in stats["months"]], ["2026-05"])
        self.assertEqual(stats["added"], 1)

    def test_deep_mode_event_dating_and_provenance(self):
        import json as _json

        class EventDateModel(StubJournalModel):
            calls = 0
            def propose(self, month, block, catalog, deep=False):
                assert deep
                EventDateModel.calls += 1
                out = super().propose(month, block, catalog)
                for s in out:
                    s["date"] = "2004-01-01"          # historical event date
                return out
        stats = import_journals(self.cfg, self.mem, model=EventDateModel(),
                                only_month="2026-06", deep=True)
        self.assertEqual(EventDateModel.calls, 4)     # one call per entry
        rows = self.mem.active()
        self.assertTrue(all(r["valid_from"] == "2004-01-01" for r in rows))
        refs = _json.loads(rows[0]["source_refs"])
        self.assertEqual(refs[0]["recorded"][:7], "2026-06")  # writing date kept

    def test_deep_mode_clamps_future_and_absurd_dates(self):
        class BadDateModel(StubJournalModel):
            def propose(self, month, block, catalog, deep=False):
                out = super().propose(month, block, catalog)
                for s in out:
                    s["date"] = "2099-01-01"          # future -> writing date
                return out
        import_journals(self.cfg, self.mem, model=BadDateModel(),
                        only_month="2026-05", deep=True)
        dates = {r["valid_from"] for r in self.mem.active()}
        self.assertEqual(dates, {"2026-05-24"})

    def test_month_rerun_never_regresses_watermark(self):
        import_journals(self.cfg, self.mem, model=StubJournalModel())
        self.assertEqual(self.mem.get_meta(WATERMARK_KEY), "2026-06")
        import_journals(self.cfg, self.mem, model=StubJournalModel(),
                        only_month="2026-05", reset=True)
        self.assertEqual(self.mem.get_meta(WATERMARK_KEY), "2026-06")

    def test_load_journals_reads_folder(self):
        journals = load_journals(self.cfg.journal_dir)
        self.assertEqual(len(journals), 1)
        self.assertEqual(journals[0]["source"], "Test Journal")
        self.assertEqual(journals[0]["file"], "test-journal.md")

    def test_imported_file_tracking(self):
        from livingpc.journal_import import imported_file_status
        status = imported_file_status(self.mem, self.cfg.journal_dir)
        self.assertEqual(status, {"test-journal.md": "new"})
        import_journals(self.cfg, self.mem, model=StubJournalModel())
        status = imported_file_status(self.mem, self.cfg.journal_dir)
        self.assertEqual(status, {"test-journal.md": "imported"})
        with open(os.path.join(self.cfg.journal_dir, "test-journal.md"),
                  "a", encoding="utf-8") as f:
            f.write("\n06/30\na brand new entry appended later.\n")
        status = imported_file_status(self.mem, self.cfg.journal_dir)
        self.assertEqual(status, {"test-journal.md": "changed"})

    def test_dry_run_does_not_mark_files(self):
        from livingpc.journal_import import imported_file_status
        import_journals(self.cfg, self.mem, model=StubJournalModel(),
                        dry_run=True)
        status = imported_file_status(self.mem, self.cfg.journal_dir)
        self.assertEqual(status, {"test-journal.md": "new"})


if __name__ == "__main__":
    unittest.main()
