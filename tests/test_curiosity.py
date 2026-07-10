"""Curiosity — user-directed goal pursuit (livingpc/curiosity.py + GUI bridge)."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.curiosity import (CuriosityContext, CuriosityStore, GeneratedItem,
                                StubCuriosityModel, answer_item, archive_curiosity,
                                dismiss_item, generate_items, pause_curiosity,
                                parse_items, reactivate_curiosity, respond_suggestion,
                                run_all_active, set_curiosity,
                                set_curiosity_from_journal, set_greatest,
                                _classification_origin)
from livingpc.inference import InferenceStore
from livingpc.memory import MemoryStore
from livingpc import crypto


class _FakeModel:
    """Returns a fixed, caller-supplied batch — lets tests pin exact
    confidences to probe the gating boundary precisely."""

    def __init__(self, items):
        self._items = items

    def generate(self, directive, context):
        return list(self._items)

    def resolve(self, directive, question, answer):
        return {"attribute": "note", "value": answer}


class _ContextCapturingModel(_FakeModel):
    def __init__(self, items):
        super().__init__(items)
        self.seen_contexts = []

    def generate(self, directive, context):
        self.seen_contexts.append(context)
        return super().generate(directive, context)


def _empty_context() -> CuriosityContext:
    return CuriosityContext("(none yet)", "(none)", "(none yet)", "(none)",
                            "(none yet)", "(none yet)")


class TestCuriosityStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CuriosityStore(os.path.join(self.tmp.name, "memory.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_add_and_get_curiosity(self):
        cid = self.store.add_curiosity("help with fitness", "fitness")
        row = self.store.get_curiosity(cid)
        self.assertEqual(row["directive"], "help with fitness")
        self.assertEqual(row["label"], "fitness")
        self.assertEqual(row["status"], "active")
        self.assertFalse(row["is_greatest"])

    def test_get_missing_curiosity_is_none(self):
        self.assertIsNone(self.store.get_curiosity(999))

    def test_rename_curiosity_changes_label_not_directive(self):
        cid = self.store.add_curiosity("understand Korean study", "Korean")
        renamed = self.store.rename(cid, "Korean fluency")
        self.assertEqual(renamed["label"], "Korean fluency")
        self.assertEqual(renamed["directive"], "understand Korean study")
        with self.assertRaises(ValueError):
            self.store.rename(cid, "  ")

    def test_notion_page_id_defaults_none_and_can_be_set(self):
        cid = self.store.add_curiosity("help with fitness", "fitness")
        self.assertIsNone(self.store.get_curiosity(cid)["notion_page_id"])
        self.store.set_notion_page_id(cid, "abc123")
        self.assertEqual(self.store.get_curiosity(cid)["notion_page_id"], "abc123")

    def test_set_greatest_is_exclusive(self):
        a = self.store.add_curiosity("a", "a")
        b = self.store.add_curiosity("b", "b")
        self.store.set_greatest(a)
        self.assertTrue(self.store.get_curiosity(a)["is_greatest"])
        self.store.set_greatest(b)
        self.assertFalse(self.store.get_curiosity(a)["is_greatest"])
        self.assertTrue(self.store.get_curiosity(b)["is_greatest"])

    def test_list_curiosities_greatest_first(self):
        a = self.store.add_curiosity("a", "a")
        b = self.store.add_curiosity("b", "b")
        self.store.set_greatest(b)
        rows = self.store.list_curiosities()
        self.assertEqual(rows[0]["id"], b)
        self.assertEqual({r["id"] for r in rows}, {a, b})

    def test_set_status_validates(self):
        cid = self.store.add_curiosity("a", "a")
        self.store.set_status(cid, "paused")
        self.assertEqual(self.store.get_curiosity(cid)["status"], "paused")
        with self.assertRaises(ValueError):
            self.store.set_status(cid, "bogus")

    def test_list_curiosities_filters_by_status(self):
        a = self.store.add_curiosity("a", "a")
        self.store.add_curiosity("b", "b")
        self.store.set_status(a, "archived")
        active = self.store.list_curiosities(status="active")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["label"], "b")

    def test_add_item_and_open_items(self):
        cid = self.store.add_curiosity("a", "a")
        iid = self.store.add_item(cid, "question", "what now?", confidence=0.9)
        open_items = self.store.open_items(cid)
        self.assertEqual(len(open_items), 1)
        self.assertEqual(open_items[0]["id"], iid)
        self.assertEqual(open_items[0]["status"], "open")
        self.assertEqual(self.store.open_items(), self.store.open_items(cid))

    def test_metric_item_metadata_round_trips(self):
        cid = self.store.add_curiosity("fitness", "fitness")
        iid = self.store.add_item(
            cid, "question", "rate consistency", metric_event_type="assessment",
            metric_dimension_slug="consistency", response_type="rating")
        item = self.store._item_dict(self.store.get_item(iid))
        self.assertEqual(item["metric_event_type"], "assessment")
        self.assertEqual(item["metric_dimension_slug"], "consistency")
        self.assertEqual(item["response_type"], "rating")

    def test_parse_items_keeps_valid_structured_assessment_metadata(self):
        items = parse_items('{"items":[{"kind":"question","text":"Rate it",'
                            '"confidence":0.9,"metric_event_type":"assessment",'
                            '"metric_dimension_slug":"Consistency",'
                            '"response_type":"rating"}]}')
        self.assertEqual(items[0].metric_event_type, "assessment")
        self.assertEqual(items[0].metric_dimension_slug, "consistency")
        self.assertEqual(items[0].response_type, "rating")

    def test_mark_answered_moves_out_of_open(self):
        cid = self.store.add_curiosity("a", "a")
        iid = self.store.add_item(cid, "question", "what now?")
        self.store.mark_answered(iid, "an answer", 42)
        self.assertEqual(self.store.open_items(cid), [])
        resolved = self.store.resolved(cid)
        self.assertEqual(resolved[0]["status"], "answered")
        self.assertEqual(resolved[0]["answer"], "an answer")
        self.assertEqual(resolved[0]["resulting_memory_id"], 42)

    def test_classification_origin_preserves_more_than_ten_answers_in_detail(self):
        cid = self.store.add_curiosity("understand social dread", "Social dread")
        for idx in range(12):
            iid = self.store.add_item(cid, "question", f"Question {idx}?")
            self.store.mark_answered(iid, f"Answer {idx}", None)
        origin = _classification_origin(self.store.get_curiosity(cid), {
            "id": 99,
            "type": "create_branch",
            "rationale": "The investigation is actionable now.",
            "payload": {"title": "Social energy"},
        }, self.store)
        self.assertIn("Question 0?", origin["detail"])
        self.assertIn("Answer 11", origin["detail"])
        self.assertIn("Approved proposal payload", origin["detail"])

    def test_mark_dismissed(self):
        cid = self.store.add_curiosity("a", "a")
        iid = self.store.add_item(cid, "question", "q")
        self.store.mark_dismissed(iid)
        self.assertEqual(self.store.get_item(iid)["status"], "dismissed")

    def test_mark_suggestion_resolved(self):
        cid = self.store.add_curiosity("a", "a")
        iid = self.store.add_item(cid, "suggestion", "try this")
        self.store.mark_suggestion_resolved(iid, "not_helpful_light")
        self.assertEqual(self.store.get_item(iid)["status"], "not_helpful_light")

    def test_stats(self):
        cid = self.store.add_curiosity("a", "a")
        self.store.add_item(cid, "question", "q1")
        i2 = self.store.add_item(cid, "question", "q2")
        self.store.mark_dismissed(i2)
        stats = self.store.stats()
        self.assertEqual(stats.get("open"), 1)
        self.assertEqual(stats.get("dismissed"), 1)

    def test_old_item_schema_adds_metric_metadata_without_losing_items(self):
        self.store.close()
        db = os.path.join(self.tmp.name, "legacy-items.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE curiosity (id INTEGER PRIMARY KEY, directive TEXT NOT NULL,
          label TEXT NOT NULL, status TEXT DEFAULT 'active', is_greatest INTEGER DEFAULT 0,
          created_at TEXT, last_run_at TEXT, notion_page_id TEXT);
        CREATE TABLE curiosity_item (id INTEGER PRIMARY KEY, curiosity_id INTEGER NOT NULL,
          kind TEXT NOT NULL, text TEXT NOT NULL, status TEXT DEFAULT 'open', answer TEXT,
          resulting_memory_id INTEGER, confidence REAL, created_at TEXT, resolved_at TEXT);
        INSERT INTO curiosity VALUES (1,'goal','label','active',0,NULL,NULL,NULL);
        INSERT INTO curiosity_item VALUES (1,1,'question','q','open',NULL,NULL,.9,NULL,NULL);
        """)
        conn.commit(); conn.close()
        migrated = CuriosityStore(db)
        try:
            item = migrated._item_dict(migrated.get_item(1))
            self.assertEqual(item["text"], "q")
            self.assertEqual(item["response_type"], "text")
            self.assertIsNone(item["metric_event_type"])
        finally:
            migrated.close()
        self.store = CuriosityStore(os.path.join(self.tmp.name, "memory.db"))


class TestGeneration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(db)
        self.inf = InferenceStore(db)
        self.store = CuriosityStore(db)

    def tearDown(self):
        self.mem.close()
        self.inf.close()
        self.store.close()
        self.tmp.cleanup()

    def test_question_gate_boundary(self):
        cid = self.store.add_curiosity("fitness goals", "fitness")
        model = _FakeModel([
            GeneratedItem("question", "below the bar", 0.69),
            GeneratedItem("question", "above the bar", 0.70),
        ])
        created = generate_items(self.mem, self.inf, self.store, cid, model)
        self.assertEqual(created, 1)
        texts = [i["text"] for i in self.store.open_items(cid)]
        self.assertEqual(texts, ["above the bar"])

    def test_suggestion_gate_is_higher_than_question_gate(self):
        cid = self.store.add_curiosity("fitness goals", "fitness")
        model = _FakeModel([
            GeneratedItem("suggestion", "generic guess", 0.75),   # clears question bar, not suggestion
            GeneratedItem("suggestion", "grounded pick", 0.80),
        ])
        created = generate_items(self.mem, self.inf, self.store, cid, model)
        self.assertEqual(created, 1)
        texts = [i["text"] for i in self.store.open_items(cid)]
        self.assertEqual(texts, ["grounded pick"])

    def test_limit_keeps_highest_confidence_first(self):
        cid = self.store.add_curiosity("fitness goals", "fitness")
        model = _FakeModel([
            GeneratedItem("question", "q-low", 0.75),
            GeneratedItem("question", "q-high", 0.95),
            GeneratedItem("question", "q-mid", 0.85),
        ])
        created = generate_items(self.mem, self.inf, self.store, cid, model, limit=2)
        self.assertEqual(created, 2)
        texts = {i["text"] for i in self.store.open_items(cid)}
        self.assertEqual(texts, {"q-high", "q-mid"})

    def test_grounded_suggestion_gets_a_slot_after_answers(self):
        cid = self.store.add_curiosity("fitness goals", "fitness")
        for n in range(2):
            iid = self.store.add_item(cid, "question", f"answered {n}", confidence=0.9)
            self.store.mark_answered(iid, f"answer {n}", None)
        model = _FakeModel([
            GeneratedItem("question", "q-high", 0.99),
            GeneratedItem("question", "q-next", 0.98),
            GeneratedItem("suggestion", "try a grounded next step", 0.81),
        ])
        created = generate_items(self.mem, self.inf, self.store, cid, model, limit=2)
        self.assertEqual(created, 2)
        items = self.store.open_items(cid)
        self.assertEqual({i["kind"] for i in items}, {"question", "suggestion"})
        self.assertIn("try a grounded next step", {i["text"] for i in items})

    def test_no_generation_for_paused_curiosity(self):
        cid = self.store.add_curiosity("fitness goals", "fitness")
        self.store.set_status(cid, "paused")
        model = _FakeModel([GeneratedItem("question", "q", 0.9)])
        created = generate_items(self.mem, self.inf, self.store, cid, model)
        self.assertEqual(created, 0)

    def test_no_generation_when_open_queue_is_full(self):
        cid = self.store.add_curiosity("fitness goals", "fitness")
        for i in range(3):
            self.store.add_item(cid, "question", f"q{i}", confidence=0.9)
        model = _FakeModel([GeneratedItem("question", "new one", 0.99)])
        created = generate_items(self.mem, self.inf, self.store, cid, model, max_open=3)
        self.assertEqual(created, 0)

    def test_touch_updates_last_run_at(self):
        cid = self.store.add_curiosity("fitness goals", "fitness")
        self.assertIsNone(self.store.get_curiosity(cid)["last_run_at"])
        generate_items(self.mem, self.inf, self.store, cid, StubCuriosityModel())
        self.assertIsNotNone(self.store.get_curiosity(cid)["last_run_at"])

    def test_set_curiosity_creates_and_runs_first_round(self):
        result = set_curiosity(self.mem, self.inf, self.store, "help me get fit",
                               StubCuriosityModel())
        self.assertIn("curiosity_id", result)
        self.assertGreater(result["created"], 0)
        row = self.store.get_curiosity(result["curiosity_id"])
        self.assertEqual(row["directive"], "help me get fit")

    def test_set_curiosity_from_journal_seeds_current_context_first(self):
        model = _ContextCapturingModel([
            GeneratedItem("question", "What feels most current about this?", .9)
        ])
        result = set_curiosity_from_journal(
            self.mem, self.inf, self.store,
            "The old story does not apply anymore. Current issue is uncertainty.",
            model, label="social dread")
        self.assertEqual(result["created"], 1)
        resolved = self.store.resolved(result["curiosity_id"], limit=5)
        self.assertEqual(resolved[0]["text"], "Initial journal dump / current framing")
        self.assertIn("old story does not apply", resolved[0]["answer"])
        self.assertEqual(len(model.seen_contexts), 1)
        self.assertIn("old story does not apply", model.seen_contexts[0].qa_block)

    def test_set_curiosity_from_journal_default_label_is_short_topic(self):
        result = set_curiosity_from_journal(
            self.mem, self.inf, self.store,
            "This weekend I am going to be meeting new people/potential friends, "
            "but I feel dread or more social anxiety.",
            StubCuriosityModel())
        row = self.store.get_curiosity(result["curiosity_id"])
        self.assertEqual(row["label"], "social dread")

    def test_journal_start_falls_back_to_initial_yes_no_clarifiers(self):
        class EmptyModel:
            def generate(self, directive, context):
                return []

        result = set_curiosity_from_journal(
            self.mem, self.inf, self.store,
            "I feel dread before a social event but I am not sure where it begins.",
            EmptyModel(), label="social dread")
        self.assertGreater(result["created"], 0)
        open_items = self.store.open_items(result["curiosity_id"])
        self.assertTrue(any(item["response_type"] == "yes_no" for item in open_items))

    def test_set_curiosity_default_label_from_directive(self):
        result = set_curiosity(self.mem, self.inf, self.store,
                               "you want to understand my fitness goals",
                               StubCuriosityModel())
        row = self.store.get_curiosity(result["curiosity_id"])
        self.assertTrue(row["label"])
        self.assertNotIn("you want to understand", row["label"])

    def test_set_curiosity_make_greatest(self):
        result = set_curiosity(self.mem, self.inf, self.store, "help me get fit",
                               StubCuriosityModel(), make_greatest=True)
        self.assertTrue(self.store.get_curiosity(result["curiosity_id"])["is_greatest"])

    def test_set_curiosity_empty_directive_raises(self):
        with self.assertRaises(ValueError):
            set_curiosity(self.mem, self.inf, self.store, "   ", StubCuriosityModel())

    def test_stub_model_offers_suggestion_only_after_two_answers(self):
        cid = self.store.add_curiosity("fitness goals", "fitness")
        model = StubCuriosityModel()
        created1 = generate_items(self.mem, self.inf, self.store, cid, model)
        self.assertTrue(all(i["kind"] == "question"
                            for i in self.store.open_items(cid)))
        for item in self.store.open_items(cid):
            self.store.mark_answered(item["id"], "an answer", None)
        created2 = generate_items(self.mem, self.inf, self.store, cid, model)
        kinds = {i["kind"] for i in self.store.open_items(cid)}
        self.assertIn("suggestion", kinds)
        self.assertGreater(created1 + created2, 0)


class TestAnswerDismissRespond(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(db)
        self.store = CuriosityStore(db)
        self.model = StubCuriosityModel()
        self.cid = self.store.add_curiosity("help me get fit", "fitness")

    def tearDown(self):
        self.mem.close()
        self.store.close()
        self.tmp.cleanup()

    def test_answer_item_writes_fact_and_marks_answered(self):
        iid = self.store.add_item(self.cid, "question", "What does progress look like?")
        result = answer_item(self.mem, self.store, iid, "hitting the gym 3x a week", self.model)
        self.assertIn("resulting_memory_id", result)
        mem_row = self.mem.get(result["resulting_memory_id"])
        self.assertEqual(mem_row["category"], "fitness")
        self.assertEqual(crypto.dec(mem_row["value"]), "hitting the gym 3x a week")
        provenance = self.mem.provenance(result["resulting_memory_id"])
        self.assertEqual(provenance["raw_source"], "hitting the gym 3x a week")
        self.assertEqual(provenance["source_refs"][0]["kind"], "curiosity-answer")
        self.assertEqual(self.store.get_item(iid)["status"], "answered")

    def test_answer_item_rejects_suggestion(self):
        iid = self.store.add_item(self.cid, "suggestion", "try this")
        with self.assertRaises(ValueError):
            answer_item(self.mem, self.store, iid, "ok", self.model)

    def test_answer_item_rejects_already_resolved(self):
        iid = self.store.add_item(self.cid, "question", "q")
        self.store.mark_dismissed(iid)
        with self.assertRaises(ValueError):
            answer_item(self.mem, self.store, iid, "ok", self.model)

    def test_answer_item_rejects_empty_text(self):
        iid = self.store.add_item(self.cid, "question", "q")
        with self.assertRaises(ValueError):
            answer_item(self.mem, self.store, iid, "   ", self.model)

    def test_answer_item_missing_id_raises(self):
        with self.assertRaises(ValueError):
            answer_item(self.mem, self.store, 999, "ok", self.model)

    def test_dismiss_item(self):
        iid = self.store.add_item(self.cid, "question", "q")
        dismiss_item(self.store, iid)
        self.assertEqual(self.store.get_item(iid)["status"], "dismissed")

    def test_dismiss_rejects_suggestion(self):
        iid = self.store.add_item(self.cid, "suggestion", "s")
        with self.assertRaises(ValueError):
            dismiss_item(self.store, iid)

    def test_respond_suggestion_all_actions(self):
        for action in ("tried", "not_helpful_light", "not_helpful_heavy", "dismissed"):
            iid = self.store.add_item(self.cid, "suggestion", f"suggestion for {action}")
            respond_suggestion(self.store, iid, action)
            self.assertEqual(self.store.get_item(iid)["status"], action)

    def test_respond_suggestion_rejects_unknown_action(self):
        iid = self.store.add_item(self.cid, "suggestion", "s")
        with self.assertRaises(ValueError):
            respond_suggestion(self.store, iid, "bogus")

    def test_respond_suggestion_rejects_question(self):
        iid = self.store.add_item(self.cid, "question", "q")
        with self.assertRaises(ValueError):
            respond_suggestion(self.store, iid, "tried")


class TestCuriosityLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CuriosityStore(os.path.join(self.tmp.name, "memory.db"))
        self.cid = self.store.add_curiosity("a", "a")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_pause_archive_reactivate(self):
        pause_curiosity(self.store, self.cid)
        self.assertEqual(self.store.get_curiosity(self.cid)["status"], "paused")
        archive_curiosity(self.store, self.cid)
        self.assertEqual(self.store.get_curiosity(self.cid)["status"], "archived")
        reactivate_curiosity(self.store, self.cid)
        self.assertEqual(self.store.get_curiosity(self.cid)["status"], "active")

    def test_set_greatest_helper(self):
        other = self.store.add_curiosity("b", "b")
        set_greatest(self.store, other)
        self.assertTrue(self.store.get_curiosity(other)["is_greatest"])
        self.assertFalse(self.store.get_curiosity(self.cid)["is_greatest"])


class TestRunAllActive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(db)
        self.inf = InferenceStore(db)
        self.store = CuriosityStore(db)

    def tearDown(self):
        self.mem.close()
        self.inf.close()
        self.store.close()
        self.tmp.cleanup()

    def test_greatest_gets_bigger_budget(self):
        greatest = self.store.add_curiosity("fitness", "fitness")
        self.store.set_greatest(greatest)
        background = self.store.add_curiosity("cooking", "cooking")
        model = _FakeModel([
            GeneratedItem("question", "q1", 0.99),
            GeneratedItem("question", "q2", 0.98),
            GeneratedItem("question", "q3", 0.97),
        ])
        run_all_active(self.mem, self.inf, self.store, model,
                       greatest_limit=3, background_limit=1)
        self.assertEqual(len(self.store.open_items(greatest)), 3)
        self.assertEqual(len(self.store.open_items(background)), 1)

    def test_skips_paused_and_archived(self):
        active = self.store.add_curiosity("fitness", "fitness")
        paused = self.store.add_curiosity("cooking", "cooking")
        self.store.set_status(paused, "paused")
        model = _FakeModel([GeneratedItem("question", "q", 0.99)])
        total = run_all_active(self.mem, self.inf, self.store, model,
                               greatest_limit=5, background_limit=5)
        self.assertEqual(total, 1)
        self.assertEqual(len(self.store.open_items(active)), 1)
        self.assertEqual(len(self.store.open_items(paused)), 0)


class TestNotionSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(db)
        self.inf = InferenceStore(db)
        self.store = CuriosityStore(db)
        self.model = StubCuriosityModel()
        self.cid = self.store.add_curiosity("help me get fit", "fitness")

    def tearDown(self):
        self.mem.close()
        self.inf.close()
        self.store.close()
        self.tmp.cleanup()

    def test_summary_says_nothing_confirmed_yet_before_any_answers(self):
        from livingpc.curiosity import notion_summary_markdown
        markdown = notion_summary_markdown(self.mem, self.inf, self.store, self.cid, self.model)
        self.assertIn("help me get fit", markdown)
        self.assertIn("Nothing confirmed yet", markdown)

    def test_summary_includes_qa_once_answered(self):
        from livingpc.curiosity import answer_item, notion_summary_markdown
        iid = self.store.add_item(self.cid, "question", "What does progress look like?")
        answer_item(self.mem, self.store, iid, "hitting the gym 3x a week", self.model)
        markdown = notion_summary_markdown(self.mem, self.inf, self.store, self.cid, self.model)
        self.assertIn("What does progress look like?", markdown)
        self.assertIn("hitting the gym 3x a week", markdown)

    def test_summary_missing_curiosity_raises(self):
        from livingpc.curiosity import notion_summary_markdown
        with self.assertRaises(ValueError):
            notion_summary_markdown(self.mem, self.inf, self.store, 999, self.model)


class TestGuiBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from livingpc.config import Config
        cfg = Config()
        cfg.memory_db_path = os.path.join(self.tmp.name, "memory.db")
        cfg.db_path = os.path.join(self.tmp.name, "living_computer.db")
        cfg.curiosity_backend = "stub"
        from gui import GuiApi
        self.api = GuiApi(cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_curiosity_set_creates_and_generates(self):
        result = self.api.curiosity_set("help me get fit", label="fitness")
        self.assertTrue(result["ok"])
        self.assertIn("curiosity_id", result)
        self.assertGreater(result["created"], 0)

    def test_curiosity_rename_bridge(self):
        result = self.api.curiosity_set("help me get fit", label="fitness")
        renamed = self.api.curiosity_rename(result["curiosity_id"], "movement")
        self.assertTrue(renamed["ok"])
        self.assertEqual(renamed["curiosity"]["label"], "movement")
        state = self.api.curiosity_state()
        self.assertEqual(state["curiosities"][0]["label"], "movement")

    def test_curiosity_state_shape(self):
        self.api.curiosity_set("help me get fit", label="fitness", make_greatest=True)
        state = self.api.curiosity_state()
        self.assertEqual(len(state["curiosities"]), 1)
        cur = state["curiosities"][0]
        self.assertTrue(cur["is_greatest"])
        self.assertIn("open_questions", cur)
        self.assertIn("open_suggestions", cur)
        self.assertEqual(state["archived"], [])

    def test_curiosity_answer_bridge(self):
        created = self.api.curiosity_set("help me get fit", label="fitness")
        state = self.api.curiosity_state()
        item_id = state["curiosities"][0]["open_questions"][0]["id"]
        result = self.api.curiosity_answer(item_id, "I go to the gym 3x a week")
        self.assertTrue(result["ok"])
        self.assertIn("resulting_memory_id", result)
        self.assertIn("curiosity_id", result)

    def test_curiosity_bridges_skip_notion_sync_silently_when_unconfigured(self):
        """Default Config has no notion_api_key set, so the best-effort sync
        inside these bridge methods should no-op without affecting the
        bridge's own success/failure result."""
        self.assertEqual(self.api.cfg.notion_api_key, "")
        r1 = self.api.curiosity_set("help me get fit", label="fitness")
        self.assertTrue(r1["ok"])
        state = self.api.curiosity_state()
        item_id = state["curiosities"][0]["open_questions"][0]["id"]
        r2 = self.api.curiosity_answer(item_id, "I go to the gym 3x a week")
        self.assertTrue(r2["ok"])
        r3 = self.api.curiosity_generate_more(r1["curiosity_id"])
        self.assertTrue(r3["ok"])

    def test_curiosity_dismiss_bridge(self):
        self.api.curiosity_set("help me get fit", label="fitness")
        state = self.api.curiosity_state()
        item_id = state["curiosities"][0]["open_questions"][0]["id"]
        result = self.api.curiosity_dismiss(item_id)
        self.assertTrue(result["ok"])

    def test_curiosity_respond_suggestion_bridge_rejects_bogus_action(self):
        self.api.curiosity_set("help me get fit", label="fitness")
        state = self.api.curiosity_state()
        item_id = state["curiosities"][0]["open_questions"][0]["id"]
        result = self.api.curiosity_respond_suggestion(item_id, "tried")
        self.assertFalse(result["ok"])   # it's a question, not a suggestion

    def test_curiosity_set_greatest_pause_archive_reactivate_bridge(self):
        r1 = self.api.curiosity_set("help me get fit", label="fitness")
        r2 = self.api.curiosity_set("learn to cook", label="cooking")
        self.assertTrue(self.api.curiosity_set_greatest(r2["curiosity_id"])["ok"])
        self.assertTrue(self.api.curiosity_pause(r1["curiosity_id"])["ok"])
        state = self.api.curiosity_state()
        paused = next(c for c in state["curiosities"] if c["id"] == r1["curiosity_id"])
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(self.api.curiosity_archive(r1["curiosity_id"])["ok"])
        state = self.api.curiosity_state()
        self.assertEqual(len(state["curiosities"]), 1)
        self.assertEqual(len(state["archived"]), 1)
        self.assertTrue(self.api.curiosity_reactivate(r1["curiosity_id"])["ok"])
        state = self.api.curiosity_state()
        self.assertEqual(len(state["curiosities"]), 2)

    def test_curiosity_generate_more_single_and_all(self):
        r1 = self.api.curiosity_set("help me get fit", label="fitness")
        single = self.api.curiosity_generate_more(r1["curiosity_id"])
        self.assertTrue(single["ok"])
        allc = self.api.curiosity_generate_more()
        self.assertTrue(allc["ok"])

    def test_work_classification_context_includes_career_memory(self):
        from livingpc.curiosity import CuriosityStore, _build_context, build_classification_prompt
        from livingpc.inference import InferenceStore
        from livingpc.memory import MemoryStore

        mem = MemoryStore(self.api.cfg.memory_db_path)
        inf = InferenceStore(self.api.cfg.memory_db_path)
        store = CuriosityStore(self.api.cfg.memory_db_path)
        try:
            mem.add("Work/Career", "current job",
                    "I currently have a job at Parsons and cannot leave without replacement income.")
            cid = store.add_curiosity(
                "Explore where to go with my career and income options.", "Work")
            context = _build_context(mem, inf, store, cid)
            self.assertIn("Parsons", context.facts_block)
            prompt = build_classification_prompt(
                store.get_curiosity(cid), context, "  (no goal tree)", "  (none)")
            self.assertIn("RELEVANT MEMORY FACTS / HARD CONTEXT", prompt)
            self.assertIn("current job", prompt)
        finally:
            store.close()
            inf.close()
            mem.close()


if __name__ == "__main__":
    unittest.main()
