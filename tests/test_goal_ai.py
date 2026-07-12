import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from gui import GuiApi
from livingpc import crypto
from livingpc.config import Config
from livingpc.curiosity import CuriosityStore
from livingpc.goal_ai import (
    AgentProposal, AgentReport, ChatResult, GardeningProposal, GoalAgentStore,
    RelevanceReview, StubGoalAgentModel,
    build_agent_context, chat_with_goal_agent, decide_proposal, due_goal_nodes,
    decide_gardening_proposal, generate_goal_description, goal_relevance_view,
    parse_report, relevance_due_nodes, review_goal_relevance, start_goal_harvest,
    summarize_goal_answer,
)
from livingpc.goal_ai import (
    get_goal_agent_model, promote_memory_candidate, run_goal_agent,
    run_goal_subtree, run_goal_sweep,
)
from livingpc.goals import GoalStore
from livingpc.inference_scheduler import goal_ai_due
from livingpc.memory import MemoryStore


def cfg_for(directory):
    cfg = Config()
    cfg.memory_db_path = os.path.join(directory, "memory.db")
    cfg.db_path = os.path.join(directory, "events.db")
    cfg.goal_ai_backend = "stub"
    cfg.inference_backend = "stub"
    cfg.goal_ai_batch_size = 12
    return cfg


@pytest.fixture
def world():
    with tempfile.TemporaryDirectory() as directory:
        cfg = cfg_for(directory)
        curiosities = CuriosityStore(cfg.memory_db_path)
        goals = GoalStore(cfg.memory_db_path)
        over_a = goals.create("overgoal", "Korean", description="Become fluent")
        sub_a = goals.create("subgoal", "Grammar", parent_id=over_a)
        task_a = goals.create("task", "Practice particles", parent_id=sub_a)
        over_b = goals.create("overgoal", "Fitness", description="Build strength")
        task_b = goals.create("task", "Lift", parent_id=over_b)
        agents = GoalAgentStore(cfg.memory_db_path)
        yield cfg, goals, agents, curiosities, {
            "root": goals.root_id, "over_a": over_a, "sub_a": sub_a,
            "task_a": task_a, "over_b": over_b, "task_b": task_b,
        }
        agents.close(); goals.close(); curiosities.close()


class StaticRelevanceModel:
    model_name = "static-relevance"

    def __init__(self, review):
        self.review = review
        self.context = None
        self.evidence = None

    def review_relevance(self, context, evidence):
        self.context = context
        self.evidence = evidence
        return self.review


def gardening_review(node_id, proposal_type, payload=None, *, evidence_refs=None,
                     state="questionable"):
    refs = list(evidence_refs or [f"node:{node_id}"])
    return RelevanceReview(
        state, .42 if state != "current" else .9, .84,
        "New evidence suggests this node deserves a deliberate relevance check.",
        "The current framing may no longer match what is being learned.",
        "The original direction still contains something useful.", refs,
        [GardeningProposal(
            proposal_type, node_id, dict(payload or {}),
            "This is a proposal only; the user should decide.", refs)])


def test_task_context_has_ancestor_intent_but_no_siblings(world):
    _, goals, agents, _, ids = world
    context = build_agent_context(goals, agents, ids["task_a"])
    text = json.dumps(context)
    assert [x["title"] for x in context["ancestor_intent"]] == [
        "Actualized Self", "Korean", "Grammar"]
    assert "Fitness" not in text
    assert "Lift" not in text
    assert context["subtree"]["children"] == []


def test_agent_context_includes_durable_origin_summary(world):
    _, goals, agents, _, ids = world
    goals.set_origin(
        ids["sub_a"],
        source_kind="investigation",
        source_id=12,
        source_proposal_id=4,
        source_label="Study method",
        summary="Created from Investigation “Study method”.",
        detail="Original investigation answers.",
    )
    context = build_agent_context(goals, agents, ids["sub_a"])
    assert context["node"]["origin"]["source_label"] == "Study method"
    assert "Created from Investigation" in context["node"]["origin"]["summary"]
    assert context["subtree"]["origin"]["detail"] == "Original investigation answers."


def test_parent_context_consumes_child_agent_report(world):
    _, goals, agents, _, ids = world
    agents.save_report(ids["task_a"], AgentReport(
        "Particle practice is blocked.", "blocked", .8, blockers=["No examples"]),
        "hash", "stub")
    context = build_agent_context(goals, agents, ids["sub_a"])
    child = context["subtree"]["children"][0]
    assert child["agent_report"]["health"] == "blocked"
    assert child["agent_report"]["brief"] == "Particle practice is blocked."


def test_overview_exposes_navigation_queues_without_private_text(world):
    _, _, agents, _, ids = world
    agents.save_report(ids["task_a"], AgentReport(
        "Private blocked brief", "blocked", .8,
        questions=["Private question text"]), "hash-a", "stub")
    agents.save_report(ids["sub_a"], AgentReport(
        "Private attention brief", "needs-attention", .7,
        proposals=[AgentProposal("pause", ids["sub_a"], {}, "Private rationale")]),
        "hash-b", "stub")
    overview = agents.overview()
    assert {x["node_id"] for x in overview["queues"]["blocked"]} == {ids["task_a"]}
    assert {x["node_id"] for x in overview["queues"]["needs_attention"]} == {ids["sub_a"]}
    assert overview["queues"]["questions"][0]["node_id"] == ids["task_a"]
    assert overview["queues"]["proposals"][0]["node_id"] == ids["sub_a"]
    assert "Private" not in json.dumps(overview)


def test_leaf_harvest_flows_upward_without_leaking_to_sibling(world):
    cfg, goals, agents, _, ids = world
    harvest = start_goal_harvest(cfg, ids["task_a"], model=StubGoalAgentModel())
    agents.commit_harvest(harvest["id"])
    soul_context = build_agent_context(goals, agents, ids["root"])
    fitness_context = build_agent_context(goals, agents, ids["over_b"])
    assert any(h["source_node_id"] == ids["task_a"]
               for h in soul_context["committed_harvests"])
    assert all(h["source_node_id"] != ids["task_a"]
               for h in fitness_context["committed_harvests"])


def test_only_soul_harvest_routes_crossover_downward(world):
    _, goals, agents, _, ids = world
    draft = {"summary": "Use tiny drills when activation is low.",
             "insights": [{"title": "Tiny drills", "detail": "Shrink the start.",
                            "kind": "method"},
                           {"title": "Private Korean detail",
                            "detail": "This should not route to Fitness.", "kind": "lesson"}],
             "routes": [{"target_node_id": ids["over_b"], "insight_indexes": [0],
                         "reason": "Useful for exercise activation"}]}
    soul = agents.create_harvest(ids["root"], draft)
    agents.commit_harvest(soul["id"])
    fitness_leaf = build_agent_context(goals, agents, ids["task_b"])
    korean = build_agent_context(goals, agents, ids["over_a"])
    assert any(h["id"] == soul["id"] for h in fitness_leaf["committed_harvests"])
    routed = next(h for h in fitness_leaf["committed_harvests"] if h["id"] == soul["id"])
    assert [i["title"] for i in routed["insights"]] == ["Tiny drills"]
    assert all(h["id"] != soul["id"] for h in korean["committed_harvests"])

    lower = agents.create_harvest(ids["task_a"], draft)
    agents.commit_harvest(lower["id"])
    routes = agents.conn.execute(
        "SELECT COUNT(*) FROM goal_harvest_route WHERE harvest_id=?", (lower["id"],)
    ).fetchone()[0]
    assert routes == 0


def test_context_uses_attached_curiosity_but_not_global_memory(world):
    cfg, goals, agents, curiosities, ids = world
    cid = curiosities.add_curiosity("find a Korean study method", "Study method")
    curiosities.add_item(cid, "question", "When do you study best?")
    goals.link_curiosity(ids["over_a"], cid)
    mem = MemoryStore(cfg.memory_db_path)
    mem.add("unrelated", "secret", "global memory must not appear")
    mem.upsert_core_profile_fact(
        "Current Reality", "current work situation",
        "Core profile basics should appear in every GoalAI context.",
        priority=100,
    )
    mem.close()
    context = build_agent_context(goals, agents, ids["over_a"])
    text = json.dumps(context)
    assert "find a Korean study method" in text
    assert "Core profile basics should appear" in text
    assert "global memory must not appear" not in text


def test_report_writes_agent_metadata_without_mutating_goal(world):
    cfg, goals, agents, _, ids = world
    before = goals.get(ids["task_a"])
    result = run_goal_agent(cfg, ids["task_a"], model=StubGoalAgentModel())
    after = goals.get(ids["task_a"])
    assert result["ok"]
    assert before == after
    assert agents.state(ids["task_a"])["last_run_at"] is not None
    assert agents.state(ids["sub_a"])["dirty"] is True


class ProposalModel:
    model_name = "proposal-model"

    def assess(self, context, role):
        node_id = context["node"]["id"]
        return AgentReport(
            "A next step is available.", "needs-attention", .8,
            proposals=[
                AgentProposal("create_child", node_id,
                              {"type": "task", "title": "Review examples"}, "Useful next step"),
                AgentProposal("request_evidence", node_id,
                              {"question": "What happened?"}, "Need evidence"),
                AgentProposal("update_fields", node_id,
                              {"priority": "high"}, "Raise priority"),
                AgentProposal("pause", node_id, {}, "Extra proposal beyond cap"),
            ])


def test_proposal_cap_and_deduplication(world):
    cfg, _, agents, _, ids = world
    cfg.goal_ai_max_open_proposals = 3
    run_goal_agent(cfg, ids["sub_a"], model=ProposalModel())
    assert len(agents.proposals(ids["sub_a"])) == 3
    agents.mark_dirty(ids["sub_a"], ancestors=False)
    run_goal_agent(cfg, ids["sub_a"], model=ProposalModel())
    assert len(agents.proposals(ids["sub_a"])) == 3


def test_agent_cannot_propose_into_unrelated_branch(world):
    _, _, agents, _, ids = world
    created = agents.add_proposal(ids["sub_a"], AgentProposal(
        "update_fields", ids["over_b"], {"priority": "high"}, "Wrong branch"))
    assert created is None


def test_promote_insight_is_confidence_gated_and_upward_only(world):
    _, _, agents, _, ids = world
    report = parse_report(json.dumps({
        "brief": "Practice revealed a reusable start constraint.",
        "health": "needs-attention",
        "confidence": .9,
        "evidence": [],
        "blockers": [],
        "next_focus": "Use the insight elsewhere if approved.",
        "questions": [],
        "proposals": [
            {"type": "promote_insight", "target_node_id": ids["over_a"],
             "payload": {"summary": "Tiny starts reduce avoidance.",
                         "title": "Tiny starts",
                         "detail": "Starting with a tiny drill makes practice easier.",
                         "kind": "method", "confidence": .86},
             "rationale": "This applies above the current Branch."},
            {"type": "promote_insight", "target_node_id": ids["over_a"],
             "payload": {"summary": "Maybe useful.",
                         "detail": "This is too uncertain to promote.",
                         "confidence": .62},
             "rationale": "Below the gate."},
        ],
    }), ids["sub_a"])
    assert report is not None
    assert len(report.proposals) == 1
    assert report.proposals[0].proposal_type == "promote_insight"

    assert agents.add_proposal(ids["sub_a"], AgentProposal(
        "promote_insight", ids["over_b"],
        {"summary": "Wrong branch", "detail": "Should not cross sideways.", "confidence": .95},
        "Unrelated branch")) is None


def test_approved_promote_insight_commits_upward_harvest(world):
    cfg, goals, agents, _, ids = world
    proposal_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "promote_insight", ids["over_a"],
        {"summary": "Tiny starts reduce avoidance.",
         "title": "Tiny starts",
         "detail": "Starting with a tiny drill makes practice easier.",
         "kind": "method",
         "confidence": .9},
        "I am 90% confident this should move up to Korean."))
    result = decide_proposal(cfg, proposal_id, "approve")
    assert result["ok"]
    assert result["harvest_id"]

    root_context = build_agent_context(goals, agents, ids["over_a"])
    sibling_context = build_agent_context(goals, agents, ids["over_b"])
    promoted = [h for h in root_context["committed_harvests"]
                if h["source_node_id"] == ids["sub_a"]]
    assert promoted
    assert promoted[0]["promotion"]["confidence"] == .9
    assert promoted[0]["insights"][0]["title"] == "Tiny starts"
    assert all(h["source_node_id"] != ids["sub_a"]
               for h in sibling_context["committed_harvests"])


def test_approve_proposal_applies_only_after_user_action(world):
    cfg, goals, agents, _, ids = world
    proposal_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "create_child", ids["sub_a"], {"type": "task", "title": "Review examples"},
        "Useful"))
    assert not any(c["title"] == "Review examples" for c in
                   next(x for x in goals.tree()["children"] if x["id"] == ids["over_a"])["children"][0]["children"])
    result = decide_proposal(cfg, proposal_id, "approve")
    assert result["ok"]
    sub = next(x for x in goals.tree()["children"] if x["id"] == ids["over_a"])["children"][0]
    assert any(c["title"] == "Review examples" for c in sub["children"])


def test_relevance_review_is_versioned_and_tree_mutation_needs_second_approval(world):
    cfg, goals, agents, _, ids = world
    original = goals.get(ids["sub_a"])
    model = StaticRelevanceModel(gardening_review(
        ids["sub_a"], "rewrite",
        {"title": "Use grammar in conversation",
         "description": "Practice grammar through real exchanges."}))
    result = review_goal_relevance(cfg, ids["sub_a"], model=model)
    assert result["proposals_created"] == 1
    assert goals.get(ids["sub_a"])["title"] == original["title"]
    view = goal_relevance_view(goals, agents, ids["sub_a"])
    assert view["state"]["relevance_state"] == "questionable"
    assert len(view["reviews"]) == 1
    proposal = view["proposals"][0]
    assert proposal["payload"]["title"] == "Use grammar in conversation"

    applied = decide_gardening_proposal(cfg, proposal["id"], "approve")
    assert applied["ok"]
    assert goals.get(ids["sub_a"])["title"] == "Use grammar in conversation"
    history = agents.gardening_proposals(ids["sub_a"], status=None)
    assert history[0]["status"] == "approved"
    assert original["title"] == "Grammar"


def test_gardening_discards_mutation_with_fabricated_evidence_reference(world):
    cfg, goals, agents, _, ids = world
    review = gardening_review(
        ids["task_a"], "archive", {}, evidence_refs=["invented:999"],
        state="outgrown")
    result = review_goal_relevance(
        cfg, ids["task_a"], model=StaticRelevanceModel(review))
    assert result["proposals_created"] == 0
    assert goals.get(ids["task_a"])["status"] == "active"
    assert agents.relevance_reviews(ids["task_a"])[0]["relevance_state"] == "outgrown"


def test_stale_gardening_proposal_cannot_overwrite_newer_user_change(world):
    cfg, goals, agents, _, ids = world
    review = gardening_review(
        ids["sub_a"], "rewrite", {"title": "Model wording"})
    result = review_goal_relevance(cfg, ids["sub_a"], model=StaticRelevanceModel(review))
    proposal_id = result["proposal_ids"][0]
    goals.update(ids["sub_a"], title="User changed this first")
    with pytest.raises(ValueError, match="changed since"):
        decide_gardening_proposal(cfg, proposal_id, "approve")
    assert goals.get(ids["sub_a"])["title"] == "User changed this first"
    assert agents.get_gardening_proposal(proposal_id)["status"] == "stale"


def test_relevance_becomes_due_only_after_new_evidence(world):
    cfg, goals, agents, _, ids = world
    first = review_goal_relevance(cfg, ids["sub_a"], model=StubGoalAgentModel())
    assert first["view"]["due"] is False
    assert ids["sub_a"] not in {item["node_id"] for item in relevance_due_nodes(goals, agents)}
    goals.add_evidence(ids["sub_a"], "manual_note", "new-1",
                       "I no longer want grammar drills by themselves.")
    due = goal_relevance_view(goals, agents, ids["sub_a"])
    assert due["due"] is True
    assert due["new_evidence"][0]["ref"].startswith("goal_evidence:")
    assert "newer evidence" in due["due_reason"]
    reviewed = review_goal_relevance(cfg, ids["sub_a"], model=StubGoalAgentModel())
    assert reviewed["view"]["due"] is False


def test_active_goal_gets_a_gentle_monthly_check_after_no_meaningful_movement(world):
    _, goals, agents, _, ids = world
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    old = (now - timedelta(days=40)).isoformat()
    goals.conn.execute(
        "UPDATE goal_node SET created_at=?,updated_at=? WHERE id IN (?,?)",
        (old, old, ids["sub_a"], ids["task_a"]))
    goals.conn.commit()
    view = goal_relevance_view(
        goals, agents, ids["sub_a"], stale_days=30, now=now)
    assert view["due"] is True and view["due_kind"] == "quiet"
    assert "may still matter" in view["due_reason"]

    goals.conn.execute(
        "UPDATE goal_node SET updated_at=? WHERE id=?",
        ((now - timedelta(days=2)).isoformat(), ids["task_a"]))
    goals.conn.commit()
    assert goal_relevance_view(
        goals, agents, ids["sub_a"], stale_days=30, now=now)["due"] is False

    goals.conn.execute(
        "UPDATE goal_node SET status='paused',updated_at=? WHERE id=?",
        (old, ids["sub_a"]))
    goals.conn.execute("UPDATE goal_node SET updated_at=? WHERE id=?", (old, ids["task_a"]))
    goals.conn.commit()
    assert goal_relevance_view(
        goals, agents, ids["sub_a"], stale_days=30, now=now)["due"] is False


def test_split_proposal_rewrites_first_part_and_creates_sibling(world):
    cfg, goals, agents, _, ids = world
    review = gardening_review(ids["sub_a"], "split", {"parts": [
        {"title": "Grammar recognition", "description": "Notice patterns."},
        {"title": "Grammar production", "description": "Use patterns aloud."},
    ]})
    result = review_goal_relevance(cfg, ids["sub_a"], model=StaticRelevanceModel(review))
    proposal_id = result["proposal_ids"][0]
    assert not any(node["title"] == "Grammar production" for node in goals.catalog())
    decide_gardening_proposal(cfg, proposal_id, "approve")
    assert goals.get(ids["sub_a"])["title"] == "Grammar recognition"
    assert any(node["title"] == "Grammar production" for node in goals.catalog())
    assert goal_relevance_view(goals, agents, ids["sub_a"])["due"] is False


def test_merge_moves_children_and_archives_source_without_deleting_history(world):
    cfg, goals, _, _, ids = world
    review = gardening_review(ids["over_a"], "merge", {
        "source_node_ids": [ids["over_b"]],
        "title": "Language and embodied confidence",
        "description": "One current direction that absorbs both roots.",
    })
    result = review_goal_relevance(cfg, ids["over_a"], model=StaticRelevanceModel(review))
    proposal_id = result["proposal_ids"][0]
    assert goals.get(ids["over_b"])["status"] == "active"
    decide_gardening_proposal(cfg, proposal_id, "approve")
    assert goals.get(ids["over_b"])["status"] == "archived"
    assert goals.get(ids["task_b"])["parent_id"] == ids["over_a"]
    assert goals.get(ids["over_b"])["title"] == "Fitness"
    assert goals.get(ids["over_a"])["title"] == "Language and embodied confidence"


@pytest.mark.parametrize("proposal_type,expected_status", [
    ("pause", "paused"), ("archive", "archived")])
def test_pause_and_archive_are_inert_until_gardening_approval(
        world, proposal_type, expected_status):
    cfg, goals, _, _, ids = world
    node_id = goals.create("task", f"Maybe {proposal_type}", parent_id=ids["sub_a"])
    review = gardening_review(node_id, proposal_type, {},
                              state="outgrown" if proposal_type == "archive" else "questionable")
    result = review_goal_relevance(cfg, node_id, model=StaticRelevanceModel(review))
    assert goals.get(node_id)["status"] == "active"
    decide_gardening_proposal(cfg, result["proposal_ids"][0], "approve")
    assert goals.get(node_id)["status"] == expected_status


def test_attach_evidence_and_leave_unchanged_are_reviewable_proposals(world):
    cfg, goals, agents, _, ids = world
    attach = gardening_review(ids["sub_a"], "attach_evidence", {
        "source_kind": "curiosity_synthesis", "source_id": "44",
        "label": "Approved synthesis about conversational practice",
    })
    result = review_goal_relevance(cfg, ids["sub_a"], model=StaticRelevanceModel(attach))
    decide_gardening_proposal(cfg, result["proposal_ids"][0], "approve")
    linked = goals.conn.execute(
        "SELECT source_kind,source_id FROM goal_evidence_link WHERE goal_id=?",
        (ids["sub_a"],)).fetchall()
    assert any(row["source_kind"] == "curiosity_synthesis" and row["source_id"] == "44"
               for row in linked)

    unchanged = gardening_review(ids["sub_a"], "leave_unchanged", {}, state="current")
    result = review_goal_relevance(cfg, ids["sub_a"], model=StaticRelevanceModel(unchanged))
    before = goals.get(ids["sub_a"])
    decide_gardening_proposal(cfg, result["proposal_ids"][0], "approve")
    after = goals.get(ids["sub_a"])
    assert before["title"] == after["title"] and before["status"] == after["status"]
    assert agents.gardening_proposals(ids["sub_a"], status=None)[0]["status"] == "approved"


def test_medium_priority_and_soul_names_are_normalized_before_commit(world):
    cfg, goals, agents, _, ids = world
    proposal_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "create_child", ids["sub_a"],
        {"type": "leaf", "title": "Small drill", "priority": "medium"}, "Useful"))
    assert decide_proposal(cfg, proposal_id, "approve")["ok"]
    child = next(c for c in goals.tree()["children"][0]["children"][0]["children"]
                 if c["title"] == "Small drill")
    assert child["type"] == "task" and child["priority"] == "normal"


def test_update_fields_proposal_can_commit_notes(world):
    cfg, goals, agents, _, ids = world
    proposal_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "update_fields", ids["sub_a"], {"notes": "Keep this scoped."}, "Remember scope"))
    assert decide_proposal(cfg, proposal_id, "approve")["ok"]
    assert goals.get(ids["sub_a"])["notes"] == "Keep this scoped."


def test_stale_proposal_is_not_applied(world):
    cfg, goals, agents, _, ids = world
    proposal_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "update_fields", ids["sub_a"], {"priority": "high"}, "Raise priority"))
    goals.update(ids["sub_a"], description="Changed after proposal")
    with pytest.raises(ValueError, match="changed since"):
        decide_proposal(cfg, proposal_id, "approve")
    assert goals.get(ids["sub_a"])["priority"] == "normal"
    assert agents.get_proposal(proposal_id)["status"] == "stale"


def test_dismissed_proposal_suppresses_repeat(world):
    cfg, _, agents, _, ids = world
    proposal = AgentProposal("pause", ids["sub_a"], {}, "Pause it")
    proposal_id = agents.add_proposal(ids["sub_a"], proposal)
    decide_proposal(cfg, proposal_id, "dismiss")
    assert agents.add_proposal(ids["sub_a"], proposal) is None


def test_dismissed_goal_ai_items_can_be_reopened(world):
    cfg, _, agents, _, ids = world
    agents.save_report(ids["task_a"], AgentReport(
        "Need input", "unknown", .4, questions=["What did you try?"]), "h", "stub")
    question = agents.questions(ids["task_a"])[0]
    agents.dismiss_question(question["id"])
    assert agents.reopen_question(question["id"]) == ids["task_a"]
    assert agents.questions(ids["task_a"])[0]["status"] == "open"

    proposal_id = agents.add_proposal(
        ids["sub_a"], AgentProposal("pause", ids["sub_a"], {}, "Pause it"))
    decide_proposal(cfg, proposal_id, "dismiss")
    reopened = decide_proposal(cfg, proposal_id, "reopen")
    assert reopened["ok"] and agents.get_proposal(proposal_id)["status"] == "open"

    candidate_id = agents.add_memory_candidate(ids["sub_a"], {
        "category": "goals", "attribute": "accomplishment", "value": "Finished a practice run"})
    assert promote_memory_candidate(cfg, candidate_id, "dismiss")["ok"]
    restored = promote_memory_candidate(cfg, candidate_id, "reopen")
    assert restored["ok"]
    assert agents.memory_candidates(ids["sub_a"])[0]["id"] == candidate_id


def test_question_answer_stays_local_and_dirties_ancestors(world):
    cfg, goals, agents, _, ids = world
    agents.save_report(ids["task_a"], AgentReport(
        "Need input", "unknown", .4, questions=["What did you try?"]), "h", "stub")
    question = agents.questions(ids["task_a"])[0]
    mem = MemoryStore(cfg.memory_db_path)
    before = len(mem.active())
    mem.close()
    agents.answer_question(question["id"], "I practiced five examples.")
    mem = MemoryStore(cfg.memory_db_path)
    assert len(mem.active()) == before
    mem.close()
    task = next(x for x in goals.tree()["children"] if x["id"] == ids["over_a"])["children"][0]["children"][0]
    assert task["evidence"][0]["label"] == "I practiced five examples."
    assert agents.state(ids["over_a"])["dirty"] is True


def test_long_answer_keeps_exact_text_but_displays_bullet_summary(world):
    cfg, goals, agents, _, ids = world
    agents.save_report(ids["task_a"], AgentReport(
        "Need context", "unknown", .4, questions=["What is happening?"]), "h", "stub")
    question = agents.questions(ids["task_a"])[0]
    exact = "I cannot leave until I find another job. " + "The role requires automation work. " * 40
    summary = summarize_goal_answer(cfg, ids["task_a"], exact, model=StubGoalAgentModel())
    agents.answer_question(question["id"], exact, summary)
    stored = agents.questions(ids["task_a"], include_resolved=True)[0]
    assert stored["answer"] == exact.strip()
    task = next(x for x in goals.tree()["children"] if x["id"] == ids["over_a"])["children"][0]["children"][0]
    assert task["evidence"][0]["label"].startswith("• ")
    assert len(task["evidence"][0]["label"]) < len(exact)
    bounded = build_agent_context(goals, agents, ids["task_a"], max_chars=5000)
    assert len(json.dumps(bounded, ensure_ascii=False, sort_keys=True)) <= 5000


def test_generated_description_is_a_draft_not_an_automatic_mutation(world):
    cfg, goals, _, _, ids = world
    before = goals.get(ids["task_a"])["description"]
    draft = generate_goal_description(cfg, ids["task_a"], model=StubGoalAgentModel())
    assert "Leaf" in draft
    assert goals.get(ids["task_a"])["description"] == before == ""


class FailingModel:
    model_name = "failing"
    def assess(self, context, role):
        raise RuntimeError("boom")


def test_model_failure_preserves_prior_report(world):
    cfg, _, agents, _, ids = world
    run_goal_agent(cfg, ids["task_a"], model=StubGoalAgentModel())
    brief = agents.state(ids["task_a"])["brief"]
    with pytest.raises(RuntimeError):
        run_goal_agent(cfg, ids["task_a"], model=FailingModel())
    state = agents.state(ids["task_a"])
    assert state["brief"] == brief
    assert state["dirty"] is True
    assert state["last_error_at"] is not None


def test_dirty_propagates_after_goal_mutation(world):
    _, goals, agents, _, ids = world
    agents.save_report(ids["task_a"], AgentReport("Okay", "on-track", .8), "h", "stub")
    agents.save_report(ids["sub_a"], AgentReport("Okay", "on-track", .8), "h2", "stub")
    agents.save_report(ids["over_a"], AgentReport("Okay", "on-track", .8), "h3", "stub")
    goals.update(ids["task_a"], notes="new local evidence")
    assert agents.state(ids["task_a"])["dirty"] is True
    assert agents.state(ids["sub_a"])["dirty"] is True
    assert agents.state(ids["over_a"])["dirty"] is True


def test_elapsed_time_alone_never_makes_clean_nodes_due(world):
    cfg, _, _, _, _ = world
    now = datetime(2026, 7, 1, 20, tzinfo=timezone.utc)
    run_goal_sweep(cfg, now=now)
    assert due_goal_nodes(cfg, now=now + timedelta(days=10)) == []


def test_due_date_boundary_dirties_node_and_ancestors_once(world):
    cfg, goals, agents, _, ids = world
    now = datetime(2026, 7, 1, 20, tzinfo=timezone.utc)
    goals.update(ids["task_a"], due_date="2026-07-05")
    run_goal_sweep(cfg, now=now)
    assert due_goal_nodes(cfg, now=now) == []
    due = due_goal_nodes(cfg, now=now + timedelta(days=1))
    assert ids["task_a"] in due
    assert ids["sub_a"] in due
    assert agents.state(ids["task_a"])["dirty_reason"] == "date became due soon"


def test_scheduler_due_order_is_bottom_up_and_skips_dormant(world):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_b"], status="paused")
    due = due_goal_nodes(cfg, now=datetime.now(timezone.utc))
    assert ids["task_b"] not in due
    assert due.index(ids["task_a"]) < due.index(ids["sub_a"])
    assert due.index(ids["sub_a"]) < due.index(ids["over_a"])
    assert due.index(ids["over_a"]) < due.index(ids["root"])


def test_batch_limit_and_sweep(world):
    cfg, _, _, _, _ = world
    cfg.goal_ai_batch_size = 2
    assert len(due_goal_nodes(cfg)) == 2
    result = run_goal_sweep(cfg)
    assert result["reviewed"] == 2
    assert result["failures"] == 0


def test_manual_subtree_runs_bottom_up(world):
    cfg, _, agents, _, ids = world
    result = run_goal_subtree(cfg, ids["over_a"], models={
        "task": StubGoalAgentModel(), "subgoal": StubGoalAgentModel(),
        "overgoal": StubGoalAgentModel()})
    assert [r["node_id"] for r in result["results"]] == [
        ids["task_a"], ids["sub_a"], ids["over_a"]]
    assert agents.state(ids["over_a"])["dirty"] is False


class ChatModel:
    model_name = "chat-model"
    def chat(self, context, messages):
        return ChatResult(
            "That accomplishment can be saved after review.",
            memory_candidate={"category": "Korean", "attribute": "accomplishment",
                              "value": "Used particles correctly in five sentences.",
                              "source_text": messages[-1]["content"]})


def test_chat_persists_and_memory_requires_explicit_approval(world):
    cfg, _, agents, _, ids = world
    result = chat_with_goal_agent(
        cfg, ids["sub_a"], "Save my accomplishment to memory", model=ChatModel())
    assert len(result["view"]["messages"]) == 2
    candidate_id = result["memory_candidate_id"]
    mem = MemoryStore(cfg.memory_db_path)
    assert mem.active() == []
    mem.close()
    saved = promote_memory_candidate(cfg, candidate_id, "save")
    assert saved["status"] == "saved"
    mem = MemoryStore(cfg.memory_db_path)
    assert mem.active_as_dicts()[0]["value"] == "Used particles correctly in five sentences."
    mem.close()


def test_forgetting_promoted_memory_removes_goal_ai_candidate(world):
    from livingpc.forget import forget_memory
    cfg, _, agents, _, ids = world
    candidate_id = agents.add_memory_candidate(ids["sub_a"], {
        "category": "Korean", "attribute": "accomplishment",
        "value": "Private accomplishment", "source_text": "Private accomplishment"})
    saved = promote_memory_candidate(cfg, candidate_id, "save")
    cfg.notion_sync_enabled = False
    result = forget_memory(cfg, saved["memory_id"], purge_backups=False,
                           sync_notion=False)
    assert result["goal_ai_candidates_removed"] == 1
    assert agents.conn.execute(
        "SELECT 1 FROM goal_agent_memory_candidate WHERE id=?", (candidate_id,)).fetchone() is None


def test_tiered_model_selection_uses_stub_in_tests(world):
    cfg, _, _, _, _ = world
    assert isinstance(get_goal_agent_model(cfg, "task"), StubGoalAgentModel)
    assert isinstance(get_goal_agent_model(cfg, "umbrella"), StubGoalAgentModel)


def test_goal_ai_due_cadence():
    now = datetime.now(timezone.utc)
    assert goal_ai_due(now, None, interval_seconds=14400)
    assert not goal_ai_due(now, now - timedelta(hours=1), interval_seconds=14400)
    assert goal_ai_due(now, now - timedelta(hours=4), interval_seconds=14400)


def test_goal_ai_bridge_round_trip(world):
    cfg, _, _, _, ids = world
    api = GuiApi(cfg)
    state = api.goal_ai_state(ids["task_a"])
    assert state["ok"] and state["agent"]["state"]["health"] == "unknown"
    reviewed = api.goal_ai_review(ids["task_a"])
    assert reviewed["ok"]
    chatted = api.goal_ai_chat(ids["task_a"], "What should I do?")
    assert chatted["ok"]


def test_tree_gardening_bridge_round_trip(world):
    cfg, _, _, _, ids = world
    api = GuiApi(cfg)
    state = api.goal_state()
    node = next(child for root in state["tree"]["children"]
                for child in root.get("children", []) if child["id"] == ids["sub_a"])
    assert "relevance" in node
    reviewed = api.goal_relevance_review(ids["sub_a"])
    assert reviewed["ok"]
    state = api.goal_state()
    node = next(child for root in state["tree"]["children"]
                for child in root.get("children", []) if child["id"] == ids["sub_a"])
    proposals = node["relevance"]["proposals"]
    assert len(proposals) == 1 and proposals[0]["type"] == "leave_unchanged"
    decided = api.goal_gardening_proposal(proposals[0]["id"], "approve")
    assert decided["ok"] and decided["proposal_type"] == "leave_unchanged"


def test_goal_ai_state_read_does_not_require_or_create_agent_row():
    with tempfile.TemporaryDirectory() as folder:
        cfg = Config(memory_db_path=os.path.join(folder, "memory.db"),
                     db_path=os.path.join(folder, "events.db"),
                     goal_ai_backend="stub")
        goals = GoalStore(cfg.memory_db_path)
        try:
            root = goals.create("overgoal", "Korean")
        finally:
            goals.close()

        api = GuiApi(cfg)
        state = api.goal_ai_state(root)
        assert state["ok"]
        assert state["agent"]["state"]["health"] == "unknown"
        assert state["agent"]["state"]["dirty"] is True

        conn = sqlite3.connect(cfg.memory_db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM goal_agent_state").fetchone()[0] == 0
        finally:
            conn.close()


def test_sensitive_goal_ai_fields_are_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVINGPC_DB_KEY", "goal-ai-test-key")
    monkeypatch.setenv("LIVINGPC_SALT_FILE", str(tmp_path / "salt"))
    crypto._fernet.cache_clear()
    db = str(tmp_path / "encrypted.db")
    curiosities = CuriosityStore(db)
    goals = GoalStore(db)
    node = goals.create("overgoal", "Private goal")
    agents = GoalAgentStore(db)
    agents.save_report(node, AgentReport(
        "Private brief", "blocked", .8, ["Private evidence"], ["Private blocker"],
        "Private next focus", ["Private question?"]), "hash", "stub")
    agents.add_message(node, "user", "Private chat")
    agents.add_memory_candidate(node, {
        "category": "Private category", "attribute": "Private attribute",
        "value": "Private accomplishment", "source_text": "Private source"})
    agents.save_relevance_review(
        node, RelevanceReview(
            "questionable", .4, .8, "Private relevance rationale",
            "Private change", "Private useful remainder", [f"node:{node}"],
            [GardeningProposal(
                "rewrite", node, {"title": "Private rewritten goal"},
                "Private proposal rationale", [f"node:{node}"])]),
        "private-hash", "stub", allowed_evidence_refs={f"node:{node}"})
    raw_state = agents.conn.execute(
        "SELECT brief,evidence_summary,blockers,next_focus FROM goal_agent_state WHERE node_id=?",
        (node,)).fetchone()
    raw_question = agents.conn.execute(
        "SELECT text FROM goal_agent_question WHERE node_id=?", (node,)).fetchone()[0]
    raw_message = agents.conn.execute(
        "SELECT content FROM goal_agent_message WHERE node_id=?", (node,)).fetchone()[0]
    raw_candidate = agents.conn.execute(
        "SELECT value FROM goal_agent_memory_candidate WHERE node_id=?", (node,)).fetchone()[0]
    raw_relevance = agents.conn.execute(
        "SELECT rationale,what_changed,evidence_refs FROM goal_relevance_state WHERE node_id=?",
        (node,)).fetchone()
    raw_review = agents.conn.execute(
        "SELECT review_json FROM goal_relevance_review WHERE node_id=?", (node,)).fetchone()[0]
    raw_gardening = agents.conn.execute(
        "SELECT payload_json,rationale,evidence_refs FROM goal_gardening_proposal "
        "WHERE target_node_id=?", (node,)).fetchone()
    assert all(crypto.is_encrypted(value) for value in [*raw_state, raw_question, raw_message,
                                                        raw_candidate, *raw_relevance,
                                                        raw_review, *raw_gardening])
    agents.close(); goals.close(); curiosities.close()
    crypto._fernet.cache_clear()


def test_request_evidence_and_start_curiosity_proposals(world):
    cfg, goals, agents, curiosities, ids = world
    request_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "request_evidence", ids["sub_a"], {"question": "What did practice reveal?"},
        "Need direct evidence"))
    assert decide_proposal(cfg, request_id, "approve")["ok"]
    assert agents.questions(ids["sub_a"])[0]["text"] == "What did practice reveal?"
    curiosity_id = agents.add_proposal(ids["over_a"], AgentProposal(
        "start_curiosity", ids["over_a"],
        {"directive": "Find the best Korean review rhythm", "label": "Review rhythm"},
        "This needs investigation"))
    assert decide_proposal(cfg, curiosity_id, "approve")["ok"]
    assert any(c["label"] == "Review rhythm" for c in goals.tree()["children"][0]["curiosities"])


def test_scheduler_sends_one_digest_for_blocked_and_proposals(tmp_path, monkeypatch):
    from livingpc import goal_ai as goal_ai_module
    from livingpc import notify as notify_module
    from livingpc.inference_scheduler import InferenceScheduler

    cfg = cfg_for(str(tmp_path))
    cfg.goal_ai_notifications = True
    cfg.reflection_quiet_start_hour = 0
    cfg.reflection_quiet_end_hour = 0
    calls = []
    monkeypatch.setattr(goal_ai_module, "run_goal_sweep", lambda _cfg: {
        "reviewed": 4, "failures": 0, "proposals_created": 2,
        "became_blocked": 1, "results": []})
    monkeypatch.setattr(notify_module, "notify", lambda title, message, cfg=None:
                        calls.append((title, message)) or True)
    scheduler = InferenceScheduler(cfg)
    assert scheduler._run_goal_ai_once()
    assert len(calls) == 1
    assert "1 newly blocked" in calls[0][1]
    assert "2 new proposal" in calls[0][1]
