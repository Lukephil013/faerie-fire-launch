import json
import os
import tempfile
import unittest
from unittest import mock

from livingpc import crypto
from livingpc.config import Config
from livingpc.curiosity import CuriosityStore
from livingpc.goals import (
    GoalStore, LeafHorizonError, StubGoalPlanner, continue_planning, record_experiment_outcome,
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

        # The "area" role is displayed as "Branch"; the stored token is unchanged.
        self.assertEqual(preview["proposed"]["type_label"], "Branch")
        self.assertEqual(preview["proposed"]["semantic_role"], "area")
        self.assertEqual(applied["goal_id"], branch)
        self.assertEqual(self.goals.semantic_role(branch)["role"], "area")
        rendered = next(node for node in self.goals.tree()["children"]
                        if node["id"] == root)["children"][0]
        self.assertEqual((rendered["type"], rendered["semantic_role"]),
                         ("subgoal", "area"))

    def test_nested_stage_is_rejected_even_with_justification(self):
        root = self.goals.create("overgoal", "Work & Contribution")
        project = self.goals.create("subgoal", "Client launch", parent_id=root)
        outer = self.goals.create("subgoal", "Delivery phase", parent_id=project)
        inner = self.goals.create("subgoal", "Quality review", parent_id=outer)
        self.goals._set_semantic_role(project, "project", rationale="Finite launch project.")
        self.goals._set_semantic_role(outer, "stage", rationale="Delivery is one project phase.")

        with self.assertRaisesRegex(ValueError, "directly beneath a Project"):
            self.goals.restructure_preview(
                inner, "subgoal", outer, semantic_role="stage",
                nested_stage_justification="Even explicit nested-stage prose cannot bypass this.")

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

    def test_finite_action_with_phases_is_project_not_area(self):
        health = self.goals.create("overgoal", "Health & Energy")
        project = self.goals.create(
            "subgoal", "Map dread-source tasks and clients", parent_id=health)
        stage = self.goals.create(
            "subgoal", "Track task types for two weeks", parent_id=project)

        health_node = next(node for node in self.goals.tree()["children"]
                           if node["id"] == health)
        project_node = next(node for node in health_node["children"]
                            if node["id"] == project)
        stage_node = next(node for node in project_node["children"]
                          if node["id"] == stage)
        self.assertEqual(project_node["semantic_role"], "project")
        self.assertEqual(stage_node["semantic_role"], "stage")

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

    def test_whole_path_restructure_rejects_terminal_stage_and_project_nesting(self):
        root = self.goals.create("overgoal", "Career")
        area = self.goals.create("subgoal", "Client work", parent_id=root)
        project = self.goals.create("subgoal", "Client launch", parent_id=area)
        terminal = self.goals.create("subgoal", "Quality review", parent_id=project)
        self.goals._set_semantic_role(area, "area", rationale="Ongoing client work")
        self.goals._set_semantic_role(project, "project", rationale="Finite project")

        with self.assertRaisesRegex(ValueError, "at least one concrete Leaf"):
            self.goals.restructure_batch_preview([], [{
                "goal_id": terminal, "role": "stage", "reason": "Phase",
            }])
        with self.assertRaisesRegex(ValueError, "beneath a Root or Area"):
            self.goals.restructure_batch_preview([], [{
                "goal_id": terminal, "role": "project", "reason": "Nested project",
            }])

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

    def test_archive_detects_orphaned_investigation(self):
        root = self.goals.create("overgoal", "Luke's Life")
        branch = self.goals.create("subgoal", "Inner work", parent_id=root)
        cid = self.curiosities.add_curiosity(
            "the rage underneath", "Agency as a Physiological Signal")
        self.goals.link_curiosity(branch, cid)

        orphans = self.goals.orphaned_curiosities_for_archive(root)

        self.assertEqual([o["id"] for o in orphans], [cid])
        self.assertEqual(orphans[0]["label"], "Agency as a Physiological Signal")
        # Detection must not mutate anything.
        self.assertEqual(self.goals.get(root)["status"], "active")

    def test_investigation_with_another_active_home_is_not_orphaned(self):
        archived_root = self.goals.create("overgoal", "Luke's Life")
        active_root = self.goals.create("overgoal", "Health & Energy")
        cid = self.curiosities.add_curiosity("the rage underneath", "Agency")
        self.goals.link_curiosity(archived_root, cid)
        self.goals.link_curiosity(active_root, cid)

        self.assertEqual(
            self.goals.orphaned_curiosities_for_archive(archived_root), [])

    def test_reroute_curiosity_drops_archived_link_and_attaches_new(self):
        old_root = self.goals.create("overgoal", "Luke's Life")
        new_root = self.goals.create("overgoal", "Health & Energy")
        cid = self.curiosities.add_curiosity("the rage underneath", "Agency")
        self.goals.link_curiosity(old_root, cid)
        self.goals.delete_subtree(old_root)

        result = self.goals.reroute_curiosity(cid, new_root)

        self.assertEqual(result["new_goal_id"], new_root)
        self.assertEqual(result["removed_from"], [old_root])
        links = {row["goal_id"] for row in self.goals.conn.execute(
            "SELECT goal_id FROM goal_curiosity_link WHERE curiosity_id=?", (cid,))}
        self.assertEqual(links, {new_root})
        node = next(x for x in self.goals.tree()["children"] if x["id"] == new_root)
        self.assertEqual(node["curiosities"][0]["label"], "Agency")

    def test_reroute_curiosity_rejects_archived_destination(self):
        dead = self.goals.create("overgoal", "Gone")
        cid = self.curiosities.add_curiosity("x", "X")
        self.goals.delete_subtree(dead)
        with self.assertRaises(ValueError):
            self.goals.reroute_curiosity(cid, dead)

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
        # Force a legacy-shaped table for the 'pre-existing profile' mirror test.
        self.goals.conn.execute("DROP TABLE IF EXISTS curiosity_metric_profile")
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

    def test_ready_plan_requires_and_persists_final_placement_confirmation(self):
        item = self._suggestion()
        placement = {**self._new_root_placement(), "user_confirmed": False,
                     "review_required": True}
        session = start_planning(
            self.goals, StubGoalPlanner(), item, self.goals.root_id, placement)
        session = summarize_plan(self.goals, StubGoalPlanner(), session["id"])
        with self.assertRaisesRegex(ValueError, "review and confirm"):
            self.goals.commit_plan(session["id"])

        confirmed = self.goals.confirm_plan_placement(session["id"], {
            **self._new_root_placement(), "target_parent_id": self.goals.root_id,
        })
        self.assertTrue(confirmed["draft"]["_placement"]["user_confirmed"])
        self.assertEqual(confirmed["target_parent_id"], self.goals.root_id)
        self.assertTrue(self.goals.commit_plan(session["id"])["goal_id"])

    def test_stub_planner_tells_user_how_to_finish_after_clarification(self):
        item = self._suggestion(); planner = StubGoalPlanner()
        session = self._start_new_root(item)
        session = continue_planning(self.goals, planner, session["id"], "Five examples")
        session = continue_planning(self.goals, planner, session["id"], "Write the first one")
        self.assertIn("press Summarize & review", session["messages"][-1]["content"])

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
        # One-Leaf model: the group had two Leaves (Brainstorm, Unknown typed);
        # only the first is committed, so one subgoal + one task remain.
        self.assertEqual(sorted(r["node_type"] for r in children),
                         ["subgoal", "task"])

    def test_commit_persists_planner_area_project_and_stage_roles(self):
        item = self._suggestion()
        root = self.goals.create("overgoal", "Health & Energy")
        session = start_planning(self.goals, StubGoalPlanner(), item, root, {
            "mode": "existing", "parent_id": root,
            "parent_path": "Actualized Self › Health & Energy", "user_confirmed": True,
        })
        draft = {"nodes": [{
            "type": "subgoal", "semantic_role": "area", "title": "Work/Life Mental Health Effects",
            "children": [{"type": "subgoal", "semantic_role": "project",
                "title": "Map dread sources", "children": [{
                    "type": "subgoal", "semantic_role": "stage", "title": "Observe patterns",
                    "children": [{"type": "task", "semantic_role": None,
                                  "title": "Log one dread event", "children": []}],
                }],
            }],
        }]}
        self.goals.set_plan_draft(session["id"], draft, summary="plan", ready=True)
        created = self.goals.commit_plan(session["id"])["goal_id"]
        root_node = next(node for node in self.goals.tree()["children"]
                         if node["id"] == root)
        area = next(node for node in root_node["children"] if node["id"] == created)
        project = area["children"][0]; stage = project["children"][0]
        self.assertEqual((area["semantic_role"], project["semantic_role"],
                          stage["semantic_role"]), ("area", "project", "stage"))


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
            self.assertEqual(next_proposal["type"], "update_fields")
            self.assertTrue(next_proposal["payload"]["adaptive_horizon"])
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

    def test_goal_state_derives_now_and_provisional_from_direct_leaf_position(self):
        soul = self.api.goal_state()["tree"]["id"]
        root = self.api.goal_create("overgoal", "Work", soul)["goal_id"]
        from livingpc.goals import GoalStore
        store = GoalStore(self.api.cfg.memory_db_path)
        project = store.create("subgoal", "Client experiment", parent_id=root)
        store._set_semantic_role(
            project, "project", rationale="Produces one client experiment.")
        unrelated = store.create("subgoal", "Computer systems", parent_id=root)
        store._set_semantic_role(
            unrelated, "area", rationale="Owns ongoing computer systems.")
        unrelated_leaf = store.create("task", "Computer Whiteboard", parent_id=unrelated)
        store.close()
        first = self.api.goal_create("task", "Draft the profile", project)["goal_id"]
        second = self.api.goal_create("task", "Publish and scan", project)["goal_id"]
        completed = self.api.goal_create("task", "Historical research", project)["goal_id"]
        self.api.goal_update(first, {
            "status": "paused", "priority": "low", "due_date": "2099-12-31",
        })
        self.api.goal_update(second, {
            "priority": "high", "due_date": "2000-01-01",
        })
        self.api.goal_update(completed, {"status": "completed"})
        marked = self.api.goal_set_project_signal(
            project, "currently_working", True)
        self.assertTrue(marked["ok"])

        tree = self.api.goal_state()["tree"]
        root_node = next(node for node in tree["children"] if node["id"] == root)
        project_node = next(node for node in root_node["children"] if node["id"] == project)
        unrelated_node = next(node for node in root_node["children"]
                              if node["id"] == unrelated)
        leaves = {leaf["id"]: leaf for leaf in project_node["children"]}

        self.assertEqual(leaves[first]["planning_role"], "now")
        self.assertEqual(leaves[second]["planning_role"], "provisional")
        self.assertIsNone(leaves[completed]["planning_role"])
        self.assertIsNone(next(leaf for leaf in unrelated_node["children"]
                               if leaf["id"] == unrelated_leaf)["planning_role"])

    def test_goal_state_follows_priority_area_through_stage_to_recursive_leaves(self):
        from livingpc.goals import GoalStore

        soul = self.api.goal_state()["tree"]["id"]
        root = self.api.goal_create("overgoal", "Health", soul)["goal_id"]
        store = GoalStore(self.api.cfg.memory_db_path)
        area = store.create("subgoal", "Regulation", parent_id=root)
        project = store.create("subgoal", "Map triggers", parent_id=area)
        stage = store.create("subgoal", "Capture phase", parent_id=project)
        first = store.create("task", "Log first event", parent_id=stage)
        second = store.create("task", "Log second event", parent_id=stage)
        store._set_semantic_role(area, "area", rationale="Ongoing health scope")
        store._set_semantic_role(project, "project", rationale="Finite map")
        store._set_semantic_role(stage, "stage", rationale="Groups capture Leaves")
        store.set_project_signal(area, "highest_priority")
        store.close()

        tree = self.api.goal_state()["tree"]
        def find(node, goal_id):
            if node["id"] == goal_id:
                return node
            return next((found for child in node.get("children", [])
                         if (found := find(child, goal_id))), None)
        self.assertTrue(find(tree, project)["project_focus"]["auto_current"])
        self.assertEqual(find(tree, first)["planning_role"], "now")
        self.assertEqual(find(tree, second)["planning_role"], "provisional")

    def test_semantic_role_changes_are_audited_with_approval_source(self):
        root = self.api.goal_create(
            "overgoal", "Work", self.api.goal_state()["tree"]["id"])["goal_id"]
        from livingpc.goals import GoalStore
        store = GoalStore(self.api.cfg.memory_db_path)
        branch = store.create("subgoal", "Client work", parent_id=root)
        store._set_semantic_role(branch, "project", rationale="Finite", source="ai")
        store._set_semantic_role(
            branch, "area", rationale="Ongoing", source="approved_restructure",
            proposal_id=77)
        rows = store.conn.execute(
            "SELECT old_role,new_role,source,proposal_id FROM goal_semantic_role_history "
            "WHERE goal_id=? ORDER BY id", (branch,)).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [(None, "project", "ai", None),
             ("project", "area", "approved_restructure", 77)])
        store.close()

    def test_attention_signals_form_area_to_project_hierarchy(self):
        from livingpc.goals import GoalStore

        soul = self.api.goal_state()["tree"]["id"]
        root = self.api.goal_create("overgoal", "Work", soul)["goal_id"]
        store = GoalStore(self.api.cfg.memory_db_path)
        area = store.create("subgoal", "Tools", parent_id=root)
        other_area = store.create("subgoal", "Other", parent_id=root)
        store._set_semantic_role(area, "area", rationale="Owns ongoing tools.")
        store._set_semantic_role(other_area, "area", rationale="Other work.")
        first = store.create("subgoal", "First project", parent_id=area)
        second = store.create("subgoal", "Second project", parent_id=area)
        outside = store.create("subgoal", "Outside project", parent_id=other_area)
        for project in (first, second, outside):
            store._set_semantic_role(project, "project", rationale="Finite result.")

        with self.assertRaisesRegex(ValueError, "active Area"):
            store.set_project_signal(first, "highest_priority")
        self.assertEqual(store.set_project_signal(area, "highest_priority")["highest_priority"], area)
        self.assertEqual(store.effective_current_project_id(), first)
        self.assertTrue(store.project_focus(first)["auto_current"])
        with self.assertRaisesRegex(ValueError, "inside the Highest priority Area"):
            store.set_project_signal(outside, "currently_working")
        store.set_project_signal(second, "currently_working")
        self.assertEqual(store.effective_current_project_id(), second)
        store.set_project_signal(other_area, "highest_priority")
        self.assertIsNone(store.project_signals()["currently_working"])
        self.assertEqual(store.effective_current_project_id(), outside)
        store.close()

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


class TestAdaptiveLeafHorizon(GoalTestCase):
    def test_committed_and_reserved_leaves_share_two_leaf_capacity(self):
        project = self.goals.create("overgoal", "Client launch")
        self.goals.create("task", "Draft contract", parent_id=project)

        with self.assertRaises(LeafHorizonError) as raised:
            self.goals.validate_leaf_candidate(
                project, "Schedule kickoff",
                reservations=[{"title": "Review budget"}], horizon=20)

        self.assertEqual(raised.exception.code, "horizon_full")
        self.assertEqual(self.goals.open_leaf_count(project), 1)

    def test_duplicate_and_semantic_overlap_are_rejected_but_sequence_is_not(self):
        project = self.goals.create("overgoal", "Upwork profile")
        draft = self.goals.create("task", "Draft Upwork profile", parent_id=project)

        with self.assertRaises(LeafHorizonError) as exact:
            self.goals.validate_leaf_candidate(project, " DRAFT--upwork profile ")
        self.assertEqual(exact.exception.code, "duplicate_title")
        self.assertEqual(exact.exception.conflicting_leaf_id, draft)

        with self.assertRaises(LeafHorizonError) as overlap:
            self.goals.validate_leaf_candidate(project, "Draft Upwork freelancer profile")
        self.assertEqual(overlap.exception.code, "semantic_overlap")
        self.assertGreaterEqual(overlap.exception.similarity, 0.55)

        valid = self.goals.validate_leaf_candidate(
            project, "Publish profile and first posting scan")
        self.assertEqual(valid["open_after_create"], 2)

    def test_create_ai_leaf_revalidates_and_stores_origin_atomically(self):
        project = self.goals.create("overgoal", "Launch")
        first = self.goals.create_ai_leaf(
            "Write copy", parent_id=project,
            origin={"source_kind": "chat", "source_id": "proposal-1"})
        second = self.goals.create_ai_leaf("Publish page", parent_id=project)

        self.assertEqual(self.goals.origin(first)["source_kind"], "chat")
        self.assertIsNone(self.goals.origin(second))
        with self.assertRaises(LeafHorizonError) as raised:
            self.goals.create_ai_leaf("Share launch", parent_id=project, horizon=99)
        self.assertEqual(raised.exception.code, "horizon_full")
        self.assertEqual(self.goals.open_leaf_count(project), 2)

    def test_plain_language_intake_reserves_open_goal_ai_task_proposals(self):
        from livingpc.goal_ai import AgentProposal, GoalAgentStore

        project = self.goals.create("overgoal", "Launch")
        self.goals.create("task", "Write copy", parent_id=project)
        agents = GoalAgentStore(self.db)
        try:
            agents.add_proposal(project, AgentProposal(
                "create_child", project,
                {"type": "task", "title": "Publish page", "description": "Go live."},
                "Tentative next step."))
        finally:
            agents.close()
        cfg = Config(memory_db_path=self.db, goal_ai_leaf_horizon=2)
        recommendation = {
            "parent_id": project, "new_type": "task",
            "title": "Share launch", "description": "Tell early users.",
        }

        with self.assertRaises(LeafHorizonError) as raised:
            propose_goal_intake(cfg, recommendation, "New intake")

        self.assertEqual(raised.exception.code, "horizon_full")

    def test_replan_requires_every_direct_leaf_once_and_target_ownership(self):
        project = self.goals.create("overgoal", "Project")
        other_project = self.goals.create("overgoal", "Other")
        history = self.goals.create(
            "task", "Historical work", parent_id=project, status="completed")
        now = self.goals.create("task", "Current work", parent_id=project)
        elsewhere = self.goals.create("task", "Elsewhere", parent_id=other_project)

        with self.assertRaisesRegex(ValueError, "every non-archived Leaf of the project"):
            self.goals.validate_replan_project(
                project, [{"op": "keep", "leaf_id": now}])
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.goals.validate_replan_project(project, [
                {"op": "keep", "leaf_id": history},
                {"op": "keep", "leaf_id": now},
                {"op": "keep", "leaf_id": elsewhere},
            ])
        with self.assertRaisesRegex(ValueError, "only once"):
            self.goals.validate_replan_project(project, [
                {"op": "keep", "leaf_id": history},
                {"op": "keep", "leaf_id": now},
                {"op": "archive", "leaf_id": now},
            ])

    def test_completed_leaves_do_not_consume_replan_horizon(self):
        project = self.goals.create("overgoal", "Project")
        history = self.goals.create(
            "task", "Historical work", parent_id=project, status="completed")
        now = self.goals.create("task", "Implement feature", parent_id=project)
        provisional = self.goals.create(
            "task", "Validate feature", parent_id=project, status="paused")

        plan = self.goals.validate_replan_project(project, [
            {"op": "keep", "leaf_id": history},
            {"op": "keep", "leaf_id": now},
            {"op": "keep", "leaf_id": provisional},
        ], horizon=10)

        self.assertEqual([item["id"] for item in plan["final_open"]],
                         [now, provisional])
        self.assertEqual(plan["horizon"], 2)

    def test_replan_rejects_duplicate_create_and_over_horizon(self):
        project = self.goals.create("overgoal", "Project")
        current = self.goals.create("task", "Implement feature", parent_id=project)
        with self.assertRaises(LeafHorizonError):
            self.goals.validate_replan_project(project, [
                {"op": "keep", "leaf_id": current},
                {"op": "create", "title": "Draft Upwork profile"},
                {"op": "create", "title": "Draft Upwork freelancer profile"},
            ])
        with self.assertRaises(LeafHorizonError) as overfull:
            self.goals.validate_replan_project(project, [
                {"op": "keep", "leaf_id": current},
                {"op": "create", "title": "Review budget"},
                {"op": "create", "title": "Schedule kickoff"},
            ])
        self.assertEqual(overfull.exception.code, "horizon_full")

    def test_replan_updates_project_archives_with_snapshot_and_reorders(self):
        project = self.goals.create(
            "overgoal", "Old project", description="Old framing")
        history = self.goals.create(
            "task", "Brainstorm wins", parent_id=project, status="completed")
        current = self.goals.create("task", "Draft profile", parent_id=project)
        duplicate = self.goals.create("task", "Duplicate draft", parent_id=project)

        result = self.goals.apply_replan_project(project, [
            {"op": "keep", "leaf_id": history},
            {"op": "update", "leaf_id": current,
             "description": "Use the evidence-backed profile draft."},
            {"op": "archive", "leaf_id": duplicate},
            {"op": "create", "title": "Publish profile and scan postings",
             "description": "Publish, then apply the novelty filter."},
        ], project_update={
            "title": "Upwork application experiment",
            "description": "Publish the profile and apply to suitable postings.",
        }, origin={"source_kind": "chat", "source_id": "repair-card"})

        created = result["created_leaf_ids"][0]
        self.assertEqual(result["ordered_leaf_ids"], [history, current, created])
        self.assertEqual(result["ordered_leaf_count"], 3)
        self.assertEqual(result["open_leaf_ids"], [current, created])
        self.assertEqual(result["operation_counts"]["archive"], 1)
        self.assertEqual(result["operation_counts"]["update"], 1)
        self.assertEqual(self.goals.get(project)["title"],
                         "Upwork application experiment")
        self.assertEqual(self.goals.get(duplicate)["status"], "archived")
        self.assertEqual(self.goals.origin(created)["source_id"], "repair-card")
        snapshot = self.goals.conn.execute(
            "SELECT prior_status FROM goal_archive_snapshot "
            "WHERE archive_root_id=? AND goal_id=?", (duplicate, duplicate)).fetchone()
        self.assertEqual(snapshot["prior_status"], "active")

    def test_replan_failure_rolls_back_project_archive_and_create(self):
        project = self.goals.create(
            "overgoal", "Original", description="Original framing")
        first = self.goals.create("task", "Current", parent_id=project)
        duplicate = self.goals.create("task", "Duplicate", parent_id=project)
        before_ids = {row["id"] for row in self.goals.conn.execute(
            "SELECT id FROM goal_node").fetchall()}

        with mock.patch.object(
                self.goals, "set_origin", side_effect=RuntimeError("origin failed")):
            with self.assertRaisesRegex(RuntimeError, "origin failed"):
                self.goals.apply_replan_project(project, [
                    {"op": "keep", "leaf_id": first},
                    {"op": "archive", "leaf_id": duplicate},
                    {"op": "create", "title": "Review result"},
                ], project_update={"title": "Changed"},
                    origin={"source_kind": "chat"})

        self.assertEqual(self.goals.get(project)["title"], "Original")
        self.assertEqual(self.goals.get(duplicate)["status"], "active")
        after_ids = {row["id"] for row in self.goals.conn.execute(
            "SELECT id FROM goal_node").fetchall()}
        self.assertEqual(after_ids, before_ids)
        self.assertEqual(self.goals.conn.execute(
            "SELECT COUNT(*) FROM goal_archive_snapshot WHERE archive_root_id=?",
            (duplicate,)).fetchone()[0], 0)

    def test_pending_reservations_aggregate_all_persisted_surfaces_privately(self):
        from livingpc.companion.history import ChatStore
        from livingpc.curiosity import ClassificationProposal
        from livingpc.goal_ai import AgentProposal, GoalAgentStore

        project = self.goals.create("overgoal", "Launch")
        agents = GoalAgentStore(self.db)
        try:
            goal_ai_id = agents.add_proposal(project, AgentProposal(
                "create_child", project,
                {"type": "task", "title": "Draft offer", "description": "Draft."},
                "Private rationale"))
        finally:
            agents.close()
        chats = ChatStore(self.db)
        chat_id = chats.create("Private chat")
        chats.replace_pending_proposals(chat_id, [{
            "action": "create_leaf", "target_node_id": project,
            "label": "Publish offer", "directive": "Publish.",
        }])
        curiosity_id = self.curiosities.add_curiosity("Study outreach", "Outreach")
        curiosity_proposal_id = self.curiosities.add_classification_proposal(
            curiosity_id, ClassificationProposal(
                "create_leaf", {"parent_id": project, "title": "Review replies",
                                "description": "Review."}, "Private rationale"))
        planning = self.goals.start_plan(None, project, "Plan it", draft={
            "nodes": [{"type": "task", "title": "Record outcome",
                       "description": "Record.", "children": []}],
        })

        reservations = self.goals.pending_leaf_reservations(project)

        self.assertEqual({item["title"] for item in reservations}, {
            "Draft offer", "Publish offer", "Review replies", "Record outcome"})
        self.assertTrue(all(set(item) == {
            "parent_id", "title", "description", "status", "replaces_leaf_id"
        } for item in reservations))
        excluded = self.goals.pending_leaf_reservations(project, exclude_refs={
            f"goal_ai:{goal_ai_id}", f"curiosity:{curiosity_proposal_id}",
            f"planning:{planning['id']}",
        })
        self.assertEqual([item["title"] for item in excluded], ["Publish offer"])

    def test_pending_removal_releases_replaced_capacity(self):
        from livingpc.companion.history import ChatStore

        project = self.goals.create("overgoal", "Launch")
        first = self.goals.create("task", "Draft offer", parent_id=project)
        second = self.goals.create("task", "Old outreach", parent_id=project)
        chats = ChatStore(self.db)
        chat_id = chats.create("Planning")
        chats.replace_pending_proposals(chat_id, [{
            "action": "delete_node", "target_node_id": second,
            "label": "Archive old outreach",
        }])

        reservations = self.goals.pending_leaf_reservations(project)
        validation = self.goals.validate_leaf_candidate(
            project, "Publish offer", reservations=reservations)

        self.assertEqual(reservations[0]["status"], "removed")
        self.assertEqual(reservations[0]["replaces_leaf_id"], second)
        self.assertEqual(validation["open_after_create"], 2)
        self.assertEqual(self.goals.get(first)["status"], "active")

    def test_replan_version_snapshot_blocks_overwriting_newer_leaf_context(self):
        project = self.goals.create(
            "overgoal", "Profile launch", description="Original framing")
        current = self.goals.create(
            "task", "Draft profile", parent_id=project,
            description="Initial draft direction")
        provisional = self.goals.create(
            "task", "Publish profile", parent_id=project)
        versions = self.goals.replan_expected_versions(project)
        self.goals.update(current, description="Newer user-approved direction")

        with self.assertRaisesRegex(ValueError, "replan is stale"):
            self.goals.apply_replan_project(project, [
                {"op": "update", "leaf_id": current,
                 "description": "Older staged direction"},
                {"op": "keep", "leaf_id": provisional},
            ], project_update={"description": "Older project framing"},
                expected_versions=versions)

        self.assertEqual(
            self.goals.get(current)["description"],
            "Newer user-approved direction")
        self.assertEqual(
            self.goals.get(project)["description"], "Original framing")

    def test_retire_pending_operations_honors_source_exclusions(self):
        from livingpc.companion.history import ChatStore
        from livingpc.curiosity import ClassificationProposal
        from livingpc.goal_ai import AgentProposal, GoalAgentStore

        project = self.goals.create("overgoal", "Launch")
        agents = GoalAgentStore(self.db)
        try:
            goal_ai_id = agents.add_proposal(project, AgentProposal(
                "create_child", project,
                {"type": "task", "title": "Draft offer"}, "Private"))
        finally:
            agents.close()
        chats = ChatStore(self.db)
        chat_id = chats.create("Keep this card")
        chats.replace_pending_proposals(chat_id, [{
            "action": "create_leaf", "target_node_id": project,
            "label": "Publish offer",
        }])
        curiosity_id = self.curiosities.add_curiosity("Study outreach", "Outreach")
        curiosity_proposal_id = self.curiosities.add_classification_proposal(
            curiosity_id, ClassificationProposal(
                "create_leaf", {"parent_id": project, "title": "Review replies"},
                "Private"))
        planning = self.goals.start_plan(None, project, "Plan", draft={
            "nodes": [{"type": "task", "title": "Record outcome", "children": []}],
        })

        result = self.goals.retire_pending_leaf_operations(
            project, exclude_refs={f"companion:{chat_id}:0"})

        self.assertEqual(result, {
            "retired": 3,
            "by_source": {"goal_ai": 1, "companion": 0,
                          "gardening": 0, "leaf_workspace": 0,
                          "curiosity": 1, "planning": 1},
        })
        self.assertEqual(self.goals.conn.execute(
            "SELECT status FROM goal_agent_proposal WHERE id=?", (goal_ai_id,)
        ).fetchone()[0], "stale")
        self.assertEqual(self.goals.conn.execute(
            "SELECT status FROM curiosity_classification_proposal WHERE id=?",
            (curiosity_proposal_id,)).fetchone()[0], "dismissed")
        self.assertEqual(self.goals.plan_session(planning["id"])["status"], "abandoned")
        self.assertEqual(len(chats.pending_proposals(chat_id)), 1)

        final = self.goals.retire_pending_leaf_operations(project)
        self.assertEqual(final["by_source"]["companion"], 1)
        self.assertEqual(chats.pending_proposals(chat_id), [])

    def test_planning_draft_trims_to_one_leaf_and_commit_respects_reservations(self):
        from livingpc.companion.history import ChatStore

        project = self.goals.create("overgoal", "Launch")
        session = self.goals.start_plan(None, project, "Plan", draft={})
        overfull = {"nodes": [{
            "type": "subgoal", "title": "Release stage", "children": [
                {"type": "task", "title": "Draft offer", "children": []},
                {"type": "task", "title": "Publish offer", "children": []},
                {"type": "task", "title": "Review replies", "children": []},
            ],
        }]}
        # One-Leaf model: an over-horizon draft is trimmed to its first Leaf, not
        # rejected. The stored draft keeps only "Draft offer".
        self.goals.set_plan_draft(session["id"], overfull, ready=True)
        stage = self.goals.plan_session(session["id"])["draft"]["nodes"][0]
        self.assertEqual([c["title"] for c in stage["children"]], ["Draft offer"])

        direct = {"nodes": [{"type": "task", "title": "Record outcome",
                             "description": "Record.", "children": []}]}
        self.goals.set_plan_draft(session["id"], direct, ready=True)
        self.goals.create("task", "Draft offer", parent_id=project)
        chats = ChatStore(self.db)
        chat_id = chats.create("Reserved")
        chats.replace_pending_proposals(chat_id, [{
            "action": "create_leaf", "target_node_id": project,
            "label": "Publish offer",
        }])

        with self.assertRaises(LeafHorizonError):
            self.goals.commit_plan(session["id"])

        self.assertEqual(self.goals.plan_session(session["id"])["status"], "ready")
        self.assertEqual(self.goals.open_leaf_count(project), 1)

    def test_upwork_repair_fixture_keeps_20_and_21_as_only_open_leaves(self):
        # Mirror the real IDs from the reported tree so the repair contract is
        # protected without touching the user's database.
        for index in range(6):
            self.goals.create("overgoal", f"Placeholder {index}")
        project = self.goals.create(
            "overgoal", "Run Upwork automation micro-test",
            description="Old listing-preparation strategy")
        self.assertEqual(project, 8)
        history = self.goals.create(
            "task", "Brainstorm automation wins", parent_id=project,
            status="completed")
        self.assertEqual(history, 9)
        for node_id in range(10, 20):
            made = self.goals.create(
                "task", f"Archived legacy {node_id}", parent_id=project,
                status="archived")
            self.assertEqual(made, node_id)
        now = self.goals.create("task", "Draft Upwork profile", parent_id=project)
        provisional = self.goals.create(
            "task", "Publish profile and first posting scan", parent_id=project)
        duplicate = self.goals.create(
            "task", "Draft Upwork profile", parent_id=project)
        obsolete = self.goals.create(
            "task", "Post Upwork profile and begin bidding", parent_id=project)
        self.assertEqual([now, provisional, duplicate, obsolete], [20, 21, 22, 23])

        result = self.goals.apply_replan_project(project, [
            {"op": "keep", "leaf_id": history},
            {"op": "keep", "leaf_id": now},
            {"op": "update", "leaf_id": provisional,
             "description": "Publish, then scan suitable existing postings."},
            {"op": "archive", "leaf_id": duplicate},
            {"op": "archive", "leaf_id": obsolete},
        ], project_update={
            "description": (
                "Update and publish the profile, apply to suitable existing "
                "postings using the AI/novelty filter, and track outcomes in Faerie.")
        })

        self.assertEqual(result["open_leaf_ids"], [20, 21])
        self.assertEqual(self.goals.get(22)["status"], "archived")
        self.assertEqual(self.goals.get(23)["status"], "archived")
        archived = [self.goals.get(node_id)["status"] for node_id in range(10, 20)]
        self.assertEqual(set(archived), {"archived"})


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


class TestPlannerReplyParsing(unittest.TestCase):
    def test_json_object_reads_fenced_prose_wrapped_and_trailing_brace_replies(self):
        from livingpc.goals import _json_object
        # Plain object.
        self.assertEqual(_json_object('{"message":"hi","draft":{}}'),
                         {"message": "hi", "draft": {}})
        # Wrapped in a ```json fence.
        self.assertEqual(_json_object('```json\n{"message":"hi"}\n```'),
                         {"message": "hi"})
        # Prose before and after the object, and trailing text with braces.
        self.assertEqual(
            _json_object('Sure! {"message":"ok","draft":{"nodes":[]}} (let me know {more})'),
            {"message": "ok", "draft": {"nodes": []}})
        # A brace inside a string value must not end the object early.
        self.assertEqual(_json_object('{"message":"use {curly} braces"}'),
                         {"message": "use {curly} braces"})

    def test_json_object_returns_empty_on_truncated_object(self):
        from livingpc.goals import _json_object
        # Reply cut off before the object closed (the max_tokens failure mode).
        self.assertEqual(_json_object('{"message":"hi","draft":{"nodes":[{"title":"a"'), {})

    def test_salvage_recovers_message_from_prose_and_truncated_json(self):
        from livingpc.goals import _salvage_planner_message
        # Pure prose reply — use it verbatim.
        self.assertEqual(_salvage_planner_message("Let's expand on the morning block."),
                         "Let's expand on the morning block.")
        # Truncated JSON, but the message field is intact and closed.
        self.assertEqual(
            _salvage_planner_message('{"message":"Good point — you already do this.","draft":{"nodes":[{'),
            "Good point — you already do this.")
        # Escaped quotes inside the message survive.
        self.assertEqual(
            _salvage_planner_message('{"message":"He said \\"hi\\" to me","draft":{'),
            'He said "hi" to me')
        # Nothing usable (broken JSON with no message field) → empty.
        self.assertEqual(_salvage_planner_message('{"draft":{"nodes":[{"title":"a"'), "")

    def _fake_planner(self, text, stop="end_turn"):
        from livingpc.goals import ClaudeGoalPlanner

        class FakeMsg:
            content = [type("Block", (), {"type": "text", "text": text})()]
            stop_reason = stop

        class FakeMessages:
            def create(self, **kwargs):
                return FakeMsg()

        planner = ClaudeGoalPlanner.__new__(ClaudeGoalPlanner)
        planner.model = "test"
        planner.client = type("Client", (), {"messages": FakeMessages()})()
        return planner

    def test_reply_keeps_the_turn_when_the_model_returns_prose(self):
        session = {"messages": [{"role": "assistant", "content": "q"}],
                   "draft": {"nodes": [1]}}
        planner = self._fake_planner("I already do this — let's expand it.")
        message, draft = planner.reply(session, "hello", {"type": "subgoal"})
        self.assertEqual(message, "I already do this — let's expand it.")
        # The existing draft is preserved rather than wiped.
        self.assertEqual(draft, {"nodes": [1]})

    def test_reply_salvages_message_from_truncated_json(self):
        session = {"messages": [{"role": "assistant", "content": "q"}],
                   "draft": {"nodes": [1]}}
        planner = self._fake_planner(
            '{"message":"You already do this.","draft":{"nodes":[{', stop="max_tokens")
        message, draft = planner.reply(session, "hi", {"type": "subgoal"})
        self.assertEqual(message, "You already do this.")
        self.assertEqual(draft, {"nodes": [1]})

    def test_reply_still_raises_when_nothing_is_recoverable(self):
        session = {"messages": [{"role": "assistant", "content": "q"}], "draft": {}}
        planner = self._fake_planner('{"draft":{"nodes":[{', stop="max_tokens")
        with self.assertRaises(ValueError):
            planner.reply(session, "hi", {"type": "subgoal"})


class TestMergeProjects(GoalTestCase):
    def _two_projects(self):
        root = self.goals.create("overgoal", "Health & Energy")
        area = self.goals.create("subgoal", "Nervous System", parent_id=root)
        self.goals._set_semantic_role(area, "area", rationale="Ongoing domain.")
        rage = self.goals.create("subgoal", "Map Rage Triggers", parent_id=area)
        dread = self.goals.create("subgoal", "Map Dread Sources", parent_id=area)
        for project in (rage, dread):
            self.goals._set_semantic_role(
                project, "project", rationale="Finite mapping effort.")
        return root, area, rage, dread

    def test_merge_moves_children_in_order_and_archives_the_husk(self):
        _root, _area, rage, dread = self._two_projects()
        keep_a = self.goals.create("task", "Choose tool", parent_id=rage)
        keep_b = self.goals.create("task", "Pattern analysis", parent_id=rage)
        stage = self.goals.create("subgoal", "Tracking phase", parent_id=dread)
        self.goals._set_semantic_role(stage, "stage", rationale="Groups tracking.")
        staged_leaf = self.goals.create("task", "Log task types", parent_id=stage)
        decide = self.goals.create("task", "Decide: batch or delegate",
                                   parent_id=dread)
        done = self.goals.create("task", "Pick log columns", parent_id=dread,
                                 status="completed")

        result = self.goals.merge_projects(dread, rage, rationale="One project.")

        self.assertEqual(result["moved"], 3)
        self.assertEqual(self.goals.get(dread)["status"], "archived")
        rows = self.goals.conn.execute(
            "SELECT id FROM goal_node WHERE parent_id=? AND status!='archived' "
            "ORDER BY position,id", (rage,)).fetchall()
        self.assertEqual([int(r["id"]) for r in rows],
                         [keep_a, keep_b, stage, decide, done])
        # The Stage's own child rides along unchanged.
        self.assertEqual(self.goals.get(staged_leaf)["parent_id"], stage)
        history = self.goals.conn.execute(
            "SELECT goal_id,old_parent_id,new_parent_id FROM "
            "goal_restructure_history ORDER BY id").fetchall()
        self.assertEqual(
            {(int(r["goal_id"]), int(r["old_parent_id"]), int(r["new_parent_id"]))
             for r in history},
            {(stage, dread, rage), (decide, dread, rage), (done, dread, rage)})

    def test_merge_is_reversible_for_the_husk_only(self):
        _root, _area, rage, dread = self._two_projects()
        moved = self.goals.create("task", "Decide: batch or delegate",
                                  parent_id=dread)
        self.goals.merge_projects(dread, rage)
        self.goals.restore_subtree(dread)
        self.assertEqual(self.goals.get(dread)["status"], "active")
        # The moved Leaf stays with the surviving project.
        self.assertEqual(self.goals.get(moved)["parent_id"], rage)

    def test_merge_validation_rejects_bad_pairs(self):
        _root, area, rage, dread = self._two_projects()
        with self.assertRaisesRegex(ValueError, "itself"):
            self.goals.validate_merge_projects(rage, rage)
        with self.assertRaisesRegex(ValueError, "not a Project"):
            self.goals.validate_merge_projects(area, rage)
        stage = self.goals.create("subgoal", "Tracking phase", parent_id=dread)
        self.goals._set_semantic_role(stage, "stage", rationale="Phase.")
        with self.assertRaisesRegex(ValueError, "not a Project"):
            self.goals.validate_merge_projects(stage, rage)
        self.goals.delete_subtree(dread)
        with self.assertRaisesRegex(ValueError, "missing or archived"):
            self.goals.validate_merge_projects(dread, rage)

    def test_merge_rejects_projects_that_contain_each_other(self):
        _root, _area, rage, dread = self._two_projects()
        # Simulate a legacy Project nested inside another Project. The role
        # API now rejects this placement, so write the role row directly.
        nested = self.goals.create("subgoal", "Nested effort", parent_id=rage)
        self.goals.conn.execute(
            "INSERT INTO goal_semantic_role (goal_id,role,updated_at) "
            "VALUES (?,?,datetime('now'))", (nested, "project"))
        self.goals.conn.commit()
        with self.assertRaisesRegex(ValueError, "contains the other"):
            self.goals.validate_merge_projects(nested, rage)
        with self.assertRaisesRegex(ValueError, "contains the other"):
            self.goals.validate_merge_projects(rage, nested)
        # An unrelated pair still validates.
        checked = self.goals.validate_merge_projects(dread, rage)
        self.assertEqual(int(checked["target"]["id"]), rage)


class TestReplanFlattensStages(GoalTestCase):
    def _project_with_stage(self):
        root = self.goals.create("overgoal", "Health & Energy")
        project = self.goals.create("subgoal", "Map Rage Triggers", parent_id=root)
        self.goals._set_semantic_role(project, "project", rationale="Finite effort.")
        direct = self.goals.create("task", "Choose tool", parent_id=project)
        stage = self.goals.create("subgoal", "Capture phase", parent_id=project)
        self.goals._set_semantic_role(stage, "stage", rationale="Groups capture.")
        under_stage = self.goals.create("task", "Log rage events", parent_id=stage)
        return root, project, direct, stage, under_stage

    def test_subtree_leaves_include_leaves_under_stages(self):
        _root, project, direct, _stage, under_stage = self._project_with_stage()
        leaf_ids = [int(leaf["id"])
                    for leaf in self.goals.project_subtree_leaves(project)]
        self.assertEqual(leaf_ids, [direct, under_stage])

    def test_replan_pulls_staged_leaves_up_and_archives_emptied_stage(self):
        _root, project, direct, stage, under_stage = self._project_with_stage()

        # A replan must account for every subtree Leaf, including the one under
        # the Stage. Keeping both pulls the staged Leaf up to a direct step.
        result = self.goals.apply_replan_project(project, [
            {"op": "keep", "leaf_id": direct},
            {"op": "rename", "leaf_id": under_stage,
             "new_title": "Run the 2-week log"},
        ])

        self.assertEqual(self.goals.get(under_stage)["parent_id"], project)
        self.assertEqual(self.goals.get(under_stage)["title"], "Run the 2-week log")
        self.assertEqual(self.goals.get(stage)["status"], "archived")
        self.assertIn(stage, result["archived_stage_ids"])
        direct_children = [int(r["id"]) for r in self.goals.conn.execute(
            "SELECT id FROM goal_node WHERE parent_id=? AND status!='archived' "
            "ORDER BY position,id", (project,)).fetchall()]
        self.assertEqual(direct_children, [direct, under_stage])

    def test_replan_referencing_only_direct_leaves_misses_staged_leaf(self):
        _root, project, direct, _stage, _under_stage = self._project_with_stage()
        with self.assertRaisesRegex(ValueError, "including those under its Stages"):
            self.goals.validate_replan_project(
                project, [{"op": "keep", "leaf_id": direct}])


if __name__ == "__main__":
    unittest.main()
