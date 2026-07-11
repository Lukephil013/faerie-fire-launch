"""Clarifying questions (livingpc/clarify.py + GUI bridge)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.clarify import (BIRTH_DATE_META_KEY, GRADE_YEAR_MAP_META_KEY,
                              ClarifyStore, StubClarifyModel,
                              answer, answer_many, dismiss, dismiss_many,
                              find_age_flags, find_anachronisms, find_grade_age_flags,
                              find_hedges, parse_grade_year_chart,
                              recheck_open_date_clarifications, scan,
                              set_grade_year_chart, suggest_corrected_date)
from livingpc.config import Config
from livingpc.memory import MemoryStore


class TestHedgeDetection(unittest.TestCase):
    def test_finds_the_rickey_style_hedges(self):
        value = ("Rickey is a family-adjacent figure (possibly a relative or "
                 "guardian) who gifted the family their first PlayStation.")
        hedges = find_hedges(value)
        self.assertIn("family-adjacent", hedges)
        self.assertIn("possibly", hedges)

    def test_no_hedges_in_a_plain_statement(self):
        self.assertEqual(find_hedges("He plays League every night after work."), [])

    def test_dedupes_repeated_hedges(self):
        hedges = find_hedges("Possibly this, possibly that, but POSSIBLY also this.")
        self.assertEqual(hedges, ["possibly"])


class TestTimelinePlausibility(unittest.TestCase):
    def test_flags_a_console_before_its_release(self):
        flags = find_anachronisms(
            "The family owned all major consoles of the era — N64, Xbox, and "
            "Gamecube — and played a wide library.", "1999-01-01")
        joined = " ".join(flags)
        self.assertIn("Xbox", joined)
        self.assertIn("GameCube", joined)
        self.assertNotIn("Nintendo 64", joined)   # N64 (1996) predates 1999, fine

    def test_no_anachronism_when_console_predates_the_date(self):
        self.assertEqual(
            find_anachronisms("Played N64 with my brothers.", "1999-01-01"), [])

    def test_does_not_confuse_ps2_with_bare_playstation(self):
        flags = find_anachronisms("Got a PS2 for my birthday.", "1998-01-01")
        joined = " ".join(flags)
        self.assertIn("PlayStation 2", joined)
        self.assertNotIn("PlayStation 3", joined)

    def test_flags_implausibly_young_age(self):
        flags = find_age_flags(
            "Given his first PlayStation.", "1996-01-01", "1995-08-19")
        self.assertTrue(flags)
        self.assertIn("1996-01-01", flags[0])

    def test_no_age_flag_once_old_enough(self):
        self.assertEqual(
            find_age_flags("Vivid childhood gaming memory.",
                           "1999-01-01", "1995-08-19"), [])

    def test_no_age_flag_without_a_birth_date(self):
        self.assertEqual(
            find_age_flags("Given his first PlayStation.", "1996-01-01", None), [])

    def test_flags_high_school_at_age_eight(self):
        # The actual case: born 1995-08-19, dated 2004-01-01 (age ~8.4),
        # value says "high school" — off by roughly six years.
        flags = find_grade_age_flags(
            "WoW became an emotional lifeline during high school.",
            "2004-01-01", "1995-08-19")
        self.assertTrue(flags)
        self.assertIn("high school", flags[0])

    def test_flags_first_grade_at_age_one(self):
        flags = find_grade_age_flags(
            "In 1st grade he attended CARD and was confused when his friend "
            "Daniel did not have to go.", "1996-09-01", "1995-08-19")
        self.assertTrue(flags)
        self.assertIn("1st grade", flags[0])

    def test_no_grade_flag_when_age_matches(self):
        self.assertEqual(
            find_grade_age_flags("In 1st grade he attended CARD.",
                                 "2002-09-01", "1995-08-19"), [])

    def test_no_grade_flag_without_a_grade_mention(self):
        self.assertEqual(
            find_grade_age_flags("He played WoW as an emotional lifeline.",
                                 "2004-01-01", "1995-08-19"), [])


class TestSuggestCorrectedDate(unittest.TestCase):
    def test_anachronism_only_suggests_release_year(self):
        suggested = suggest_corrected_date(
            "The family owned all major consoles of the era — N64, Xbox, and "
            "Gamecube — and played a wide library.", "1999-01-01", None)
        self.assertEqual(suggested, "2001-01-01")   # max(Xbox, GameCube) release year

    def test_grade_mismatch_suggests_birth_plus_midpoint(self):
        suggested = suggest_corrected_date(
            "WoW became an emotional lifeline during high school.",
            "2004-01-01", "1995-08-19")
        # high school range is 13.5-18.5, midpoint 16.0 -> birth year + 16
        self.assertEqual(suggested, "2011-08-19")

    def test_bare_age_only_suggests_birth_plus_min_plausible_age(self):
        suggested = suggest_corrected_date(
            "Vivid personal memory with no era-specific details.",
            "1996-01-01", "1995-08-19", min_plausible_age=2.0)
        self.assertEqual(suggested, "1997-08-19")

    def test_no_flags_no_suggestion(self):
        self.assertIsNone(suggest_corrected_date(
            "Nothing unusual here.", "2010-01-01", "1995-08-19"))

    def test_no_suggestion_without_birth_date_for_age_style_issues(self):
        # No console mentioned and no birth date -> nothing computable.
        self.assertIsNone(suggest_corrected_date(
            "A vivid personal memory.", "1996-01-01", None))


GRADE_CHART_TEXT = (
    "Kindergarten\t2000–2001 5 years old\n"
    "1st grade\t2001–2002 6 years old\n"
    "2nd grade\t2002–2003\t7 years old\n"
    "3rd grade\t2003–2004\t8 years old\n"
    "4th grade\t2004–2005\t9 years old\n"
    "5th grade\t2005–2006\t10 years old\n"
    "6th grade\t2006–2007\t11 years old\n"
    "7th grade\t2007–2008\t12 years old\n"
    "8th grade\t2008–2009\t13 years old\n"
    "9th grade / freshman\t2009–2010\t14 years old\n"
    "10th grade / sophomore\t2010–2011\t15 years old\n"
    "11th grade / junior\t2011–2012\t16 years old\n"
    "12th grade / senior\t2012–2013\t17 years old\n"
)


class TestGradeYearChart(unittest.TestCase):
    def test_parses_every_line_of_a_real_chart(self):
        parsed = parse_grade_year_chart(GRADE_CHART_TEXT)
        self.assertEqual(len(parsed), 13)
        self.assertEqual(parsed["kindergarten"], {"start": "2000-09-01", "end": "2001-06-30"})
        self.assertEqual(parsed["1st grade"], {"start": "2001-09-01", "end": "2002-06-30"})
        self.assertEqual(parsed["12th grade"], {"start": "2012-09-01", "end": "2013-06-30"})

    def test_strips_freshman_sophomore_junior_senior_aliases(self):
        parsed = parse_grade_year_chart(GRADE_CHART_TEXT)
        self.assertIn("9th grade", parsed)
        self.assertIn("10th grade", parsed)
        self.assertIn("11th grade", parsed)
        self.assertIn("12th grade", parsed)

    def test_tolerates_headers_and_blank_lines(self):
        text = "Grade\tSchool year\tAge\n\n" + GRADE_CHART_TEXT + "\n(end of chart)\n"
        parsed = parse_grade_year_chart(text)
        self.assertEqual(len(parsed), 13)

    def test_unrecognizable_text_parses_to_empty(self):
        self.assertEqual(parse_grade_year_chart("just some notes, no years here"), {})
        self.assertEqual(parse_grade_year_chart(""), {})

    def test_find_grade_age_flags_uses_exact_chart_over_generic_age(self):
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        # 1st grade was actually 2001-2002 on the chart; dated to 2005 instead.
        flags = find_grade_age_flags(
            "In 1st grade he attended CARD.", "2005-09-01", None,
            grade_year_map=grade_year_map)
        self.assertTrue(flags)
        self.assertIn("1st grade", flags[0])
        self.assertIn("your own record", flags[0])

    def test_find_grade_age_flags_no_flag_when_within_charted_window(self):
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        self.assertEqual(find_grade_age_flags(
            "In 1st grade he attended CARD.", "2001-10-01", None,
            grade_year_map=grade_year_map), [])

    def test_chart_suppresses_double_flagging_from_generic_heuristic(self):
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        # Within the chart's window, so no flag — even though a birth date
        # that would make the generic age-range heuristic complain is given.
        flags = find_grade_age_flags(
            "In 1st grade he attended CARD.", "2001-10-01", "1995-08-19",
            grade_year_map=grade_year_map)
        self.assertEqual(flags, [])

    def test_chart_still_flags_once_even_with_birth_date_present(self):
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        flags = find_grade_age_flags(
            "In 1st grade he attended CARD.", "2005-09-01", "1995-08-19",
            grade_year_map=grade_year_map)
        self.assertEqual(len(flags), 1)   # exact flag only, not also the generic one

    def test_suggest_corrected_date_prefers_exact_chart_start(self):
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        suggested = suggest_corrected_date(
            "In 1st grade he attended CARD.", "1996-01-01", "1995-08-19",
            grade_year_map=grade_year_map)
        self.assertEqual(suggested, "2001-09-01")   # chart's start, not birth+midpoint

    def test_set_grade_year_chart_stores_and_loads(self):
        with tempfile.TemporaryDirectory() as d:
            mem = MemoryStore(os.path.join(d, "memory.db"))
            try:
                count = set_grade_year_chart(mem, GRADE_CHART_TEXT)
                self.assertEqual(count, 13)
                import json
                stored = json.loads(mem.get_meta(GRADE_YEAR_MAP_META_KEY))
                self.assertEqual(stored["kindergarten"], {"start": "2000-09-01", "end": "2001-06-30"})
            finally:
                mem.close()

    def test_set_grade_year_chart_empty_text_clears_it(self):
        with tempfile.TemporaryDirectory() as d:
            mem = MemoryStore(os.path.join(d, "memory.db"))
            try:
                set_grade_year_chart(mem, GRADE_CHART_TEXT)
                count = set_grade_year_chart(mem, "")
                self.assertEqual(count, 0)
                self.assertEqual(mem.get_meta(GRADE_YEAR_MAP_META_KEY), "{}")
            finally:
                mem.close()

    def test_composite_middle_school_resolved_from_numbered_grades(self):
        # A real chart (like GRADE_CHART_TEXT) lists numbered grades, never a
        # literal "middle school" row — a memory phrased generically ("in
        # middle school") must still be checked against the 6th-8th grade
        # window it implies, not fall through to the crude generic heuristic.
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        self.assertNotIn("middle school", grade_year_map)   # confirms the gap exists
        # Middle school (6th-8th) was 2006-2009 on the chart; dated to 2021 —
        # the old bug used the generic 10.5-13.5 age-range guess here instead
        # and produced a nonsensical "you'd have been 25.8" style flag.
        flags = find_grade_age_flags(
            "I was in middle school with Eli.", "2021-05-27", "1995-08-19",
            grade_year_map=grade_year_map)
        self.assertTrue(flags)
        self.assertIn("middle school", flags[0])
        self.assertIn("your own record", flags[0])
        self.assertNotIn("25.8", flags[0])

    def test_composite_middle_school_no_flag_when_within_charted_window(self):
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        self.assertEqual(find_grade_age_flags(
            "I was in middle school with Eli.", "2007-10-01", "1995-08-19",
            grade_year_map=grade_year_map), [])

    def test_composite_high_school_resolved_from_numbered_grades(self):
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        self.assertNotIn("high school", grade_year_map)
        flags = find_grade_age_flags(
            "WoW became an emotional lifeline during high school.",
            "2021-05-27", "1995-08-19", grade_year_map=grade_year_map)
        self.assertTrue(flags)
        self.assertIn("high school", flags[0])
        self.assertIn("your own record", flags[0])

    def test_suggest_corrected_date_uses_synthesized_middle_school_window(self):
        grade_year_map = parse_grade_year_chart(GRADE_CHART_TEXT)
        suggested = suggest_corrected_date(
            "I was in middle school with Eli.", "2021-05-27", "1995-08-19",
            grade_year_map=grade_year_map)
        self.assertEqual(suggested, "2006-09-01")   # 6th grade's charted start


class TestScanAndAnswerFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(self.db)
        self.store = ClarifyStore(self.db)
        self.model = StubClarifyModel()
        self.mid = self.mem.add(
            "relationships", "Rickey — family figure who gave them PlayStation",
            "Rickey is a family-adjacent figure (possibly a relative or "
            "guardian) who gifted the family their first PlayStation and was "
            "present in his childhood home.")
        self.mem.add("gaming", "favorite genre", "Prefers fast-paced shooters.")

    def tearDown(self):
        self.mem.close()
        self.store.close()
        self.tmp.cleanup()

    def test_scan_flags_only_the_hedged_memory(self):
        created = scan(self.mem, self.store, self.model)
        self.assertEqual(created, 1)
        open_items = self.store.open_items()
        self.assertEqual(len(open_items), 1)
        self.assertEqual(open_items[0]["memory_id"], self.mid)
        self.assertIn("family-adjacent", open_items[0]["hedges"])

    def test_scan_is_idempotent_once_covered(self):
        scan(self.mem, self.store, self.model)
        again = scan(self.mem, self.store, self.model)
        self.assertEqual(again, 0)
        self.assertEqual(len(self.store.open_items()), 1)

    def test_answer_rewrites_the_memory_and_closes_the_question(self):
        scan(self.mem, self.store, self.model)
        cid = self.store.open_items()[0]["id"]
        result = answer(self.mem, self.store, cid,
                        "He was our next-door neighbor, the same age as my "
                        "oldest brother — we saw him daily but never lived "
                        "together.", self.model)
        self.assertIsNotNone(result["resulting_memory_id"])
        new_row = self.mem.get(result["resulting_memory_id"])
        self.assertEqual(new_row["status"], "active")
        old_row = self.mem.get(self.mid)
        self.assertEqual(old_row["status"], "superseded")
        self.assertNotIn(
            "family-adjacent",
            self.mem.active_as_dicts(category="relationships")[0]["value"])
        item = self.store._dict(self.store.get(cid))
        self.assertEqual(item["status"], "answered")
        self.assertEqual(item["resulting_memory_id"], result["resulting_memory_id"])

    def test_answer_without_date_correction_preserves_original_valid_from(self):
        dated_mid = self.mem.add(
            "relationships", "Rickey dated",
            "Rickey is a family-adjacent figure (possibly a relative) who "
            "gave them a PlayStation.", valid_from="1996-01-01")
        scan(self.mem, self.store, self.model)
        cid = next(i["id"] for i in self.store.open_items()
                  if i["memory_id"] == dated_mid)
        result = answer(self.mem, self.store, cid,
                        "He was our next-door neighbor.", self.model)
        new_row = self.mem.get(result["resulting_memory_id"])
        self.assertEqual(new_row["valid_from"], "1996-01-01")   # not today

    def test_answer_with_date_correction_moves_the_memory(self):
        # A console-anachronism-only memory would now auto-resolve in scan()
        # (see TestAutoResolveDateFlags), so this test exercises answer()'s
        # text-driven date-correction directly, independent of scan().
        dated_mid = self.mem.add(
            "gaming", "console library",
            "The family owned all major consoles of the era — N64, Xbox, "
            "and Gamecube — and played a wide library.", valid_from="1999-01-01")
        cid = self.store.add(dated_mid, "gaming", "console library",
                             ["Xbox wasn't released until 2001"],
                             "When did you actually get these consoles?")
        result = answer(self.mem, self.store, cid,
                        "That was actually more like 2002, after we got the "
                        "Xbox.", self.model)
        new_row = self.mem.get(result["resulting_memory_id"])
        self.assertEqual(new_row["valid_from"], "2002-01-01")

    def test_scan_auto_resolves_bare_age_flag_using_birth_date_from_meta(self):
        # No hedge here, so this is a pure date problem -> auto-resolved,
        # not left open for a question.
        self.mem.set_meta(BIRTH_DATE_META_KEY, "1995-08-19")
        dated_mid = self.mem.add(
            "relationships", "Rickey dated 2",
            "Given his first PlayStation by Rickey.", valid_from="1996-01-01")
        created = scan(self.mem, self.store, self.model)
        self.assertGreaterEqual(created, 1)
        self.assertEqual(
            [i for i in self.store.open_items() if i["memory_id"] == dated_mid], [])
        resolved = [i for i in self.store.resolved() if i["memory_id"] == dated_mid]
        self.assertEqual(len(resolved), 1)
        self.assertTrue(resolved[0]["question"].startswith("[auto]"))
        self.assertIsNotNone(resolved[0]["resulting_memory_id"])
        new_row = self.mem.get(resolved[0]["resulting_memory_id"])
        self.assertEqual(new_row["valid_from"], "1997-08-19")   # birth + 2.0y

    def test_scan_auto_resolves_high_school_at_age_eight(self):
        self.mem.set_meta(BIRTH_DATE_META_KEY, "1995-08-19")
        dated_mid = self.mem.add(
            "formative events", "World of Warcraft — got him through high school",
            "WoW became an emotional lifeline during high school, which he "
            "describes as an otherwise painful experience.", valid_from="2004-01-01")
        scan(self.mem, self.store, self.model)
        self.assertEqual(
            [i for i in self.store.open_items() if i["memory_id"] == dated_mid], [])
        resolved = [i for i in self.store.resolved() if i["memory_id"] == dated_mid]
        self.assertEqual(len(resolved), 1)
        self.assertTrue(resolved[0]["question"].startswith("[auto]"))
        new_row = self.mem.get(resolved[0]["resulting_memory_id"])
        self.assertEqual(new_row["status"], "active")
        self.assertEqual(new_row["valid_from"], "2011-08-19")   # birth + midpoint(16)

    def test_scan_auto_resolves_anachronism_only_memory_without_birth_date(self):
        # Anachronism detection doesn't need a birth date at all.
        dated_mid = self.mem.add(
            "gaming", "console library 2",
            "Got an Xbox and a Gamecube that year.", valid_from="1999-06-01")
        scan(self.mem, self.store, self.model)
        self.assertEqual(
            [i for i in self.store.open_items() if i["memory_id"] == dated_mid], [])
        resolved = [i for i in self.store.resolved() if i["memory_id"] == dated_mid]
        self.assertEqual(len(resolved), 1)
        new_row = self.mem.get(resolved[0]["resulting_memory_id"])
        self.assertEqual(new_row["valid_from"], "2001-01-01")

    def test_scan_auto_resolves_using_grade_year_chart_without_birth_date(self):
        # Chart alone is enough — no birth date needed for the exact check.
        # (suggest_corrected_date only ever moves a date forward, so the
        # wrongly-dated memory here is dated BEFORE its charted school year.)
        set_grade_year_chart(self.mem, GRADE_CHART_TEXT)
        dated_mid = self.mem.add(
            "school", "1st grade memory",
            "In 1st grade he attended CARD.", valid_from="1996-01-01")
        scan(self.mem, self.store, self.model)
        self.assertEqual(
            [i for i in self.store.open_items() if i["memory_id"] == dated_mid], [])
        resolved = [i for i in self.store.resolved() if i["memory_id"] == dated_mid]
        self.assertEqual(len(resolved), 1)
        new_row = self.mem.get(resolved[0]["resulting_memory_id"])
        self.assertEqual(new_row["valid_from"], "2001-09-01")   # chart's start, exact

    def test_scan_asks_instead_of_auto_resolving_when_hedge_present_too(self):
        # A hedge needs testimony even if a date flag also fires on the same
        # memory — auto-resolve only applies when there's nothing to ask.
        self.mem.set_meta(BIRTH_DATE_META_KEY, "1995-08-19")
        dated_mid = self.mem.add(
            "relationships", "Rickey dated 3",
            "Rickey is a family-adjacent figure (possibly a relative) who "
            "gave them a PlayStation.", valid_from="1996-01-01")
        scan(self.mem, self.store, self.model)
        open_items = [i for i in self.store.open_items() if i["memory_id"] == dated_mid]
        self.assertEqual(len(open_items), 1)

    def test_answer_requires_open_status(self):
        scan(self.mem, self.store, self.model)
        cid = self.store.open_items()[0]["id"]
        answer(self.mem, self.store, cid, "It was our neighbor.", self.model)
        with self.assertRaises(ValueError):
            answer(self.mem, self.store, cid, "again", self.model)

    def test_answer_rejects_empty_text(self):
        scan(self.mem, self.store, self.model)
        cid = self.store.open_items()[0]["id"]
        with self.assertRaises(ValueError):
            answer(self.mem, self.store, cid, "   ", self.model)

    def test_dismiss_is_terminal_and_never_resurfaces(self):
        scan(self.mem, self.store, self.model)
        cid = self.store.open_items()[0]["id"]
        dismiss(self.store, cid)
        self.assertEqual(self.store.open_items(), [])
        # a fresh scan must not re-flag the same (now-covered) memory
        self.assertEqual(scan(self.mem, self.store, self.model), 0)
        resolved = self.store.resolved()
        self.assertEqual(resolved[0]["status"], "dismissed")

    def test_unknown_clarification_id_raises(self):
        with self.assertRaises(ValueError):
            answer(self.mem, self.store, 99999, "x", self.model)
        with self.assertRaises(ValueError):
            dismiss(self.store, 99999)

    def test_dismiss_many_bulk_closes_multiple_and_reports_bad_id(self):
        self.mem.add("relationships", "second hedge",
                     "Possibly a step-sibling — unclear which side of the family.")
        scan(self.mem, self.store, self.model)
        ids = [i["id"] for i in self.store.open_items()]
        self.assertEqual(len(ids), 2)
        result = dismiss_many(self.store, ids + [99999])
        self.assertEqual(sorted(result["dismissed"]), sorted(ids))
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["id"], 99999)
        self.assertEqual(self.store.open_items(), [])

    def test_answer_many_applies_same_text_to_a_batch(self):
        self.mem.add("relationships", "second hedge",
                     "Possibly a step-sibling — unclear which side of the family.")
        scan(self.mem, self.store, self.model)
        ids = [i["id"] for i in self.store.open_items()]
        result = answer_many(self.mem, self.store, ids + [99999],
                             "It was a family friend, not a blood relation.",
                             self.model)
        self.assertEqual(len(result["answered"]), 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["id"], 99999)
        self.assertEqual(self.store.open_items(), [])
        for item in result["answered"]:
            self.assertIsNotNone(item["resulting_memory_id"])


class TestRecheckOpenDateClarifications(unittest.TestCase):
    """recheck_open_date_clarifications is the piece that answers "I just
    saved my grade chart / birth date — shouldn't my already-open questions
    resolve now?" scan() alone never revisits an item once it's queued
    (covered_memory_ids makes that permanent, by design, to avoid looping);
    this is the explicit re-evaluation path for that."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(self.db)
        self.store = ClarifyStore(self.db)
        self.model = StubClarifyModel()

    def tearDown(self):
        self.mem.close()
        self.store.close()
        self.tmp.cleanup()

    def test_recheck_resolves_a_stale_open_item_once_chart_is_saved(self):
        # Queued with no chart/birth date at all -> generic heuristic can't
        # even fire (no birth date), so it becomes a real (if useless)
        # question with no computable flags... use a birth date up front so
        # a flag exists to queue, mirroring Luke's actual case: flagged
        # BEFORE the chart existed, using the generic age-range guess.
        self.mem.set_meta(BIRTH_DATE_META_KEY, "1995-08-19")
        mid = self.mem.add("school", "middle school with Eli",
                           "I was in middle school with Eli.",
                           valid_from="2021-05-27")
        created = scan(self.mem, self.store, self.model)
        self.assertEqual(created, 1)
        cid = self.store.open_items()[0]["id"]
        self.assertIn("middle school", self.store.open_items()[0]["question"])

        # Now the chart gets saved — this is the moment recheck should fire.
        set_grade_year_chart(self.mem, GRADE_CHART_TEXT)
        resolved = recheck_open_date_clarifications(self.mem, self.store)
        self.assertEqual(resolved, 1)
        self.assertEqual(self.store.open_items(), [])
        item = self.store._dict(self.store.get(cid))
        self.assertEqual(item["status"], "answered")
        self.assertTrue(item["answer"].startswith("Date corrected"))
        new_row = self.mem.get(item["resulting_memory_id"])
        self.assertEqual(new_row["valid_from"], "2006-09-01")   # 6th grade's start
        self.assertEqual(
            self.mem.active_as_dicts(category="school")[0]["valid_from"],
            "2006-09-01")

    def test_recheck_marks_correct_when_chart_confirms_the_existing_date(self):
        self.mem.set_meta(BIRTH_DATE_META_KEY, "1995-08-19")
        self.mem.add("school", "middle school with Eli",
                     "I was in middle school with Eli.", valid_from="2021-05-27")
        scan(self.mem, self.store, self.model)
        cid = self.store.open_items()[0]["id"]
        # A chart that actually agrees with 2021 (implausible in real life,
        # but exercises the "already correct, just mark it resolved" path).
        set_grade_year_chart(self.mem, "6th grade\t2020-2021\t11 years old\n"
                                        "7th grade\t2021-2022\t12 years old\n"
                                        "8th grade\t2022-2023\t13 years old\n")
        resolved = recheck_open_date_clarifications(self.mem, self.store)
        self.assertEqual(resolved, 1)
        item = self.store._dict(self.store.get(cid))
        self.assertEqual(item["status"], "answered")
        self.assertIsNone(item["resulting_memory_id"])
        self.assertIn("no longer flagged", item["answer"])

    def test_recheck_leaves_hedge_items_untouched(self):
        self.mem.add("relationships", "Rickey",
                     "Rickey is a family-adjacent figure (possibly a "
                     "relative) who gave them a PlayStation.")
        scan(self.mem, self.store, self.model)
        self.assertEqual(len(self.store.open_items()), 1)
        resolved = recheck_open_date_clarifications(self.mem, self.store)
        self.assertEqual(resolved, 0)
        self.assertEqual(len(self.store.open_items()), 1)

    def test_recheck_leaves_still_ambiguous_items_open(self):
        # A date flag that's still real (birth date present) but with
        # nothing computable to correct it TO (too far in the future for any
        # heuristic candidate to reach) stays open rather than being forced
        # closed with a guess.
        self.mem.set_meta(BIRTH_DATE_META_KEY, "1995-08-19")
        self.mem.add("school", "1st grade memory",
                     "In 1st grade he attended CARD.", valid_from="2025-01-01")
        scan(self.mem, self.store, self.model)
        self.assertEqual(len(self.store.open_items()), 1)
        resolved = recheck_open_date_clarifications(self.mem, self.store)
        self.assertEqual(resolved, 0)
        self.assertEqual(len(self.store.open_items()), 1)

    def test_recheck_no_op_when_nothing_open(self):
        self.assertEqual(recheck_open_date_clarifications(self.mem, self.store), 0)


class TestGuiBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.memory_db_path = os.path.join(self.tmp.name, "memory.db")
        cfg.db_path = os.path.join(self.tmp.name, "living_computer.db")
        cfg.clarify_backend = "stub"
        from gui import GuiApi
        self.api = GuiApi(cfg)
        mem = MemoryStore(cfg.memory_db_path)
        self.mid = mem.add("relationships", "Rickey",
                           "Rickey is a family-adjacent figure (possibly a "
                           "relative) who gave them a PlayStation.")
        mem.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_state_answer_roundtrip(self):
        scanned = self.api.clarify_scan()
        self.assertTrue(scanned["ok"])
        self.assertEqual(scanned["created"], 1)

        state = self.api.clarify_state()
        self.assertEqual(len(state["open"]), 1)
        item = state["open"][0]
        self.assertIn("family-adjacent", item["value"])   # live value enriched in
        cid = item["id"]

        result = self.api.clarify_answer(cid, "Next-door neighbor, not family.")
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["resulting_memory_id"])

        state = self.api.clarify_state()
        self.assertEqual(state["open"], [])
        self.assertEqual(len(state["resolved"]), 1)
        self.assertEqual(state["resolved"][0]["status"], "answered")

    def test_dismiss_bridge(self):
        self.api.clarify_scan()
        cid = self.api.clarify_state()["open"][0]["id"]
        result = self.api.clarify_dismiss(cid)
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.clarify_state()["open"], [])

    def test_bridge_reports_bad_id(self):
        r = self.api.clarify_answer(99999, "x")
        self.assertFalse(r["ok"])
        r = self.api.clarify_dismiss(99999)
        self.assertFalse(r["ok"])

    def test_birth_date_roundtrip_and_validation(self):
        self.assertIsNone(self.api.clarify_state()["birth_date"])
        bad = self.api.clarify_set_birth_date("08/19/1995")
        self.assertFalse(bad["ok"])
        ok = self.api.clarify_set_birth_date("1995-08-19")
        self.assertTrue(ok["ok"])
        self.assertEqual(self.api.clarify_state()["birth_date"], "1995-08-19")

    def test_scan_auto_resolves_age_flag_once_birth_date_is_set(self):
        # No hedge on this one -> auto-resolved, not left open.
        self.api.clarify_set_birth_date("1995-08-19")
        mem = MemoryStore(self.api.cfg.memory_db_path)
        try:
            mem.add("relationships", "Rickey dated",
                    "Given his first PlayStation by Rickey.",
                    valid_from="1996-01-01")
        finally:
            mem.close()
        scanned = self.api.clarify_scan()
        self.assertTrue(scanned["ok"])
        self.assertGreaterEqual(scanned["created"], 1)
        state = self.api.clarify_state()
        flagged = [i for i in state["open"] if i["attribute"] == "Rickey dated"]
        self.assertEqual(flagged, [])
        auto = [r for r in state["resolved"] if r["attribute"] == "Rickey dated"]
        self.assertEqual(len(auto), 1)
        self.assertTrue(auto[0]["question"].startswith("[auto]"))

    def test_bulk_dismiss_bridge(self):
        mem = MemoryStore(self.api.cfg.memory_db_path)
        try:
            mem.add("relationships", "second hedge",
                    "Possibly a cousin — not certain which side of the family.")
        finally:
            mem.close()
        self.api.clarify_scan()
        ids = [i["id"] for i in self.api.clarify_state()["open"]]
        self.assertEqual(len(ids), 2)
        result = self.api.clarify_dismiss_many(ids)
        self.assertTrue(result["ok"])
        self.assertEqual(sorted(result["dismissed"]), sorted(ids))
        self.assertEqual(self.api.clarify_state()["open"], [])

    def test_bulk_answer_bridge(self):
        mem = MemoryStore(self.api.cfg.memory_db_path)
        try:
            mem.add("relationships", "second hedge",
                    "Possibly a cousin — not certain which side of the family.")
        finally:
            mem.close()
        self.api.clarify_scan()
        ids = [i["id"] for i in self.api.clarify_state()["open"]]
        self.assertEqual(len(ids), 2)
        result = self.api.clarify_answer_many(ids, "Just a close family friend.")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["answered"]), 2)
        self.assertEqual(self.api.clarify_state()["open"], [])

    def test_bulk_bridge_reports_bad_id_without_aborting(self):
        self.api.clarify_scan()
        ids = [i["id"] for i in self.api.clarify_state()["open"]]
        result = self.api.clarify_dismiss_many(ids + [99999])
        self.assertTrue(result["ok"])
        self.assertEqual(sorted(result["dismissed"]), sorted(ids))
        self.assertEqual(len(result["errors"]), 1)

    def test_grade_chart_bridge_roundtrip(self):
        self.assertEqual(self.api.clarify_state()["grade_chart_count"], 0)
        result = self.api.clarify_set_grade_chart(GRADE_CHART_TEXT)
        self.assertTrue(result["ok"])
        self.assertEqual(result["grades"], 13)
        self.assertEqual(self.api.clarify_state()["grade_chart_count"], 13)

    def test_grade_chart_bridge_rejects_unrecognizable_text(self):
        result = self.api.clarify_set_grade_chart("just some notes, no years here")
        self.assertFalse(result["ok"])
        self.assertEqual(result["grades"], 0)
        self.assertIn("couldn't recognize", result["message"])
        self.assertEqual(self.api.clarify_state()["grade_chart_count"], 0)

    def test_grade_chart_bridge_empty_text_clears_without_error(self):
        self.api.clarify_set_grade_chart(GRADE_CHART_TEXT)
        result = self.api.clarify_set_grade_chart("")
        self.assertTrue(result["ok"])
        self.assertEqual(result["grades"], 0)
        self.assertEqual(self.api.clarify_state()["grade_chart_count"], 0)

    def test_scan_auto_resolves_using_grade_chart_via_bridge(self):
        self.api.clarify_set_grade_chart(GRADE_CHART_TEXT)
        mem = MemoryStore(self.api.cfg.memory_db_path)
        try:
            mem.add("school", "1st grade memory",
                    "In 1st grade he attended CARD.", valid_from="1996-01-01")
        finally:
            mem.close()
        scanned = self.api.clarify_scan()
        self.assertTrue(scanned["ok"])
        self.assertGreaterEqual(scanned["created"], 1)
        state = self.api.clarify_state()
        flagged = [i for i in state["open"] if i["attribute"] == "1st grade memory"]
        self.assertEqual(flagged, [])
        auto = [r for r in state["resolved"] if r["attribute"] == "1st grade memory"]
        self.assertEqual(len(auto), 1)


if __name__ == "__main__":
    unittest.main()
