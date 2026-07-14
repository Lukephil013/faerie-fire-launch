import json
import os
import tempfile
import unittest

from livingpc import crypto
from livingpc.config import Config
from livingpc.curiosity import CuriosityStore
from livingpc.goals import (
    GoalStore, StubGoalPlanner, continue_planning, record_experiment_outcome,
    propose_goal_intake,
    propose_goal_tree_restructure, propose_suggestion_leaf_update,
    recommend_goal_intake, recommend_goal_restructure, recommend_goal_tree_restructure,
    recommend_suggestion_placement, start_planning,
    suggestion_leaf_overlaps, summarize_plan,
)
from livingpc.goals import TREE_RESTRUCTURE_SYSTEM
from livingpc.goal_ai import decide_proposal
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
    def test_tree_restructure_prompt_unpacks_generic_catch_all_roots(self):
        self.assertIn("generic catch-all Root", TREE_RESTRUCTURE_SYSTEM)
        self.assertIn("move its descendants", TREE_RESTRUCTURE_SYSTEM)
        self.assertIn("archive it after", TREE_RESTRUCTURE_SYSTEM)

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

    def test_restructure_preview_and_apply_preserve_identity_and_attached_data(self):
        life = self.goals.create("overgoal", "Luke's Life")
        career = self.goals.create("subgoal", "Career & Building Things", parent_id=life)
        project = self.goals.create("overgoal", "Run Upwork automation micro-test")
        branch = self.goals.create("subgoal", "Evaluate and validate", parent_id=project)
        leaf = self.goals.create("task", "Post listing", parent_id=project, status="completed")
        cid = self.curiosities.add_curiosity("test freelance automation", "Upwork experiment")
        self.goals.link_curiosity(project, cid)
        self.goals.add_evidence(leaf, "manual_note", "proof-1", "Listing was drafted.")

        preview = self.goals.restructure_preview(project, "subgoal", career)
        applied = self.goals.restructure(project, "subgoal", career,
                                         rationale="Career owns this experiment.")

        self.assertTrue(preview["node_id_preserved"])
        self.assertEqual(preview["retained_counts"]["nodes"], 3)
        self.assertEqual(preview["retained_counts"]["investigation_links"], 1)
        self.assertEqual(preview["retained_counts"]["evidence_links"], 1)
        self.assertEqual(preview["retained_counts"]["completed_nodes"], 1)
        self.assertEqual(applied["goal_id"], project)
        self.assertEqual(self.goals.get(project)["type"], "subgoal")
        self.assertEqual(self.goals.get(project)["parent_id"], career)
        self.assertEqual(self.goals.get(branch)["parent_id"], project)
        self.assertEqual(self.goals.get(leaf)["parent_id"], project)
        self.assertEqual(self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_restructure_history WHERE goal_id=?", (project,)
        ).fetchone()[0], 1)

    def test_invalid_restructure_rolls_back_without_partial_move(self):
        root = self.goals.create("overgoal", "Career")
        branch = self.goals.create("subgoal", "Client experiment", parent_id=root)
        self.goals.create("task", "Post listing", parent_id=branch)
        before = self.goals.get(branch)

        with self.assertRaisesRegex(ValueError, "cannot contain"):
            self.goals.restructure(branch, "task", root)

        after = self.goals.get(branch)
        self.assertEqual((after["type"], after["parent_id"]),
                         (before["type"], before["parent_id"]))
        self.assertEqual(self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_restructure_history WHERE goal_id=?", (branch,)
        ).fetchone()[0], 0)

    def test_manual_restructure_persists_area_project_and_stage_roles(self):
        root = self.goals.create("overgoal", "Work & Contribution")
        branch = self.goals.create("subgoal", "Client work", parent_id=root)

        preview = self.goals.restructure_preview(
            branch, "subgoal", root, 0, "area")
        applied = self.goals.restructure(
            branch, "subgoal", root, 0, semantic_role="area",
            rationale="This is an ongoing responsibility.")

        self.assertEqual(preview["proposed"]["type_label"], "Area")
        self.assertEqual(preview["proposed"]["semantic_role"], "area")
        self.assertEqual(applied["goal_id"], branch)
        self.assertEqual(self.goals.semantic_role(branch)["role"], "area")
        rendered = next(node for node in self.goals.tree()["children"]
                        if node["id"] == root)["children"][0]
        self.assertEqual((rendered["type"], rendered["semantic_role"]),
                         ("subgoal", "area"))

    def test_nested_stage_requires_explicit_macro_stage_justification(self):
        root = self.goals.create("overgoal", "Work & Contribution")
        project = self.goals.create("subgoal", "Client launch", parent_id=root)
        outer = self.goals.create("subgoal", "Delivery phase", parent_id=project)
        inner = self.goals.create("subgoal", "Quality review", parent_id=outer)
        self.goals._set_semantic_role(project, "project", rationale="Finite launch project.")
        self.goals._set_semantic_role(outer, "stage", rationale="Delivery is one project phase.")

        with self.assertRaisesRegex(ValueError, "macro-stage/substage"):
            self.goals.restructure_preview(
                inner, "subgoal", outer, semantic_role="stage")

        explanation = (
            "Delivery is the macro-stage; quality review is its separately owned substage.")
        preview = self.goals.restructure_preview(
            inner, "subgoal", outer, semantic_role="stage",
            nested_stage_justification=explanation)
        applied = self.goals.restructure(
            inner, "subgoal", outer, semantic_role="stage", rationale=explanation)

        self.assertEqual(preview["proposed"]["semantic_role"], "stage")
        self.assertEqual(applied["goal_id"], inner)
        self.assertEqual(self.goals.semantic_role(inner)["role"], "stage")
        self.assertIn("macro-stage", self.goals.semantic_role(inner)["rationale"])

    def test_ai_restructure_recommends_temporary_upwork_root_under_career(self):
        life = self.goals.create("overgoal", "Luke's Life")
        career = self.goals.create(
            "subgoal", "Career & Building Things", parent_id=life,
            description="Work, clients, products, and professional experiments.")
        project = self.goals.create(
            "overgoal", "Run Upwork automation micro-test",
            description="Post one automation service to test freelance client demand.")
        self.goals.create("task", "Post listing", parent_id=project)
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db,
                     curiosity_backend="stub")

        result = recommend_goal_restructure(cfg, StubGoalPlanner(), project)

        self.assertEqual(result["action"], "restructure")
        self.assertEqual(result["recommendation"]["new_type"], "subgoal")
        self.assertEqual(result["recommendation"]["parent_id"], career)
        self.assertIn("Career & Building Things", result["recommendation"]["preview"]["proposed"]["path"])

    def test_nested_branches_render_as_area_project_and_stage(self):
        life = self.goals.create("overgoal", "Luke's Life")
        career = self.goals.create("subgoal", "Career & Building Things", parent_id=life)
        project = self.goals.create("subgoal", "Upwork automation experiment", parent_id=career)
        stage = self.goals.create("subgoal", "Evaluate and validate", parent_id=project)
        self.goals.create("task", "Choose one offer", parent_id=stage)

        tree = self.goals.tree()
        life_node = next(node for node in tree["children"] if node["id"] == life)
        career_node = next(node for node in life_node["children"] if node["id"] == career)
        project_node = next(node for node in career_node["children"] if node["id"] == project)
        stage_node = next(node for node in project_node["children"] if node["id"] == stage)

        self.assertEqual(career_node["semantic_role"], "area")
        self.assertEqual(project_node["semantic_role"], "project")
        self.assertEqual(stage_node["semantic_role"], "stage")
        self.assertTrue(all(node["semantic_role_source"] == "derived"
                            for node in [career_node, project_node, stage_node]))

    def test_legacy_root_children_use_meaning_not_child_count_for_role(self):
        life = self.goals.create("overgoal", "Luke's Life")
        korean = self.goals.create("subgoal", "Korean Language", parent_id=life)
        league = self.goals.create("subgoal", "League of Legends", parent_id=life)
        ambient = self.goals.create("subgoal", "Ambient Interface", parent_id=life)

        life_node = next(node for node in self.goals.tree()["children"] if node["id"] == life)
        roles = {node["id"]: node["semantic_role"] for node in life_node["children"]}

        self.assertEqual(roles[korean], "area")
        self.assertEqual(roles[league], "area")
        self.assertEqual(roles[ambient], "project")

    def test_optional_starter_roots_are_explicit_bilingual_and_idempotent(self):
        catalog = self.goals.starter_root_catalog("ko")
        self.assertEqual(len(catalog), 7)
        self.assertTrue(all(not item["active"] for item in catalog))
        self.assertEqual(next(item for item in catalog if item["key"] == "health")["title"],
                         "건강과 에너지")

        made = self.goals.apply_starter_roots(["work", "health"], "en")
        repeated = self.goals.apply_starter_roots(["work", "health"], "en")

        self.assertEqual(len(made["created_goal_ids"]), 2)
        self.assertEqual(repeated["created_goal_ids"], [])
        roots = [node for node in self.goals.tree()["children"] if node["type"] == "overgoal"]
        self.assertEqual({node["title"] for node in roots},
                         {"Work & Contribution", "Health & Energy"})
        origins = self.goals.conn.execute(
            "SELECT source_kind,source_id FROM goal_origin WHERE goal_id IN (?,?)",
            tuple(made["created_goal_ids"])).fetchall()
        self.assertEqual({(row["source_kind"], row["source_id"]) for row in origins},
                         {("starter_root", "work"), ("starter_root", "health")})

    def test_plain_language_intake_classifies_then_waits_for_approval(self):
        career = self.goals.create(
            "overgoal", "Work & Contribution",
            description="Career, client work, products, and professional experiments.")
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db,
                     curiosity_backend="stub")

        classified = recommend_goal_intake(
            cfg, StubGoalPlanner(), career,
            "Build a small Upwork automation project to test client demand")
        recommendation = classified["recommendation"]
        staged = propose_goal_intake(cfg, recommendation, classified["rationale"])

        self.assertEqual(classified["action"], "propose")
        self.assertEqual((recommendation["new_type"], recommendation["semantic_role"]),
                         ("subgoal", "project"))
        self.assertEqual(self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_node WHERE parent_id=?", (career,)).fetchone()[0], 0)

        approved = decide_proposal(cfg, staged["proposal_id"], "approve")
        created = self.goals.get(approved["created_goal_id"])
        self.assertEqual(created["parent_id"], career)
        self.assertEqual(self.goals.semantic_role(created["id"])["role"], "project")

    def test_plain_language_intake_points_to_existing_equivalent(self):
        career = self.goals.create("overgoal", "Work & Contribution")
        project = self.goals.create(
            "subgoal", "Build Upwork automation service", parent_id=career,
            description="Build an Upwork automation service to test client demand.")
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db,
                     curiosity_backend="stub")

        result = recommend_goal_intake(
            cfg, StubGoalPlanner(), career,
            "Build an Upwork automation service to test client demand")

        self.assertEqual(result["action"], "existing")
        self.assertEqual(result["existing_goal_id"], project)

    def test_ai_whole_path_restructure_promotes_domain_and_labels_nested_scopes(self):
        life = self.goals.create("overgoal", "Luke's Life")
        career = self.goals.create("subgoal", "Career & Building Things", parent_id=life)
        project = self.goals.create("subgoal", "Run Upwork automation micro-test",
                                    parent_id=career)
        stage = self.goals.create("subgoal", "Evaluate and validate", parent_id=project)
        leaf = self.goals.create("task", "Choose one offer", parent_id=stage)
        self.goals.add_evidence(leaf, "manual_note", "proof", "Prior work remains attached.")
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db,
                     curiosity_backend="stub")

        result = recommend_goal_tree_restructure(cfg, StubGoalPlanner(), project)

        self.assertEqual(result["action"], "restructure")
        preview = result["recommendation"]["preview"]
        self.assertEqual(len(preview["structural_changes"]), 1)
        self.assertEqual(preview["structural_changes"][0]["goal_id"], career)
        self.assertEqual(preview["structural_changes"][0]["proposed"]["type"], "overgoal")
        roles = {item["goal_id"]: item["proposed_role"] for item in preview["role_changes"]}
        self.assertEqual(roles, {project: "project", stage: "stage"})
        self.assertEqual(preview["retained_counts"]["evidence_links"], 1)

        staged = propose_goal_tree_restructure(
            cfg, result["scope_id"], result["recommendation"]["changes"],
            result["recommendation"]["role_updates"], result["rationale"])
        unchanged = self.goals.get(career)
        applied = decide_proposal(cfg, staged["proposal_id"], "approve")

        self.assertEqual(unchanged["type"], "subgoal")
        self.assertTrue(applied["ok"])
        self.assertEqual(self.goals.get(career)["type"], "overgoal")
        self.assertEqual(self.goals.get(career)["parent_id"], self.goals.root_id)
        self.assertEqual(self.goals.get(project)["parent_id"], career)
        self.assertEqual(self.goals.get(stage)["parent_id"], project)
        self.assertEqual(self.goals.semantic_role(project)["role"], "project")
        self.assertEqual(self.goals.semantic_role(stage)["role"], "stage")
        self.assertEqual(self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_evidence_link WHERE goal_id=?", (leaf,)
        ).fetchone()[0], 1)

    def test_invalid_whole_path_restructure_is_atomic(self):
        root = self.goals.create("overgoal", "Career")
        project = self.goals.create("subgoal", "Client project", parent_id=root)
        stage = self.goals.create("subgoal", "Delivery stage", parent_id=project)
        before = (self.goals.get(project)["parent_id"], self.goals.get(stage)["parent_id"])

        with self.assertRaisesRegex(ValueError, "cycle"):
            self.goals.restructure_batch([
                {"goal_id": project, "new_type": "subgoal", "parent_id": stage},
                {"goal_id": stage, "new_type": "subgoal", "parent_id": project},
            ], [])

        self.assertEqual((self.goals.get(project)["parent_id"],
                          self.goals.get(stage)["parent_id"]), before)

    def test_whole_path_restructure_guards_new_nested_stage_roles(self):
        root = self.goals.create("overgoal", "Career")
        project = self.goals.create("subgoal", "Client launch", parent_id=root)
        outer = self.goals.create("subgoal", "Delivery phase", parent_id=project)
        inner = self.goals.create("subgoal", "Quality review", parent_id=outer)
        self.goals._set_semantic_role(project, "project", rationale="Finite project")
        self.goals._set_semantic_role(outer, "stage", rationale="Project phase")
        self.goals._set_semantic_role(inner, "project", rationale="Prior classification")

        with self.assertRaisesRegex(ValueError, "macro-stage/substage"):
            self.goals.restructure_batch_preview([], [
                {"goal_id": inner, "role": "stage", "reason": "Make this another stage."},
            ])

        explanation = (
            "Delivery is the macro-stage; quality review is a bounded substage with its own leaves.")
        preview = self.goals.restructure_batch_preview([], [
            {"goal_id": inner, "role": "stage", "reason": "Clarify the phase boundary.",
             "nested_stage_justification": explanation},
        ])
        self.assertEqual(preview["role_changes"][0]["proposed_role"], "stage")

    def test_completion_excludes_paused_and_archived_tasks(self):
        over = self.goals.create("overgoal", "Korean")
        complete = self.goals.create("task", "Done", parent_id=over, status="completed")
        self.goals.create("task", "Open", parent_id=over)
        self.goals.create("task", "Paused", parent_id=over, status="paused")
        self.goals.create("task", "Archived", parent_id=over, status="archived")
        node = next(x for x in self.goals.tree()["children"] if x["id"] == over)
        self.assertEqual(node["completion"], {"done": 1, "total": 2, "percent": 50.0})
        self.assertIsNotNone(self.goals.get(complete)["completed_at"])

    def test_archive_and_restore_subtree_preserve_each_prior_status(self):
        root = self.goals.create("overgoal", "Work")
        project = self.goals.create("subgoal", "Client project", parent_id=root,
                                    status="paused")
        completed = self.goals.create(
            "task", "Finished step", parent_id=project, status="completed")
        active = self.goals.create("task", "Next step", parent_id=project)
        self.goals.add_evidence(completed, "manual_note", "proof", "Finished proof")

        archived = self.goals.delete_subtree(project)

        self.assertEqual(archived, 3)
        self.assertTrue(all(self.goals.get(node_id)["status"] == "archived"
                            for node_id in (project, completed, active)))
        self.assertEqual(self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_evidence_link WHERE goal_id=?", (completed,)
        ).fetchone()[0], 1)

        restored = self.goals.restore_subtree(project)

        self.assertEqual(restored, 3)
        self.assertEqual(self.goals.get(project)["status"], "paused")
        self.assertEqual(self.goals.get(completed)["status"], "completed")
        self.assertEqual(self.goals.get(active)["status"], "active")
        self.assertEqual(self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_archive_snapshot WHERE archive_root_id=?", (project,)
        ).fetchone()[0], 0)

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

    def _new_root_placement(self):
        return {"mode": "new_root", "root_eligible": True,
                "root_title": "Learning & Education",
                "root_description": "An enduring domain for learning and skill development.",
                "user_confirmed": True}

    def _start_new_root(self, item):
        return start_planning(self.goals, StubGoalPlanner(), item,
                              self.goals.root_id, self._new_root_placement())

    def test_planner_persists_dialogue_and_only_commits_after_summary(self):
        item = self._suggestion()
        planner = StubGoalPlanner()
        session = start_planning(self.goals, planner, item,
                                 self.goals.root_id, self._new_root_placement())
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
        session = self._start_new_root(item)
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

    def test_unplaced_suggestion_cannot_silently_default_to_soul(self):
        item = self._suggestion()
        with self.assertRaisesRegex(ValueError, "placement review is required"):
            start_planning(self.goals, StubGoalPlanner(), item)

    def test_semantic_placement_prefers_career_branch_for_upwork_project(self):
        life = self.goals.create("overgoal", "Luke's Life")
        career = self.goals.create(
            "subgoal", "Career & Building Things", parent_id=life,
            description="Work, independent products, clients, and professional experiments.")
        self.goals.create("subgoal", "Korean Language", parent_id=life,
                          description="Language learning and cultural fluency.")
        cid = self.curiosities.add_curiosity("test freelance work", "Career experiment")
        item = self.curiosities.add_item(
            cid, "suggestion", "Post a small Upwork automation gig to test client demand.")
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db,
                     curiosity_backend="stub")

        result = recommend_suggestion_placement(cfg, StubGoalPlanner(), item)

        self.assertEqual(result["recommended_parent_id"], career)
        self.assertIn("Career & Building Things", result["proposed_path"])
        self.assertFalse(result["new_root"]["eligible"])

    def test_offline_placement_names_a_durable_domain_not_the_project(self):
        cid = self.curiosities.add_curiosity("test freelance work", "Career experiment")
        item = self.curiosities.add_item(
            cid, "suggestion", "Post a small Upwork automation gig to test demand.")
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db,
                     curiosity_backend="stub")

        result = recommend_suggestion_placement(cfg, StubGoalPlanner(), item)

        self.assertIsNone(result["recommended_parent_id"])
        self.assertTrue(result["new_root"]["eligible"])
        self.assertEqual(result["new_root"]["title"], "Career & Work")
        self.assertNotIn("Upwork automation gig ›", result["new_root"]["path"])

    def test_new_root_requires_durable_domain_approval_and_wraps_project(self):
        item = self._suggestion()
        with self.assertRaisesRegex(ValueError, "durable life-domain"):
            start_planning(self.goals, StubGoalPlanner(), item, self.goals.root_id, {
                "mode": "new_root", "root_eligible": False,
                "root_title": "Master one grammar pattern",
                "root_description": "A temporary exercise.",
            })
        session = self._start_new_root(item)
        session = continue_planning(
            self.goals, StubGoalPlanner(), session["id"], "Use it in five sentences")
        session = summarize_plan(self.goals, StubGoalPlanner(), session["id"])
        result = self.goals.commit_plan(session["id"])
        root = self.goals.get(result["goal_id"])
        self.assertEqual(root["title"], "Learning & Education")
        children = self.goals.conn.execute(
            "SELECT node_type FROM goal_node WHERE parent_id=?", (root["id"],)).fetchall()
        self.assertEqual([row["node_type"] for row in children], ["subgoal"])

    def test_confirming_a_different_parent_replaces_wrong_active_session(self):
        item = self._suggestion()
        old = self._start_new_root(item)
        career = self.goals.create("overgoal", "Career & Work")

        new = start_planning(self.goals, StubGoalPlanner(), item, career, {
            "mode": "existing", "parent_id": career,
            "parent_path": "Actualized Self › Career & Work", "user_confirmed": True,
        })

        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(new["target_parent_id"], career)
        self.assertEqual(self.goals.plan_session(old["id"])["status"], "abandoned")

    def test_overlapping_suggestion_can_propose_adapting_existing_leaf(self):
        root = self.goals.create("overgoal", "Career experiments")
        branch = self.goals.create("subgoal", "Test automation freelancing", parent_id=root)
        leaf = self.goals.create(
            "task", "Write and post an Upwork automation listing", parent_id=branch,
            description="Package one automation script and post a small Upwork listing.")
        cid = self.curiosities.add_curiosity("test paid automation work", "Paid automation")
        item = self.curiosities.add_item(
            cid, "suggestion",
            "Pick one small automation task already solved at work, package it as a reusable "
            "script, and post a $50-100 micro-gig on Upwork this week.", confidence=.87)
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db)

        overlap = suggestion_leaf_overlaps(cfg, item)
        self.assertEqual(overlap["matches"][0]["goal_id"], leaf)
        self.assertGreaterEqual(overlap["matches"][0]["similarity"], .28)

        proposed = propose_suggestion_leaf_update(
            cfg, item, leaf, "Post one automation micro-gig",
            "Package a proven work automation and post it as a $50-100 Upwork micro-gig.")
        self.assertEqual(self.goals.get(leaf)["title"],
                         "Write and post an Upwork automation listing")
        result = decide_proposal(cfg, proposed["proposal_id"], "approve")
        self.assertTrue(result["ok"])
        self.assertEqual(self.goals.get(leaf)["title"], "Post one automation micro-gig")
        resolved = self.curiosities.get_item(item)
        self.assertEqual(resolved["status"], "tried")
        self.assertEqual(resolved["implementation_goal_id"], leaf)

    def test_overlap_recognizes_the_suggestion_that_created_a_concise_leaf(self):
        root = self.goals.create("overgoal", "Career experiments")
        branch = self.goals.create("subgoal", "Test independent automation work", parent_id=root)
        leaf = self.goals.create(
            "task", "Run the current market test", parent_id=branch,
            description="Complete the already-defined experiment.")
        cid = self.curiosities.add_curiosity("test automation freelancing", "Freelance test")
        original = self.curiosities.add_item(
            cid, "suggestion",
            "Pick one small automation task already solved at work, extract it as a reusable "
            "tool, and post a low-cost micro-gig on Upwork this week to test demand.",
            confidence=.86)
        self.curiosities.conn.execute(
            "UPDATE curiosity_item SET status='tried',implementation_goal_id=? WHERE id=?",
            (leaf, original))
        self.curiosities.conn.commit()
        refined = self.curiosities.add_item(
            cid, "suggestion",
            "Pick one small automation task you already solved at work, extract it as a reusable "
            "tool or script, and post a $50-100 micro-gig on Upwork this week to test demand and "
            "whether you enjoy the client-facing work.", confidence=.9)
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db)

        overlap = suggestion_leaf_overlaps(cfg, refined)

        match = next(m for m in overlap["matches"] if m["goal_id"] == leaf)
        self.assertEqual(match["matched_via"], "originating_suggestion")
        self.assertGreater(match["origin_similarity"], match["leaf_similarity"])

    def test_plan_origin_matches_its_root_but_is_not_inherited_by_every_leaf(self):
        root = self.goals.create(
            "overgoal", "Run a small market experiment",
            description="Test one independent work idea from selection through results.")
        unrelated = self.goals.create(
            "task", "Record emotional reactions", parent_id=root,
            description="Write a short reflection after the experiment.")
        cid = self.curiosities.add_curiosity("test automation work", "Market test")
        original = self.curiosities.add_item(
            cid, "suggestion",
            "Pick one automation task already solved at work and post it as a small Upwork gig.")
        self.curiosities.conn.execute(
            "UPDATE curiosity_item SET status='tried',implementation_goal_id=? WHERE id=?",
            (root, original))
        self.curiosities.conn.commit()
        refined = self.curiosities.add_item(
            cid, "suggestion",
            "Pick one automation task you already solved at work and post it as a small Upwork "
            "micro-gig to test demand and independent work.")
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db)

        matches = suggestion_leaf_overlaps(cfg, refined)["matches"]

        root_match = next(m for m in matches if m["goal_id"] == root)
        self.assertEqual(root_match["type_label"], "Root")
        self.assertEqual(root_match["matched_via"], "originating_suggestion")
        inherited = [m for m in matches if m["goal_id"] == unrelated]
        self.assertFalse(inherited)

    def test_repeated_adapt_replaces_prior_open_update_for_same_suggestion(self):
        root = self.goals.create("overgoal", "Career experiment")
        leaf = self.goals.create("task", "Post one listing", parent_id=root)
        cid = self.curiosities.add_curiosity("test work", "Work test")
        item = self.curiosities.add_item(
            cid, "suggestion", "Post one small freelance listing to test demand.")
        cfg = Config(db_path=os.path.join(self.tmp.name, "events.db"), memory_db_path=self.db)

        first = propose_suggestion_leaf_update(
            cfg, item, leaf, "Post one listing", "First combined direction.")
        second = propose_suggestion_leaf_update(
            cfg, item, leaf, "Post one clearer listing", "Revised combined direction.")

        self.assertNotEqual(first["proposal_id"], second["proposal_id"])
        self.assertEqual(second["replaced_open_proposals"], 1)
        rows = self.goals.conn.execute(
            "SELECT id,status FROM goal_agent_proposal WHERE id IN (?,?) ORDER BY id",
            (first["proposal_id"], second["proposal_id"])).fetchall()
        self.assertEqual([(r["id"], r["status"]) for r in rows],
                         [(first["proposal_id"], "stale"),
                          (second["proposal_id"], "open")])


    def test_invalid_draft_rolls_back_entire_commit(self):
        item = self._suggestion()
        session = self._start_new_root(item)
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
        session = self._start_new_root(item)
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
            "SELECT id,node_type FROM goal_node WHERE parent_id=?",
            (result["goal_id"],)).fetchall()
        self.assertEqual([r["node_type"] for r in rows], ["subgoal"])
        children = self.goals.conn.execute(
            "SELECT node_type FROM goal_node WHERE parent_id=?", (rows[0]["id"],)).fetchall()
        self.assertEqual(sorted(r["node_type"] for r in children),
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

    def test_archive_and_restore_bridges_keep_subtree_reversible(self):
        soul = self.api.goal_state()["tree"]["id"]
        root = self.api.goal_create("overgoal", "Work", soul)["goal_id"]
        leaf = self.api.goal_create("task", "Test offer", root)["goal_id"]

        archived = self.api.goal_archive(root)
        restored = self.api.goal_restore(root)

        self.assertTrue(archived["ok"] and restored["ok"])
        self.assertEqual(archived["archived_count"], 2)
        root_after = next(node for node in restored["tree"]["children"]
                          if node["id"] == root)
        self.assertEqual(root_after["status"], "active")
        self.assertEqual(root_after["children"][0]["id"], leaf)

    def test_root_starter_and_plain_language_intake_bridges(self):
        state = self.api.goal_state()
        starters = self.api.goal_root_starters("en")
        made = self.api.goal_root_starters_apply(["work"], "en")
        work = made["created_goal_ids"][0]
        classified = self.api.goal_intake_recommend(
            work, "Build a client onboarding automation project")
        staged = self.api.goal_intake_propose(
            classified["recommendation"], classified["rationale"])

        self.assertTrue(state["ok"] and starters["ok"] and made["ok"])
        self.assertEqual(len(starters["starters"]), 7)
        self.assertTrue(classified["ok"] and staged["ok"])
        self.assertEqual(classified["recommendation"]["semantic_role"], "project")
        before = self.api.goal_state()["tree"]
        work_before = next(node for node in before["children"] if node["id"] == work)
        self.assertEqual(work_before["children"], [])

        approved = self.api.goal_ai_proposal(staged["proposal_id"], "approve", None, "")
        work_after = next(node for node in approved["tree"]["children"] if node["id"] == work)
        self.assertEqual(len(work_after["children"]), 1)
        self.assertEqual(work_after["children"][0]["semantic_role"], "project")

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
        root = self.api.goal_create("overgoal", "Learning", self.api.goal_state()["tree"]["id"])
        started = self.api.goal_plan_start(item, root["goal_id"], {
            "mode": "existing", "parent_id": root["goal_id"],
            "parent_path": "Actualized Self › Learning", "user_confirmed": True,
        })
        self.assertTrue(started["ok"])
        session_id = started["session"]["id"]
        replied = self.api.goal_plan_reply(session_id, "Explain it without notes")
        self.assertTrue(replied["ok"])
        summarized = self.api.goal_plan_summarize(session_id)
        self.assertEqual(summarized["session"]["status"], "ready")
        committed = self.api.goal_plan_commit(session_id)
        self.assertTrue(committed["ok"])

    def test_placement_bridge_blocks_soul_fallback_and_recommends_existing_domain(self):
        state = self.api.goal_state()
        soul = state["tree"]["id"]
        life = self.api.goal_create("overgoal", "My Life", soul)["goal_id"]
        career = self.api.goal_create("subgoal", "Career & Client Work", life)["goal_id"]
        curiosity = CuriosityStore(self.api.cfg.memory_db_path)
        cid = curiosity.add_curiosity("test freelance work", "Career experiment")
        item = curiosity.add_item(
            cid, "suggestion", "Post an Upwork automation service to test client demand.")
        curiosity.close()

        blocked = self.api.goal_plan_start(item)
        placement = self.api.goal_plan_placement(item)

        self.assertFalse(blocked["ok"])
        self.assertIn("placement review is required", blocked["message"])
        self.assertTrue(placement["ok"])
        self.assertEqual(placement["recommended_parent_id"], career)
        self.assertIn("Career & Client Work", placement["proposed_path"])

    def test_restructure_bridge_previews_then_waits_for_separate_approval(self):
        soul = self.api.goal_state()["tree"]["id"]
        life = self.api.goal_create("overgoal", "My Life", soul)["goal_id"]
        career = self.api.goal_create("subgoal", "Career", life)["goal_id"]
        project = self.api.goal_create("overgoal", "Temporary client experiment", soul)["goal_id"]
        self.api.goal_create("task", "Run experiment", project)

        recommended = self.api.goal_restructure_recommend(project)
        preview = self.api.goal_restructure_preview(project, "subgoal", career, 0)
        staged = self.api.goal_restructure_propose(
            project, "subgoal", career, 0, "Career owns this temporary experiment.",
            "project")
        unchanged = next(node for node in self.api.goal_state()["tree"]["children"]
                         if node["id"] == project)
        applied = self.api.goal_ai_proposal(staged["proposal_id"], "approve", None, "")

        self.assertTrue(recommended["ok"] and preview["ok"] and staged["ok"] and applied["ok"])
        self.assertEqual(unchanged["type"], "overgoal")
        moved = next(node for node in applied["tree"]["children"][0]["children"]
                     if node["id"] == career)["children"][0]
        self.assertEqual((moved["id"], moved["type"]), (project, "subgoal"))
        self.assertEqual(moved["semantic_role"], "project")

        whole = self.api.goal_tree_restructure_recommend(project)
        recommendation = whole["recommendation"]
        whole_staged = self.api.goal_tree_restructure_propose(
            whole["scope_id"], recommendation["changes"],
            recommendation["role_updates"], whole["rationale"])
        whole_applied = self.api.goal_ai_proposal(
            whole_staged["proposal_id"], "approve", None, "")

        self.assertTrue(whole["ok"] and whole_staged["ok"] and whole_applied["ok"])
        career_after = next(node for node in whole_applied["tree"]["children"]
                            if node["id"] == career)
        self.assertEqual(career_after["type"], "overgoal")
        project_after = next(node for node in career_after["children"] if node["id"] == project)
        self.assertEqual(project_after["semantic_role"], "project")


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
