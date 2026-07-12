import json
import os
import tempfile
import unittest

from livingpc import crypto
from livingpc.config import Config
from livingpc.curiosity import CuriosityStore
from livingpc.goals import (
    GoalStore, StubGoalPlanner, continue_planning, record_experiment_outcome,
    start_planning, summarize_plan,
)
from livingpc.inference import InferenceStore
from livingpc.memory import MemoryStore


class GoalTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.curiosities = CuriosityStore(self.db)
        self.goals = GoalStore(self.db)

    def tearDown(self):
        self.goals.close()
        self.curiosities.close()
        self.tmp.cleanup()


class TestGoalGraph(GoalTestCase):
    def test_single_renameable_umbrella_is_reused(self):
        root = self.goals.get(self.goals.root_id)
        self.assertEqual(root["title"], "Actualized Self")
        self.goals.update(root["id"], title="Future me")
        other = GoalStore(self.db)
        try:
            self.assertEqual(other.root_id, root["id"])
            self.assertEqual(other.get(other.root_id)["title"], "Future me")
            count = other.conn.execute(
                "SELECT COUNT(*) FROM goal_node WHERE node_type='umbrella'").fetchone()[0]
            self.assertEqual(count, 1)
        finally:
            other.close()

    def test_private_goal_text_uses_crypto_storage(self):
        goal_id = self.goals.create(
            "overgoal", "Fluent in Korean", description="private description",
            notes="private notes")
        raw = self.goals.conn.execute(
            "SELECT title,description,notes FROM goal_node WHERE id=?", (goal_id,)).fetchone()
        if crypto.enabled():
            self.assertTrue(all(crypto.is_encrypted(value) for value in raw))
        self.assertEqual(self.goals.get(goal_id)["description"], "private description")

    def test_typed_recursive_tree_and_cycle_prevention(self):
        over = self.goals.create("overgoal", "Korean")
        sub = self.goals.create("subgoal", "Grammar", parent_id=over)
        nested = self.goals.create("subgoal", "Particles", parent_id=sub)
        task = self.goals.create("task", "Practice 은/는", parent_id=nested)
        self.assertEqual(self.goals.get(task)["parent_id"], nested)
        with self.assertRaises(ValueError):
            self.goals.create("overgoal", "Wrong", parent_id=sub)
        with self.assertRaises(ValueError):
            self.goals.move(sub, nested)
        with self.assertRaises(ValueError):
            self.goals.move(task, task)

    def test_move_reorders_siblings_deterministically(self):
        over = self.goals.create("overgoal", "Korean")
        first = self.goals.create("task", "First", parent_id=over)
        second = self.goals.create("task", "Second", parent_id=over)
        self.goals.move(second, over, 0)
        parent = next(x for x in self.goals.tree()["children"] if x["id"] == over)
        self.assertEqual([x["id"] for x in parent["children"]], [second, first])

    def test_completion_excludes_paused_and_archived_tasks(self):
        over = self.goals.create("overgoal", "Korean")
        complete = self.goals.create("task", "Done", parent_id=over, status="completed")
        self.goals.create("task", "Open", parent_id=over)
        self.goals.create("task", "Paused", parent_id=over, status="paused")
        self.goals.create("task", "Archived", parent_id=over, status="archived")
        node = next(x for x in self.goals.tree()["children"] if x["id"] == over)
        self.assertEqual(node["completion"], {"done": 1, "total": 2, "percent": 50.0})
        self.assertIsNotNone(self.goals.get(complete)["completed_at"])

    def test_empty_goal_completion_is_unknown(self):
        over = self.goals.create("overgoal", "Mental health")
        node = next(x for x in self.goals.tree()["children"] if x["id"] == over)
        self.assertEqual(node["completion"], {"done": 0, "total": 0, "percent": None})

    def test_curiosity_and_evidence_links_round_trip(self):
        curiosity = self.curiosities.add_curiosity("learn Korean", "Korean research")
        over = self.goals.create("overgoal", "Korean")
        self.goals.link_curiosity(over, curiosity)
        self.goals.add_evidence(over, "manual_note", "note-1", "Passed a practice test")
        node = next(x for x in self.goals.tree()["children"] if x["id"] == over)
        self.assertEqual(node["curiosities"][0]["label"], "Korean research")
        self.assertEqual(node["evidence"][0]["label"], "Passed a practice test")

    def test_origin_round_trips_through_get_and_tree(self):
        over = self.goals.create("overgoal", "Mental Health")
        self.goals.set_origin(
            over,
            source_kind="investigation",
            source_id=42,
            source_proposal_id=7,
            source_label="Social dread",
            summary="Created from Investigation “Social dread”.",
            detail="Original question and answered evidence.",
        )
        node = self.goals.get(over)
        self.assertEqual(node["origin"]["source_kind"], "investigation")
        self.assertEqual(node["origin"]["source_id"], "42")
        self.assertEqual(node["origin"]["source_proposal_id"], 7)
        self.assertEqual(node["origin"]["source_label"], "Social dread")
        self.assertIn("Created from Investigation", node["origin"]["summary"])
        tree_node = next(x for x in self.goals.tree()["children"] if x["id"] == over)
        self.assertEqual(tree_node["origin"]["detail"], "Original question and answered evidence.")

    def test_backfills_missing_origin_from_existing_tree_and_curiosity(self):
        curiosity = self.curiosities.add_curiosity(
            "Why do I dread meeting new people?", "Social Dread")
        item = self.curiosities.add_item(curiosity, "question", "What happens first?")
        self.curiosities.conn.execute(
            "UPDATE curiosity_item SET status='answered',answer=? WHERE id=?",
            (crypto.enc("I calculate whether the event will be draining."), item))
        self.curiosities.conn.commit()
        root = self.goals.create(
            "overgoal", "Mental Health",
            description="Understand and reduce anxiety patterns.")
        branch = self.goals.create(
            "subgoal", "Social Energy", parent_id=root,
            description="Track the pre-event dread pattern.")
        self.goals.link_curiosity(branch, curiosity)

        count = self.goals.backfill_missing_origins()
        self.assertGreaterEqual(count, 2)
        branch_node = self.goals.get(branch)
        self.assertEqual(branch_node["origin"]["source_kind"], "backfill")
        self.assertIn("Existing Branch", branch_node["origin"]["summary"])
        self.assertIn("Social Dread", branch_node["origin"]["summary"])
        self.assertIn("What happens first?", branch_node["origin"]["detail"])

    def test_matching_root_auto_attaches_once_and_descendants_inherit(self):
        curiosity = self.curiosities.add_curiosity(
            "help me understand and maintain my mental health", "Mental Health")
        root = self.goals.create("overgoal", "Mental Health")
        branch = self.goals.create("subgoal", "Work anxiety", parent_id=root)
        leaf = self.goals.create("task", "Prepare for meeting", parent_id=branch)
        tree = self.goals.tree()
        root_node = next(x for x in tree["children"] if x["id"] == root)
        branch_node = root_node["children"][0]
        leaf_node = branch_node["children"][0]
        self.assertEqual(root_node["curiosities"][0]["id"], curiosity)
        self.assertIsNone(root_node["curiosities"][0]["inherited_from_id"])
        self.assertEqual(branch_node["curiosities"][0]["inherited_from_id"], root)
        self.assertEqual(leaf_node["curiosities"][0]["inherited_from_title"],
                         "Mental Health")
        count = self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_curiosity_link WHERE curiosity_id=?", (curiosity,)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_root_auto_attach_requires_one_unambiguous_exact_name(self):
        self.curiosities.add_curiosity("one", "Mental Health")
        self.curiosities.add_curiosity("two", "Mental Health")
        root = self.goals.create("overgoal", "Mental Health")
        node = next(x for x in self.goals.tree()["children"] if x["id"] == root)
        self.assertEqual(node["curiosities"], [])

    def test_forgetting_memory_removes_goal_evidence_link(self):
        from livingpc.forget import forget_memory
        mem = MemoryStore(self.db)
        memory_id = mem.add("Korean", "test", "I passed")
        mem.close()
        over = self.goals.create("overgoal", "Korean")
        self.goals.add_evidence(over, "memory", str(memory_id), "Passed")
        cfg = Config()
        cfg.memory_db_path = self.db
        cfg.db_path = os.path.join(self.tmp.name, "events.db")
        cfg.notion_sync_enabled = False
        result = forget_memory(cfg, memory_id, purge_backups=False, sync_notion=False)
        self.assertEqual(result["goal_evidence_removed"], 1)
        self.assertEqual(self.goals.tree()["children"][0]["evidence"], [])


class TestGoalMastery(GoalTestCase):
    def test_mastery_is_opt_in_and_not_changed_by_task_completion(self):
        over = self.goals.create("overgoal", "Korean")
        task = self.goals.create("task", "Study", parent_id=over)
        self.goals.update(task, status="completed")
        self.assertIsNone(self.goals.mastery(over))
        profile = self.goals.enable_mastery(over, ["Recall", "Application"])
        self.assertIsNone(profile["scores"]["recall"]["mastery"])
        self.goals.record_mastery(over, "recall", 80, .8, "assessment", "quiz-1")
        profile = self.goals.mastery(over)
        self.assertEqual(profile["scores"]["recall"]["mastery"], 80.0)
        self.assertIsNone(profile["scores"]["application"]["mastery"])

    def test_parent_does_not_average_child_mastery(self):
        over = self.goals.create("overgoal", "Korean")
        sub = self.goals.create("subgoal", "Grammar", parent_id=over)
        self.goals.enable_mastery(sub, ["Accuracy"])
        self.goals.record_mastery(sub, "accuracy", 90, .9, "assessment", "one")
        tree = self.goals.tree()
        parent = next(x for x in tree["children"] if x["id"] == over)
        self.assertIsNone(parent["mastery"])
        self.assertEqual(parent["children"][0]["mastery"]["scores"]["accuracy"]["mastery"], 90)

    def test_existing_curiosity_profiles_are_mirrored_without_mutation(self):
        cid = self.curiosities.add_curiosity("learn Korean", "Korean")
        self.goals.conn.execute(
            "CREATE TABLE IF NOT EXISTS curiosity_metric_profile ("
            "curiosity_id INTEGER PRIMARY KEY,status TEXT,dimensions_json TEXT,"
            "created_at TEXT,approved_at TEXT)")
        self.goals.conn.execute(
            "INSERT INTO curiosity_metric_profile VALUES (?,?,?,?,?)",
            (cid, "approved", json.dumps([{"slug": "recall", "label": "Recall"}]),
             "2026-01-01", "2026-01-02"))
        self.goals.conn.commit()
        reopened = GoalStore(self.db)
        try:
            row = reopened.conn.execute(
                "SELECT status FROM mastery_subject_profile "
                "WHERE subject_type='curiosity' AND subject_id=?", (cid,)).fetchone()
            self.assertEqual(row["status"], "approved")
            original = reopened.conn.execute(
                "SELECT status FROM curiosity_metric_profile WHERE curiosity_id=?", (cid,)).fetchone()
            self.assertEqual(original["status"], "approved")
        finally:
            reopened.close()


class TestGoalPlanner(GoalTestCase):
    def _suggestion(self):
        cid = self.curiosities.add_curiosity("learn Korean", "Korean")
        return self.curiosities.add_item(cid, "suggestion", "Master one grammar pattern")

    def test_planner_persists_dialogue_and_only_commits_after_summary(self):
        item = self._suggestion()
        planner = StubGoalPlanner()
        session = start_planning(self.goals, planner, item)
        self.assertEqual(session["status"], "active")
        with self.assertRaises(ValueError):
            self.goals.commit_plan(session["id"])
        session = continue_planning(
            self.goals, planner, session["id"], "Use it correctly in five sentences")
        self.assertEqual([m["role"] for m in session["messages"]],
                         ["assistant", "user", "assistant"])
        session = summarize_plan(self.goals, planner, session["id"])
        self.assertEqual(session["status"], "ready")
        result = self.goals.commit_plan(session["id"])
        self.assertFalse(result["already_implemented"])
        again = self.goals.commit_plan(session["id"])
        self.assertTrue(again["already_implemented"])
        item_data = self.curiosities._item_dict(self.curiosities.get_item(item))
        self.assertEqual(item_data["status"], "implemented")
        self.assertEqual(item_data["implementation_goal_id"], result["goal_id"])

    def test_abandon_leaves_suggestion_open(self):
        item = self._suggestion()
        session = start_planning(self.goals, StubGoalPlanner(), item)
        self.goals.abandon_plan(session["id"])
        item_data = self.curiosities._item_dict(self.curiosities.get_item(item))
        self.assertEqual(item_data["status"], "open")
        self.assertIsNone(item_data["implementation_session_id"])

    def test_planner_defaults_to_curiositys_attached_goal(self):
        item = self._suggestion()
        curiosity_id = self.curiosities.get_item(item)["curiosity_id"]
        over = self.goals.create("overgoal", "Korean")
        self.goals.link_curiosity(over, curiosity_id)
        session = start_planning(self.goals, StubGoalPlanner(), item)
        self.assertEqual(session["target_parent_id"], over)

    def test_invalid_draft_rolls_back_entire_commit(self):
        item = self._suggestion()
        session = start_planning(self.goals, StubGoalPlanner(), item)
        bad = {"nodes": [{"type": "overgoal", "title": "Valid parent", "children": [
            {"type": "task", "title": "", "children": []}
        ]}]}
        self.goals.set_plan_draft(session["id"], bad, summary="bad", ready=True)
        with self.assertRaises(ValueError):
            self.goals.commit_plan(session["id"])
        count = self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_node WHERE node_type!='umbrella'").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.goals.plan_session(session["id"])["status"], "ready")

    def test_commit_coerces_model_draft_types_onto_valid_placements(self):
        """A real model draft used type='umbrella' at top level with tasks and a
        nested overgoal under it; commits must coerce instead of erroring."""
        item = self._suggestion()
        session = start_planning(self.goals, StubGoalPlanner(), item)
        draft = {"nodes": [{
            "type": "umbrella", "title": "Run micro-test", "children": [
                {"type": "task", "title": "Brainstorm session", "children": []},
                {"type": "overgoal", "title": "Evaluate and select", "children": [
                    {"type": "task", "title": "Shortlist tasks", "children": []},
                ]},
                {"type": "goal", "title": "Unknown typed leaf", "children": []},
            ],
        }]}
        self.goals.set_plan_draft(session["id"], draft, summary="plan", ready=True)
        result = self.goals.commit_plan(session["id"])
        top = self.goals.get(result["goal_id"])
        self.assertEqual(top["type"], "overgoal")
        rows = self.goals.conn.execute(
            "SELECT node_type FROM goal_node WHERE parent_id=?",
            (result["goal_id"],)).fetchall()
        self.assertEqual(sorted(r["node_type"] for r in rows),
                         ["subgoal", "task", "task"])


class TestExperimentOutcomes(GoalTestCase):
    def _leaf(self):
        over = self.goals.create("overgoal", "Energy")
        return over, self.goals.create("task", "Try a prepared handoff", parent_id=over)

    def test_completed_attempted_avoided_and_abandoned_all_produce_learning(self):
        _, leaf = self._leaf()
        for index, result in enumerate(("completed", "attempted", "avoided", "abandoned")):
            if index:
                leaf = self.goals.create(
                    "task", f"Experiment {result}",
                    parent_id=self.goals.get(leaf)["parent_id"])
            outcome = self.goals.add_outcome(
                leaf, result, f"What happened for {result}",
                expected_obstacle="I expected friction", surprise="The timing mattered",
                helpfulness=5, changed_understanding=f"Learning from {result}",
                next_adjustment=f"Adjust after {result}")
            self.assertEqual(outcome["result"], result)
            self.assertEqual(len(self.goals.outcomes(leaf)), 1)
            expected = "completed" if result == "completed" else "active"
            self.assertEqual(self.goals.get(leaf)["status"], expected)
            evidence = self.goals.conn.execute(
                "SELECT label FROM goal_evidence_link WHERE goal_id=? AND source_kind='experiment_outcome'",
                (leaf,)).fetchone()
            self.assertIn(f"Learning from {result}", crypto.dec(evidence["label"]))
            raw = self.goals.conn.execute(
                "SELECT what_happened,expected_obstacle,surprise,changed_understanding,"
                "next_adjustment FROM experiment_outcome WHERE id=?",
                (outcome["id"],)).fetchone()
            if crypto.enabled():
                self.assertTrue(all(crypto.is_encrypted(value) for value in raw))

    def test_outcome_infers_investigation_link_from_implemented_suggestion(self):
        cid = self.curiosities.add_curiosity("understand energy", "Energy")
        item = self.curiosities.add_item(cid, "suggestion", "Prepare the handoff")
        over, leaf = self._leaf()
        self.goals.conn.execute(
            "UPDATE curiosity_item SET implementation_goal_id=? WHERE id=?", (leaf, item))
        self.goals.conn.commit()
        outcome = self.goals.add_outcome(
            leaf, "attempted", "I tried it once", helpfulness=6)
        self.assertEqual(outcome["curiosity_id"], cid)
        self.assertEqual(outcome["source_item_id"], item)

    def test_failed_advice_creates_lower_confidence_synthesis_draft(self):
        cfg = Config(memory_db_path=self.db, db_path=os.path.join(self.tmp.name, "events.db"),
                     goal_ai_backend="stub", inference_backend="stub")
        cid = self.curiosities.add_curiosity("understand handoff energy", "Handoff")
        over, leaf = self._leaf()
        self.goals.link_curiosity(over, cid)
        synthesis = self.curiosities.add_synthesis(cid, {
            "interpretation": "Preparing every handoff should prevent the dip.",
            "confidence": .8,
            "supporting_evidence": [{"item_id": 1, "summary": "One good day"}],
            "counterevidence": [], "experiments": ["Prepare the handoff"],
        })
        self.curiosities.decide_synthesis(synthesis["id"], "approve")
        inf = InferenceStore(self.db)
        belief_id = inf.add_candidate(
            "handoff energy", "Prepared handoffs protect my energy", confidence=.9)
        inf.confirm(belief_id); inf.close()
        from livingpc.goal_ai import GoalAgentStore, build_agent_context
        agents = GoalAgentStore(self.db)
        try:
            result = record_experiment_outcome(cfg, leaf, {
                "result": "attempted", "what_happened": "The preparation added pressure.",
                "expected_obstacle": "Forgetting the plan",
                "surprise": "Planning itself was draining", "helpfulness": 2,
                "changed_understanding": "Preparation only helps when it stays lightweight.",
                "next_adjustment": "Try a one-line handoff cue instead.",
            })
            self.assertTrue(result["synthesis_drafted"])
            memories = MemoryStore(self.db)
            try:
                memory = next(item for item in memories.active_as_dicts()
                              if item["id"] == result["memory_id"])
                self.assertIn("Preparation only helps", memory["value"])
            finally:
                memories.close()
            draft = self.curiosities.latest_synthesis(cid, status="draft")
            approved = self.curiosities.latest_synthesis(cid, status="approved")
            self.assertLess(draft["payload"]["confidence"],
                            approved["payload"]["confidence"])
            self.assertEqual(draft["based_on_outcome_id"], result["outcome"]["id"])
            self.assertTrue(any("2/10 helpful" in item
                                for item in draft["payload"]["counterevidence"]))
            self.assertEqual(agents.state(leaf)["dirty"], True)
            context = build_agent_context(self.goals, agents, leaf)
            self.assertIn("one-line handoff cue", json.dumps(context))
            next_proposal = agents.get_proposal(result["next_proposal_id"])
            self.assertEqual(next_proposal["type"], "create_child")
            self.assertIn("one-line handoff cue", next_proposal["payload"]["title"])
            self.assertFalse(any(
                "one-line handoff cue" in node["title"].lower()
                for node in self.goals.catalog()))
        finally:
            agents.close()
        inf = InferenceStore(self.db)
        try:
            self.assertGreater(inf.evidence_episode_count("handoff energy"), 0)
        finally:
            inf.close()
        tree_leaf = next(node for node in self.goals.tree()["children"]
                         if node["id"] == over)["children"][0]
        self.assertEqual(tree_leaf["outcomes"][0]["next_adjustment"],
                         "Try a one-line handoff cue instead.")

    def test_helpful_outcome_marks_synthesis_ready_without_silent_model_call(self):
        cfg = Config(memory_db_path=self.db, db_path=os.path.join(self.tmp.name, "events.db"),
                     goal_ai_backend="stub", inference_backend="stub")
        cid = self.curiosities.add_curiosity("understand energy", "Energy")
        over, leaf = self._leaf(); self.goals.link_curiosity(over, cid)
        synthesis = self.curiosities.add_synthesis(cid, {
            "interpretation": "A small cue may help.", "confidence": .6})
        self.curiosities.decide_synthesis(synthesis["id"], "approve")
        result = record_experiment_outcome(cfg, leaf, {
            "result": "completed", "what_happened": "The cue helped.",
            "helpfulness": 8, "changed_understanding": "Smaller preparation works better."})
        self.assertFalse(result["synthesis_drafted"])
        readiness = self.curiosities.synthesis_due(cid)
        self.assertTrue(readiness["due"])
        self.assertEqual(readiness["new_outcomes"], 1)
        self.assertIsNone(self.curiosities.latest_synthesis(cid, status="draft"))


class TestGoalBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.memory_db_path = os.path.join(self.tmp.name, "memory.db")
        cfg.db_path = os.path.join(self.tmp.name, "events.db")
        cfg.curiosity_backend = "stub"
        from gui import GuiApi
        self.api = GuiApi(cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manual_tree_bridge(self):
        state = self.api.goal_state()
        self.assertTrue(state["ok"])
        root = state["tree"]["id"]
        made = self.api.goal_create("overgoal", "Korean", root)
        self.assertTrue(made["ok"])
        task = self.api.goal_create("task", "Review vocabulary", made["goal_id"])
        self.assertTrue(task["ok"])
        changed = self.api.goal_update(task["goal_id"], {"status": "completed"})
        self.assertTrue(changed["ok"])
        over = next(x for x in changed["tree"]["children"] if x["id"] == made["goal_id"])
        self.assertEqual(over["completion"]["percent"], 100.0)

    def test_experiment_outcome_bridge_updates_leaf_and_preserves_learning(self):
        state = self.api.goal_state(); root = state["tree"]["id"]
        over = self.api.goal_create("overgoal", "Energy", root)["goal_id"]
        leaf = self.api.goal_create("task", "Try a smaller handoff", over)["goal_id"]
        saved = self.api.goal_experiment_outcome(leaf, {
            "result": "completed", "what_happened": "The smaller cue was easier.",
            "helpfulness": 8, "surprise": "Less planning felt safer.",
            "changed_understanding": "Lightweight preparation works better.",
            "next_adjustment": "Test the cue before a social transition.",
        })
        self.assertTrue(saved["ok"])
        state = self.api.goal_state()
        node = next(child for parent in state["tree"]["children"]
                    for child in parent.get("children", []) if child["id"] == leaf)
        self.assertEqual(node["status"], "completed")
        self.assertEqual(node["outcomes"][0]["next_adjustment"],
                         "Test the cue before a social transition.")

    def test_implement_bridge_flow(self):
        curiosity = CuriosityStore(self.api.cfg.memory_db_path)
        cid = curiosity.add_curiosity("learn", "learning")
        item = curiosity.add_item(cid, "suggestion", "Practice one concept")
        curiosity.close()
        started = self.api.goal_plan_start(item)
        self.assertTrue(started["ok"])
        session_id = started["session"]["id"]
        replied = self.api.goal_plan_reply(session_id, "Explain it without notes")
        self.assertTrue(replied["ok"])
        summarized = self.api.goal_plan_summarize(session_id)
        self.assertEqual(summarized["session"]["status"], "ready")
        committed = self.api.goal_plan_commit(session_id)
        self.assertTrue(committed["ok"])


class TestGoalNotionExport(GoalTestCase):
    def test_explicit_export_omits_notes_and_archived_children(self):
        from livingpc.notion_sync import export_goal_to_notion

        over = self.goals.create("overgoal", "Korean", notes="never upload this")
        self.goals.create("task", "Practice aloud", parent_id=over, status="completed")
        self.goals.create("task", "Old secret task", parent_id=over, status="archived")

        class FakeClient:
            def __init__(self):
                self.calls = []

            def create_page(self, parent, title, blocks):
                self.calls.append((parent, title, blocks))
                return "page-1"

        cfg = Config()
        cfg.notion_sync_enabled = True
        cfg.notion_api_key = "token"
        cfg.notion_parent_page_id = "parent"
        client = FakeClient()
        result = export_goal_to_notion(cfg, self.goals, over, client=client)
        self.assertTrue(result["ok"])
        payload = json.dumps(client.calls)
        self.assertIn("Practice aloud", payload)
        self.assertNotIn("never upload this", payload)
        self.assertNotIn("Old secret task", payload)

    def test_export_requires_explicit_configuration(self):
        from livingpc.notion_sync import export_goal_to_notion
        over = self.goals.create("overgoal", "Korean")
        cfg = Config()
        cfg.notion_api_key = ""
        self.assertFalse(export_goal_to_notion(cfg, self.goals, over)["ok"])


if __name__ == "__main__":
    unittest.main()
