"""Curiosity — user-directed goal pursuit (livingpc/curiosity.py + GUI bridge)."""
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.curiosity import (ClassificationProposal, CuriosityContext,
                                CuriosityStore, GeneratedItem, StubCuriosityModel,
                                answer_item, archive_curiosity, classify_curiosity,
                                decide_classification_proposal, dismiss_item,
                                generate_items, pause_curiosity, parse_items,
                                reactivate_curiosity, respond_suggestion,
                                reassess_open_suggestions,
                                reconcile_synthesis, run_all_active, set_curiosity,
                                set_curiosity_from_journal, set_greatest,
                                merge_investigations, related_investigation_groups,
                                start_investigation_candidate,
                                suggest_exploration_threads,
                                suggest_investigation_candidates,
                                synthesize_curiosity, _classification_origin)
from livingpc.goals import GoalStore
from livingpc.inference import InferenceStore
from livingpc.memory import MemoryStore
from livingpc import crypto
from livingpc.companion.history import ChatStore


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


class _ExplorationReviewModel(_FakeModel):
    def __init__(self, items=()):
        super().__init__(items)
        self.review_calls = 0

    def suggest_threads(self, curiosity, context, synthesis, existing):
        return [
            {"title": "Existing direction", "directive": "Duplicate it."},
            {"title": "Body signals", "directive": "Compare physical signals.",
             "rationale": "The current synthesis leaves this uncertain."},
            {"title": "Work context", "directive": "Compare different work settings.",
             "rationale": "The answers mention more than one environment."},
            {"title": "Early memory", "directive": "Trace the earliest example.",
             "rationale": "The origin of the pattern is unresolved."},
        ]

    def review_suggestions(self, curiosity, context, suggestions):
        self.review_calls += 1
        return [{
            "item_id": suggestions[0]["id"],
            "status": "needs_revision",
            "confidence": .82,
            "rationale": "The new answer narrows when this experiment applies.",
            "revised_text": "Try it only after a high-conflict handoff.",
        }]


def _empty_context() -> CuriosityContext:
    return CuriosityContext("(none yet)", "(none)", "(none yet)", "(none)",
                            "(none yet)", "(none yet)")


def _candidate_payload(topic="handoff-energy", **changes):
    payload = {
        "title": "Explore handoff energy",
        "question": "When do clear handoffs protect my energy?",
        "rationale": "An approved synthesis left this uncertainty open.",
        "what_could_change": "It could clarify which transitions need preparation.",
        "evidence_refs": ["synthesis:1"],
        "relevance": .8, "uncertainty": .75, "expected_usefulness": .78,
        "burden": "low", "sensitivity": "normal", "topic_key": topic,
    }
    payload.update(changes)
    return payload


class _CandidateModel(StubCuriosityModel):
    def __init__(self, candidates):
        self.candidates = candidates
        self.context = None

    def suggest_investigations(self, context):
        self.context = context
        return list(self.candidates)


class _ClassificationModel(StubCuriosityModel):
    def __init__(self, proposals):
        self.proposals = list(proposals)

    def classify(self, curiosity, context, tree_summary, attached_summary):
        return list(self.proposals)


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

    def test_approved_investigation_context_is_deduplicated(self):
        cid = self.store.add_curiosity("understand agency reactions", "Agency")
        note = "Slowing down feels relieving and uncomfortable at the same time."
        first = self.store.add_context(cid, note, source_kind="chat", source_ref="chat-1")
        repeated = self.store.add_context(cid, "  " + note + "  ", source_kind="chat")
        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual([row["note"] for row in self.store.contexts(cid)], [note])

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

    def test_question_feedback_becomes_a_separate_preference_signal(self):
        cid = self.store.add_curiosity("a", "a")
        iid = self.store.add_item(cid, "question", "what now?")
        self.store.record_interaction_feedback(
            iid, answer_confidence=0.7, question_fit="too_broad")
        preference = self.store.interaction_preference_block(cid)
        self.assertIn("narrower, concrete questions", preference)
        row = self.store.conn.execute(
            "SELECT answer_confidence,question_fit FROM curiosity_interaction_feedback WHERE item_id=?",
            (iid,)).fetchone()
        self.assertEqual(row["question_fit"], "too_broad")
        self.assertEqual(row["answer_confidence"], 0.7)

    def test_question_feedback_is_optional_and_supports_thumbs_down(self):
        cid = self.store.add_curiosity("a", "a")
        iid = self.store.add_item(cid, "question", "what now?")
        # No feedback given -> nothing recorded (feedback is optional).
        self.store.record_interaction_feedback(iid)
        rows = self.store.conn.execute(
            "SELECT * FROM curiosity_interaction_feedback WHERE item_id=?",
            (iid,)).fetchall()
        self.assertEqual(rows, [])
        # Thumbs down maps onto its own preference signal.
        self.store.record_interaction_feedback(iid, question_fit="thumbs_down")
        preference = self.store.interaction_preference_block(cid)
        self.assertIn("narrower, clearly relevant questions", preference)

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

    def test_suggestion_feedback_reason_is_stored_and_optional(self):
        from livingpc.curiosity import respond_suggestion
        cid = self.store.add_curiosity("a", "a")
        # A "why wasn't this useful" reason lands in the answer column.
        nh = self.store.add_item(cid, "suggestion", "try this")
        respond_suggestion(self.store, nh, "not_helpful_heavy",
                           reason="too generic, I already do this")
        row = self.store.get_item(nh)
        self.assertEqual(row["status"], "not_helpful_heavy")
        self.assertEqual(row["answer"], "too generic, I already do this")
        # Dismiss with no reason still works and stores no answer.
        dm = self.store.add_item(cid, "suggestion", "try that")
        respond_suggestion(self.store, dm, "dismissed", reason=None)
        row = self.store.get_item(dm)
        self.assertEqual(row["status"], "dismissed")
        self.assertIn(row["answer"], (None, ""))

    def test_deduplicate_open_suggestions_repairs_an_existing_stack(self):
        cid = self.store.add_curiosity("a", "a")
        weaker = self.store.add_item(
            cid, "suggestion", "Block the morning for creative work.", confidence=.85)
        stronger = self.store.add_item(
            cid, "suggestion", "Take a walk before opening work chat.", confidence=.92)
        question = self.store.add_item(cid, "question", "What interrupts you?")

        dismissed = self.store.deduplicate_open_suggestions(cid)

        self.assertEqual(dismissed, [weaker])
        self.assertEqual(self.store.get_item(weaker)["status"], "dismissed")
        self.assertEqual(self.store.get_item(stronger)["status"], "open")
        self.assertEqual(self.store.get_item(question)["status"], "open")

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
            synthesis = migrated.add_synthesis(1, {
                "interpretation": "Legacy data remains available after migration.",
                "confidence": .4,
            }, based_on_item_id=1)
            self.assertEqual(synthesis["version"], 1)
            self.assertEqual(migrated._item_dict(migrated.get_item(1))["text"], "q")
        finally:
            migrated.close()
        self.store = CuriosityStore(os.path.join(self.tmp.name, "memory.db"))

    def test_candidate_gates_and_visible_limit_are_deterministic(self):
        self.assertIsNone(self.store.add_candidate(_candidate_payload(
            "low-value", expected_usefulness=.4)))
        self.assertIsNone(self.store.add_candidate(_candidate_payload(
            "high-burden", burden="high", expected_usefulness=.8)))
        for index in range(3):
            self.assertIsNotNone(self.store.add_candidate(_candidate_payload(
                f"topic-{index}", title=f"Candidate {index}",
                question=f"What could pattern {index} reveal?")))
        self.assertEqual(len(self.store.visible_candidates()), 2)

    def test_rejection_and_never_ask_are_durable_topic_blocks(self):
        rejected = self.store.add_candidate(_candidate_payload("blocked-topic"))
        self.store.decide_candidate(rejected["id"], "reject", note="Not relevant")
        self.assertTrue(self.store.candidate_topic_blocked("blocked-topic"))
        self.assertIsNone(self.store.add_candidate(_candidate_payload(
            "blocked-topic", question="A differently worded repeat?")))
        never = self.store.add_candidate(_candidate_payload("private-topic"))
        self.store.decide_candidate(never["id"], "never_ask")
        self.assertTrue(self.store.candidate_topic_blocked("private-topic"))

    def test_candidate_can_be_refined_or_deferred_without_starting(self):
        candidate = self.store.add_candidate(_candidate_payload())
        revised = dict(candidate["payload"])
        revised["question"] = "Which handoffs actually feel supportive?"
        refined = self.store.decide_candidate(
            candidate["id"], "refine", payload=revised)
        self.assertEqual(refined["payload"]["question"], revised["question"])
        deferred = self.store.decide_candidate(
            candidate["id"], "defer", defer_until="2999-01-01T00:00:00+00:00")
        self.assertEqual(deferred["status"], "deferred")
        self.assertEqual(self.store.visible_candidates(), [])
        self.assertIsNone(self.store.add_candidate(_candidate_payload(
            question="Do not resurface a deferred topic with new wording.")))
        self.assertEqual(self.store.list_curiosities(status="active"), [])


class TestProposalsDisabledByDefault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(db)
        self.inf = InferenceStore(db)
        self.store = CuriosityStore(db)

    def tearDown(self):
        self.mem.close(); self.inf.close(); self.store.close(); self.tmp.cleanup()

    def test_no_suggestions_are_generated_by_default(self):
        # An Investigation only queues questions now; the stub model would offer
        # a suggestion after two answers, but the disabled flag must drop it.
        cid = self.store.add_curiosity("fitness goals", "fitness")
        model = StubCuriosityModel()
        generate_items(self.mem, self.inf, self.store, cid, model)
        for item in self.store.open_items(cid):
            self.store.mark_answered(item["id"], "an answer", None)
        generate_items(self.mem, self.inf, self.store, cid, model)
        kinds = {i["kind"] for i in self.store.open_items(cid)}
        self.assertNotIn("suggestion", kinds)
        self.assertEqual(kinds, {"question"} if kinds else set())


class TestGeneration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = os.path.join(self.tmp.name, "memory.db")
        self.mem = MemoryStore(db)
        self.inf = InferenceStore(db)
        self.store = CuriosityStore(db)
        # These cases exercise the (now dormant) suggestion machinery, which is
        # disabled by default. Enable it here so the preserved path stays covered.
        import livingpc.curiosity as _cur
        self._proposals_prev = _cur.INVESTIGATION_PROPOSALS_ENABLED
        _cur.INVESTIGATION_PROPOSALS_ENABLED = True

    def tearDown(self):
        import livingpc.curiosity as _cur
        _cur.INVESTIGATION_PROPOSALS_ENABLED = self._proposals_prev
        self.mem.close()
        self.inf.close()
        self.store.close()
        self.tmp.cleanup()

    def test_stale_open_questions_are_retired_by_review(self):
        cid = self.store.add_curiosity("work tension", "Agency")
        stale = self.store.add_item(cid, "question",
                                    "Do you check your computer after work?",
                                    confidence=0.8)
        kept = self.store.add_item(cid, "question",
                                   "What would satisfy the threat detector?",
                                   confidence=0.9)
        answered = self.store.add_item(cid, "question", "seed", confidence=0.9)
        self.store.mark_answered(answered, "Yes — checking just delays the anxiety.", 1)

        class Reviewer(_FakeModel):
            def review_questions(self, curiosity, context, questions):
                verdicts = []
                for item in questions:
                    if item["id"] == stale:
                        verdicts.append({"item_id": item["id"],
                                         "status": "retired_stale",
                                         "confidence": 0.9,
                                         "rationale": "Already answered directly."})
                    else:
                        verdicts.append({"item_id": item["id"],
                                         "status": "still_relevant",
                                         "confidence": 0.8,
                                         "rationale": "Still open."})
                return verdicts

        generate_items(self.mem, self.inf, self.store, cid, Reviewer([]))
        retired = self.store.get_item(stale)
        surviving = self.store.get_item(kept)
        self.assertEqual(retired["status"], "dismissed")
        self.assertEqual(retired["relevance_status"], "retired_stale")
        self.assertIn("Already answered", retired["relevance_rationale"])
        self.assertEqual(surviving["status"], "open")
        self.assertEqual(surviving["relevance_status"], "still_relevant")
        # Kept questions record a watermark: the same review doesn't rerun.
        self.assertEqual(surviving["relevance_based_on_item_id"], answered)

    def test_low_confidence_retirement_is_downgraded_to_keep(self):
        cid = self.store.add_curiosity("work tension", "Agency")
        question = self.store.add_item(cid, "question", "Edge case?", confidence=0.8)
        answered = self.store.add_item(cid, "question", "seed", confidence=0.9)
        self.store.mark_answered(answered, "some answer", 1)

        class TimidReviewer(_FakeModel):
            def review_questions(self, curiosity, context, questions):
                return [{"item_id": item["id"], "status": "retired_stale",
                         "confidence": 0.5, "rationale": "Maybe stale?"}
                        for item in questions]

        generate_items(self.mem, self.inf, self.store, cid, TimidReviewer([]))
        current = self.store.get_item(question)
        self.assertEqual(current["status"], "open")
        self.assertEqual(current["relevance_status"], "still_relevant")

    def test_fresh_context_round_makes_room_in_a_full_queue(self):
        cid = self.store.add_curiosity("work tension", "Agency")
        weakest = self.store.add_item(cid, "question", "weakest", confidence=0.71)
        self.store.add_item(cid, "question", "stronger", confidence=0.9)
        self.store.add_item(cid, "question", "strongest", confidence=0.95)
        self.store.add_context(cid, "Threat detector does not clock out.",
                               source_kind="chat")
        model = _FakeModel([GeneratedItem(
            "question", "What would let you trust that you're done?", 0.9)])

        # Without the fresh-context flag a full queue still blocks generation.
        created = generate_items(self.mem, self.inf, self.store, cid, model,
                                 max_open=3)
        self.assertEqual(created, 0)
        self.assertEqual(self.store.get_item(weakest)["status"], "open")

        created = generate_items(self.mem, self.inf, self.store, cid, model,
                                 max_open=3, fresh_context=True)
        self.assertEqual(created, 1)
        evicted = self.store.get_item(weakest)
        self.assertEqual(evicted["status"], "dismissed")
        self.assertIn("make room", evicted["relevance_rationale"])
        open_texts = [item["text"] for item in self.store.open_items(cid)]
        self.assertIn("What would let you trust that you're done?", open_texts)
        self.assertEqual(len(open_texts), 3)

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

    def test_approved_chat_context_reaches_investigation_ai(self):
        cid = self.store.add_curiosity(
            "understand obligation and agency", "Agency")
        self.store.add_context(
            cid,
            "My internal clock creates urgency without an external deadline; slowing down "
            "feels relieving and uncomfortable.",
            source_kind="chat")
        model = _ContextCapturingModel([
            GeneratedItem("question", "What makes the discomfort spike?", .9)
        ])
        self.assertEqual(generate_items(self.mem, self.inf, self.store, cid, model), 1)
        self.assertIn("internal clock", model.seen_contexts[0].investigation_context_block)
        self.assertIn("relieving and uncomfortable",
                      model.seen_contexts[0].investigation_context_block)

    def test_exploration_thread_suggestions_are_grounded_bounded_and_distinct(self):
        cid = self.store.add_curiosity("understand a recurring work pattern", "Pattern")
        self.store.add_thread(cid, "Existing direction", "Already being explored.")
        question = self.store.add_item(
            cid, "question", "Where does it show up?", confidence=.9)
        self.store.mark_answered(question, "At work and during competitive games.", None)

        directions = suggest_exploration_threads(
            self.mem, self.inf, self.store, cid, _ExplorationReviewModel())

        self.assertEqual(len(directions), 3)
        self.assertEqual([item["title"] for item in directions],
                         ["Body signals", "Work context", "Early memory"])
        self.assertTrue(all(item["directive"] for item in directions))

    def test_open_suggestion_is_reassessed_once_per_new_answer(self):
        cid = self.store.add_curiosity("understand a recurring work pattern", "Pattern")
        suggestion = self.store.add_item(
            cid, "suggestion", "Try the same experiment after every handoff.",
            confidence=.9)
        question = self.store.add_item(
            cid, "question", "What did the latest example show?", confidence=.9)
        self.store.mark_answered(question, "Only high-conflict handoffs cause the crash.", None)
        model = _ExplorationReviewModel()

        reviewed = reassess_open_suggestions(
            self.mem, self.inf, self.store, cid, model)

        self.assertEqual(len(reviewed), 1)
        current = self.store.get_item(suggestion)
        self.assertEqual(current["relevance_status"], "needs_revision")
        self.assertEqual(current["relevance_based_on_item_id"], question)
        self.assertIn("high-conflict", current["relevance_revised_text"])
        reassess_open_suggestions(self.mem, self.inf, self.store, cid, model)
        self.assertEqual(model.review_calls, 1)

    def test_deep_investigation_refreshes_later_calibration_and_relevant_chat(self):
        from livingpc.curiosity import _build_context, build_synthesis_prompt

        cid = self.store.add_curiosity(
            "understand why my energy crashes after client handoffs", "Energy crashes")
        item_id = self.store.add_item(
            cid, "question", "What happens after a difficult client handoff?", confidence=.9)
        self.store.mark_answered(item_id, "I skip food and then lose focus.", None)
        baseline = self.store.add_synthesis(
            cid, {"interpretation": "Food may matter.", "confidence": .5},
            based_on_item_id=item_id)
        self.store.decide_synthesis(baseline["id"], "approve")
        self.assertFalse(self.store.synthesis_due(cid)["due"])

        # These arrive only after the Investigation already has deep evidence.
        self.mem.upsert_core_profile_fact(
            "Needs & Limits", "non-negotiable constraint",
            "I need a real lunch before demanding afternoon work.",
            source_kind="soul_calibration")
        chats = ChatStore(self.store.db_path)
        chat_id = chats.create("Energy update")
        chats.append(chat_id, "user", "My client handoff energy crash is worse after conflict.")
        chats.append(chat_id, "assistant", "You always fear every client conversation.")

        context = _build_context(self.mem, self.inf, self.store, cid)
        prompt = build_synthesis_prompt(
            self.store.get_curiosity(cid), context,
            [item for item in self.store.items_for_curiosity(cid)
             if item["status"] == "answered"], None)

        self.assertIn("real lunch", context.core_profile_block)
        self.assertIn("worse after conflict", context.chat_context_block)
        self.assertNotIn("always fear", context.chat_context_block)
        self.assertIn("later Soul Calibration answers", prompt)
        self.assertIn("worse after conflict", prompt)
        due = self.store.synthesis_due(cid)
        self.assertTrue(due["due"])
        self.assertGreaterEqual(due["new_context"], 2)

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

    def test_near_duplicate_suggestions_keep_only_the_clearer_higher_confidence_one(self):
        cid = self.store.add_curiosity("test Faerie with friends", "Faerie testing")
        first = ("After your wife tests Faerie V2, set a concrete date within one week to test it "
                 "with your friend remotely. Before that test, write down three specific things you "
                 "are watching for beyond whether they use it unprompted: clarifying questions, "
                 "helpful features, and suggested changes. This gives a clearer signal than waiting "
                 "two weeks to see whether they open it.")
        clearer = ("After your wife tests Faerie V2 this week, set a concrete date within one week "
                   "to test it with your friend remotely. Before that test, write down three specific "
                   "signals beyond whether they use it unprompted: clarifying questions about "
                   "features, helpful investigations or GoalAI moments, and changes they suggest "
                   "unprompted. This gives a clearer read than waiting two weeks to see whether they "
                   "opened it.")
        model = _FakeModel([
            GeneratedItem("suggestion", first, .85),
            GeneratedItem("suggestion", clearer, .89),
        ])

        created = generate_items(self.mem, self.inf, self.store, cid, model, limit=3)

        self.assertEqual(created, 1)
        suggestions = [item for item in self.store.open_items(cid)
                       if item["kind"] == "suggestion"]
        self.assertEqual([item["text"] for item in suggestions], [clearer])

    def test_distinct_suggestions_are_still_limited_to_best_one(self):
        cid = self.store.add_curiosity("protect recovery time", "Recovery")
        model = _FakeModel([
            GeneratedItem("suggestion", "Block the first hour for creative work.", .91),
            GeneratedItem("suggestion", "Take a walk before opening work chat.", .89),
        ])

        created = generate_items(self.mem, self.inf, self.store, cid, model, limit=3)

        self.assertEqual(created, 1)
        suggestions = [item for item in self.store.open_items(cid)
                       if item["kind"] == "suggestion"]
        self.assertEqual([item["text"] for item in suggestions],
                         ["Block the first hour for creative work."])

    def test_open_suggestion_blocks_another_but_not_new_questions(self):
        cid = self.store.add_curiosity("protect recovery time", "Recovery")
        self.store.add_item(
            cid, "suggestion", "Keep the first hour for creative work.", confidence=.85)
        model = _FakeModel([
            GeneratedItem("suggestion", "Disable work notifications until lunch.", .99),
            GeneratedItem("question", "Which interruption changes your state most?", .90),
        ])

        created = generate_items(self.mem, self.inf, self.store, cid, model, limit=3)

        self.assertEqual(created, 1)
        items = self.store.open_items(cid)
        self.assertEqual(sum(item["kind"] == "suggestion" for item in items), 1)
        self.assertIn("Which interruption changes your state most?",
                      {item["text"] for item in items})

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

    def test_proactive_checkpoint_surfaces_a_revisable_suggestion_after_fifteen_answers(self):
        cid = self.store.add_curiosity("understand a recurring pattern", "pattern")
        for n in range(15):
            iid = self.store.add_item(cid, "question", f"answered {n}", confidence=0.9)
            self.store.mark_answered(iid, f"answer {n}", None)
        model = _FakeModel([
            GeneratedItem("question", "one more useful distinction", 0.92),
            GeneratedItem("suggestion", "try this safe and revisable experiment", 0.56),
        ])

        created = generate_items(self.mem, self.inf, self.store, cid, model, limit=2)

        self.assertEqual(created, 2)
        self.assertIn("try this safe and revisable experiment",
                      {item["text"] for item in self.store.open_items(cid)})

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

    def test_set_curiosity_from_journal_reuses_similar_active_topic(self):
        existing = self.store.add_curiosity(
            "Energy sensitivity around food", "Energy Sensitivity & Food")
        result = set_curiosity_from_journal(
            self.mem, self.inf, self.store,
            "Food seems to change my energy and I want to understand the pattern.",
            StubCuriosityModel(), label="Energy & Food Investigation")
        self.assertEqual(result["curiosity_id"], existing)
        self.assertTrue(result["reused"])
        self.assertEqual(len(self.store.list_curiosities("active")), 1)
        resolved = self.store.resolved(existing, limit=5)
        self.assertTrue(any("Food seems to change my energy" in item["answer"]
                            for item in resolved))

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

    def test_synthesis_versions_are_reviewed_and_preserved(self):
        cid = self.store.add_curiosity("understand my energy", "Energy")
        for number in range(2):
            item_id = self.store.add_item(
                cid, "question", f"question {number}", confidence=.9)
            self.store.mark_answered(item_id, f"answer {number}", None)

        readiness = self.store.synthesis_due(cid)
        self.assertTrue(readiness["due"])
        first = synthesize_curiosity(
            self.mem, self.inf, self.store, cid, StubCuriosityModel())
        self.assertEqual(first["version"], 1)
        self.assertEqual(first["status"], "draft")
        self.assertFalse(self.store.synthesis_due(cid)["due"])
        self.assertIsNone(self.store.latest_synthesis(cid, status="approved"))

        edited = dict(first["payload"])
        edited["interpretation"] = "Energy appears steadier after earlier fuel."
        approved = self.store.decide_synthesis(
            first["id"], "approve", payload=edited, note="This feels accurate")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["payload"]["interpretation"], edited["interpretation"])

        for number in range(2, 4):
            item_id = self.store.add_item(
                cid, "question", f"question {number}", confidence=.9)
            self.store.mark_answered(item_id, f"answer {number}", None)
        self.assertTrue(self.store.synthesis_due(cid)["due"])
        second = synthesize_curiosity(
            self.mem, self.inf, self.store, cid, StubCuriosityModel())
        self.assertEqual(second["version"], 2)
        self.store.decide_synthesis(second["id"], "approve")

        history = self.store.synthesis_history(cid)
        self.assertEqual([item["version"] for item in history], [2, 1])
        self.assertEqual([item["status"] for item in history],
                         ["approved", "approved"])
        self.assertEqual(history[1]["payload"]["interpretation"],
                         edited["interpretation"])

    def test_unparseable_synthesis_with_evidence_raises_instead_of_zero_draft(self):
        """A truncated/unparseable model reply must not become a misleading
        0%-confidence 'not enough evidence' draft when answers exist."""
        class _EmptyModel(StubCuriosityModel):
            def synthesize(self, curiosity, context, answered_items, previous):
                return {}

        cid = self.store.add_curiosity("understand a pattern", "Pattern")
        item_id = self.store.add_item(cid, "question", "a question", confidence=.9)
        self.store.mark_answered(item_id, "a real answer", None)
        with self.assertRaises(ValueError):
            synthesize_curiosity(
                self.mem, self.inf, self.store, cid, _EmptyModel())
        self.assertEqual(self.store.synthesis_history(cid), [])
        # With no answered evidence the honest low-confidence fallback remains.
        empty_cid = self.store.add_curiosity("another pattern", "Pattern 2")
        draft = synthesize_curiosity(
            self.mem, self.inf, self.store, empty_cid, _EmptyModel())
        self.assertEqual(draft["status"], "draft")
        self.assertIn("not enough evidence", draft["payload"]["interpretation"])

    def test_synthesis_can_lower_confidence_and_report_insufficient_evidence(self):
        cid = self.store.add_curiosity("understand a pattern", "Pattern")
        first = self.store.add_synthesis(cid, {
            "interpretation": "A tentative pattern.", "confidence": .8,
        })
        self.store.decide_synthesis(first["id"], "approve")
        second = self.store.add_synthesis(cid, {
            "interpretation": "The new evidence is insufficient to support the pattern.",
            "confidence": .25,
            "counterevidence": ["The pattern did not recur."],
            "unknowns": ["Which context matters?"],
        })
        approved = self.store.decide_synthesis(second["id"], "approve")
        self.assertLess(approved["payload"]["confidence"],
                        first["payload"]["confidence"])
        self.assertIn("insufficient", approved["payload"]["interpretation"])

    def test_synthesis_requires_decision_before_another_draft(self):
        cid = self.store.add_curiosity("understand a pattern", "Pattern")
        draft = self.store.add_synthesis(cid, {"interpretation": "Maybe."})
        with self.assertRaises(ValueError):
            self.store.add_synthesis(cid, {"interpretation": "Another."})
        self.store.decide_synthesis(draft["id"], "reject")
        next_draft = self.store.add_synthesis(cid, {"interpretation": "Revised."})
        self.assertEqual(next_draft["version"], 2)

    def test_approved_synthesis_drafts_person_updates_only_on_explicit_request(self):
        cid = self.store.add_curiosity("understand handoff energy", "Handoff energy")
        synthesis = self.store.add_synthesis(cid, {
            "interpretation": "Clear handoffs appear to help my energy stay steadier.",
            "confidence": .7,
            "supporting_evidence": [
                {"item_id": 1, "summary": "A clear plan reduced the dip."},
                {"item_id": 2, "summary": "An unclear transition preceded a crash."},
            ],
            "counterevidence": ["Travel days differ."],
        })
        self.store.decide_synthesis(synthesis["id"], "approve")
        self.assertEqual(self.inf.person_proposals(cid), [])

        proposals = reconcile_synthesis(
            self.inf, self.store, synthesis["id"], StubCuriosityModel())
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["status"], "open")
        self.assertEqual(self.inf.confirmed(), [])
        self.assertIsNotNone(self.inf.person_reconciliation_run(synthesis["id"]))
        again = reconcile_synthesis(
            self.inf, self.store, synthesis["id"], StubCuriosityModel())
        self.assertEqual([p["id"] for p in again], [p["id"] for p in proposals])

    def test_insufficient_synthesis_records_no_person_model_change(self):
        cid = self.store.add_curiosity("understand a pattern", "Pattern")
        synthesis = self.store.add_synthesis(cid, {
            "interpretation": "There is not enough evidence yet.",
            "confidence": .1,
        })
        self.store.decide_synthesis(synthesis["id"], "approve")
        proposals = reconcile_synthesis(
            self.inf, self.store, synthesis["id"], StubCuriosityModel())
        self.assertEqual(proposals, [])
        run = self.inf.person_reconciliation_run(synthesis["id"])
        self.assertEqual(run["result_count"], 0)

    def test_suggested_investigations_are_bounded_grounded_and_inert(self):
        cid = self.store.add_curiosity("understand handoff energy", "Handoff")
        synthesis = self.store.add_synthesis(cid, {
            "interpretation": "Preparation may protect energy.", "confidence": .7,
            "unknowns": ["Which handoffs benefit most?"],
        })
        self.store.decide_synthesis(synthesis["id"], "approve")
        good = _candidate_payload(
            evidence_refs=[f"synthesis:{synthesis['id']}", "invented:999"])
        model = _CandidateModel([good, _candidate_payload(
            "second", title="Second", question="What makes transitions feel voluntary?"),
            _candidate_payload("third", title="Third", question="A third candidate?",
                               relevance=.95, uncertainty=.95,
                               expected_usefulness=.95)])
        goals = GoalStore(os.path.join(self.tmp.name, "memory.db"))
        try:
            result = suggest_investigation_candidates(
                self.store, self.inf, goals, model)
        finally:
            goals.close()
        visible = result["candidates"]
        self.assertLessEqual(len(visible), 2)
        self.assertEqual(len(self.store.list_curiosities(status="active")), 1)
        self.assertEqual(result["routed"], 0)
        related = next(item for item in visible
                       if item["topic_key"] == "handoff-energy")
        self.assertEqual(related["payload"]["related_curiosity_id"], cid)
        self.assertEqual(related["payload"]["recommended_route"], "thread")
        self.assertTrue(related["payload"]["directions"])
        self.assertEqual(model.context["capacity"]["candidate_slots"], 2)

    def test_candidate_starts_only_after_explicit_action_and_respects_capacity(self):
        candidate = self.store.add_candidate(_candidate_payload())
        self.assertEqual(self.store.list_curiosities(status="active"), [])
        result = start_investigation_candidate(
            self.mem, self.inf, self.store, candidate["id"], StubCuriosityModel())
        self.assertEqual(self.store.candidate(candidate["id"])["status"], "started")
        self.assertEqual(result["curiosity_id"],
                         self.store.candidate(candidate["id"])["started_curiosity_id"])
        self.assertGreater(len(self.store.open_items(result["curiosity_id"])), 0)

        full_candidate = self.store.add_candidate(_candidate_payload("capacity-test"))
        for index in range(4):
            self.store.add_curiosity(f"active {index}", f"active {index}")
        reused = start_investigation_candidate(
            self.mem, self.inf, self.store, full_candidate["id"],
            StubCuriosityModel())
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["curiosity_id"], result["curiosity_id"])
        self.assertEqual(self.store.candidate(full_candidate["id"])["status"], "started")

    def test_related_investigations_can_merge_without_losing_history(self):
        target = self.store.add_curiosity(
            "understand energy sensitivity around meals", "Energy Sensitivity & Food")
        source = self.store.add_curiosity(
            "learn what food patterns cause energy crashes", "Energy & Food Investigation")
        item_id = self.store.add_item(source, "question", "When does the crash begin?")
        synthesis = self.store.add_synthesis(source, {
            "interpretation": "Meal timing may matter.", "confidence": .6})
        self.store.add_classification_context(source, "Older useful lead")
        groups = related_investigation_groups(self.store)
        self.assertEqual({member["id"] for member in groups[0]["members"]},
                         {target, source})

        merged = merge_investigations(self.store, target, [source])
        self.assertEqual(merged["archived_ids"], [source])
        self.assertEqual(self.store.get_curiosity(source)["status"], "archived")
        self.assertEqual(self.store.get_item(item_id)["curiosity_id"], target)
        self.assertEqual(self.store.get_synthesis(synthesis["id"])["curiosity_id"], target)
        notes = [row["note"] for row in self.store.classification_contexts(target, 20)]
        self.assertTrue(any("Older useful lead" in note for note in notes))
        self.assertTrue(any("Energy & Food Investigation" in note for note in notes))

    def test_candidate_direction_can_become_a_thread_without_new_investigation(self):
        parent = self.store.add_curiosity(
            "understand how Faerie Fire should grow", "Faerie Fire")
        candidate = self.store.add_candidate(_candidate_payload(
            "faerie-aesthetic", title="Explore Faerie Fire directions",
            question="Which direction matters next?", related_curiosity_id=parent,
            recommended_route="thread", directions=[{
                "title": "Aesthetic and identity",
                "question": "What should the voice and ambient UI feel like?",
                "rationale": "This is distinct from market validation.",
            }]))
        result = start_investigation_candidate(
            self.mem, self.inf, self.store, candidate["id"], StubCuriosityModel(),
            route="thread", direction_index=0)
        self.assertEqual(result["curiosity_id"], parent)
        self.assertEqual(result["route"], "thread")
        self.assertEqual(len(self.store.list_curiosities(status="active")), 1)
        thread = self.store.get_thread(result["thread_id"])
        self.assertEqual(thread["title"], "Aesthetic and identity")
        self.assertTrue(any(item["thread_id"] == thread["id"]
                            for item in self.store.items_for_curiosity(parent)))
        general = self.store.add_item(parent, "question", "A broad question")
        moved = self.store.assign_item_thread(general, thread["id"])
        self.assertEqual(moved["thread_id"], thread["id"])
        self.assertIsNone(self.store.assign_item_thread(general, None)["thread_id"])

    def test_sensitive_candidate_requires_explicit_permission(self):
        candidate = self.store.add_candidate(_candidate_payload(
            "sensitive-fear", sensitivity="sensitive",
            question="When does this fear feel safest to explore?"))
        with self.assertRaises(ValueError):
            start_investigation_candidate(
                self.mem, self.inf, self.store, candidate["id"],
                StubCuriosityModel())
        self.assertEqual(self.store.candidate(candidate["id"])["status"], "open")
        result = start_investigation_candidate(
            self.mem, self.inf, self.store, candidate["id"], StubCuriosityModel(),
            sensitive_permission=True)
        self.assertIn("curiosity_id", result)


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

    def test_answer_item_can_save_locally_without_a_model_round_trip(self):
        iid = self.store.add_item(self.cid, "question", "What happened first?")
        result = answer_item(self.mem, self.store, iid, "My shoulders tightened.", None)
        self.assertEqual(self.store.get_item(iid)["status"], "answered")
        self.assertEqual(crypto.dec(self.mem.get(result["resulting_memory_id"])["value"]),
                         "My shoulders tightened.")

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


class TestClassificationLeafHorizon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.cfg = SimpleNamespace(
            memory_db_path=self.db,
            goal_ai_leaf_horizon=2,
        )
        self.mem = MemoryStore(self.db)
        self.inf = InferenceStore(self.db)
        self.store = CuriosityStore(self.db)
        self.goals = GoalStore(self.db)
        self.project = self.goals.create("overgoal", "Upwork experiment")
        self.cid = self.store.add_curiosity(
            "Use this investigation to shape the Upwork experiment.",
            "Upwork learning",
        )

    def tearDown(self):
        self.goals.close()
        self.store.close()
        self.inf.close()
        self.mem.close()
        self.tmp.cleanup()

    def _leaf(self, title, description=""):
        return ClassificationProposal(
            "create_leaf",
            {"parent_id": self.project, "title": title,
             "description": description, "priority": "normal"},
            "The investigation now points to an actionable next step.",
        )

    def test_staging_rejects_title_overlap_and_reserves_the_two_leaf_horizon(self):
        self.goals.create(
            "task", "Draft Upwork profile", parent_id=self.project)
        model = _ClassificationModel([
            self._leaf("DRAFT Upwork profile!!"),
            self._leaf("Draft the Upwork profile"),
            self._leaf("Publish profile and first posting scan"),
            self._leaf("Track applications and outcomes"),
        ])

        result = classify_curiosity(
            self.cfg, self.mem, self.inf, self.store, self.cid, model)

        self.assertEqual(result["created"], 1)
        self.assertEqual(
            [row["payload"]["title"] for row in result["proposals"]],
            ["Publish profile and first posting scan"],
        )

    def test_staging_counts_a_command_center_leaf_card_as_reserved(self):
        self.goals.create(
            "task", "Draft Upwork profile", parent_id=self.project)
        chats = ChatStore(self.db)
        chat_id = chats.create("Upwork")
        chats.replace_pending_proposals(chat_id, [{
            "action": "create_leaf",
            "target_node_id": self.project,
            "label": "Publish profile and first posting scan",
            "directive": "Publish the profile, then scan the first postings.",
        }])

        result = classify_curiosity(
            self.cfg, self.mem, self.inf, self.store, self.cid,
            _ClassificationModel([self._leaf("Track applications and outcomes")]),
        )

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["proposals"], [])

    def test_approved_leaf_is_revalidated_and_records_origin_atomically(self):
        proposal_id = self.store.add_classification_proposal(
            self.cid, self._leaf("Draft Upwork profile"))

        result = decide_classification_proposal(
            self.cfg, proposal_id, "approve")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "approved")
        leaf = GoalStore(self.db)
        try:
            created = leaf.get(result["attached_goal_id"])
            self.assertEqual(created["title"], "Draft Upwork profile")
            self.assertEqual(created["origin"]["source_kind"], "investigation")
            self.assertEqual(created["origin"]["source_proposal_id"], proposal_id)
        finally:
            leaf.close()

    def test_stale_leaf_approval_dismisses_card_and_applies_nothing(self):
        proposal_id = self.store.add_classification_proposal(
            self.cid, self._leaf("Publish profile"))
        existing_id = self.goals.create(
            "task", "Publish profile", parent_id=self.project)

        result = decide_classification_proposal(
            self.cfg, proposal_id, "approve")

        self.assertFalse(result["ok"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["status"], "dismissed")
        self.assertIn("Nothing was applied", result["message"])
        self.assertEqual(
            self.store.get_classification_proposal(proposal_id)["status"],
            "dismissed",
        )
        leaves = self.goals.conn.execute(
            "SELECT id FROM goal_node WHERE parent_id=? AND node_type='task' "
            "AND status!='archived' ORDER BY id", (self.project,)
        ).fetchall()
        self.assertEqual([int(node["id"]) for node in leaves], [existing_id])


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
        self.assertIn("person_model_proposals", cur)
        self.assertIn("person_model_reconciled_synthesis_ids", cur)
        self.assertIn("investigation_candidates", state)
        self.assertEqual(state["archived"], [])

    def test_candidate_bridge_never_autostarts_and_honors_user_action(self):
        store = CuriosityStore(self.api.cfg.memory_db_path)
        try:
            candidate = store.add_candidate(_candidate_payload())
            self.assertEqual(store.list_curiosities(status="active"), [])
        finally:
            store.close()
        started = self.api.curiosity_candidate_action(candidate["id"], "start")
        self.assertTrue(started["ok"])
        state = self.api.curiosity_state()
        self.assertEqual(len(state["curiosities"]), 1)
        self.assertEqual(state["curiosities"][0]["label"], "Explore handoff energy")
        self.assertEqual(state["investigation_candidates"], [])

    def test_person_model_reconciliation_bridge_requires_two_explicit_approvals(self):
        created = self.api.curiosity_set("understand my handoff energy", label="Handoff")
        cid = created["curiosity_id"]
        state = self.api.curiosity_state()
        questions = state["curiosities"][0]["open_questions"]
        for index, item in enumerate(questions[:2]):
            self.assertTrue(self.api.curiosity_answer(
                item["id"], f"answer {index} about clear handoffs")["ok"])
        drafted = self.api.curiosity_synthesize(cid)
        self.assertTrue(drafted["ok"])
        synthesis = drafted["synthesis"]
        approved = self.api.curiosity_synthesis_decide(
            synthesis["id"], "approve", synthesis["payload"], "accurate")
        self.assertTrue(approved["ok"])
        inf = InferenceStore(self.api.cfg.memory_db_path)
        try:
            self.assertEqual(inf.confirmed(), [])
        finally:
            inf.close()

        reconciled = self.api.curiosity_person_reconcile(synthesis["id"])
        self.assertTrue(reconciled["ok"])
        self.assertEqual(len(reconciled["proposals"]), 1)
        proposal = reconciled["proposals"][0]
        inf = InferenceStore(self.api.cfg.memory_db_path)
        try:
            self.assertEqual(inf.confirmed(), [])
        finally:
            inf.close()
        applied = self.api.curiosity_person_proposal(
            proposal["id"], "approve", proposal["payload"], "fits me")
        self.assertTrue(applied["ok"])
        inf = InferenceStore(self.api.cfg.memory_db_path)
        try:
            self.assertEqual(len(inf.confirmed()), 1)
        finally:
            inf.close()

    def test_curiosity_answer_bridge(self):
        created = self.api.curiosity_set("help me get fit", label="fitness")
        state = self.api.curiosity_state()
        item_id = state["curiosities"][0]["open_questions"][0]["id"]
        result = self.api.curiosity_answer(item_id, "I go to the gym 3x a week")
        self.assertTrue(result["ok"])
        self.assertIn("resulting_memory_id", result)
        self.assertIn("curiosity_id", result)

    def test_curiosity_answer_bridge_does_not_run_cloud_sync_per_answer(self):
        created = self.api.curiosity_set("help me get fit", label="fitness")
        state = self.api.curiosity_state()
        item_id = state["curiosities"][0]["open_questions"][0]["id"]
        self.api._sync_curiosity_notion_quietly = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("answer save should not trigger a cloud summary"))

        result = self.api.curiosity_answer(item_id, "I go to the gym 3x a week")

        self.assertTrue(result["ok"])

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
