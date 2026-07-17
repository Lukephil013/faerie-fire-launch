import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from gui import GuiApi
from livingpc import crypto
from livingpc.config import Config
from livingpc.context_attachment import ContextAttachmentStore
from livingpc.curiosity import CuriosityStore
from livingpc.goal_ai import (
    AgentProposal, AgentReport, ChatResult, ClaudeGoalAgentModel, GardeningProposal, GoalAgentStore,
    LEAF_WORKSPACE_SYSTEM, STEP_COACH_SYSTEM, LeafStepDraft, LeafWorkspaceReply,
    RelevanceReview, StubGoalAgentModel,
    StepCoachReply, build_agent_context, build_leaf_step_draft_context,
    build_leaf_workspace_context, build_step_coach_context,
    chat_with_goal_agent, confirm_step_coach_completion, decide_proposal,
    decide_step_coach_revision, due_goal_nodes,
    decide_gardening_proposal, draft_leaf_steps, generate_goal_description, goal_relevance_view,
    open_step_coach, parse_leaf_step_draft, parse_report, parse_step_coach, relevance_due_nodes, review_goal_relevance,
    propose_leaf_boundary_merge, propose_leaf_boundary_rewrite, send_step_coach, set_step_coach_status, start_goal_harvest,
    prepare_goal_archive, summarize_goal_answer,
    open_leaf_workspace, send_leaf_workspace, decide_leaf_workspace_proposal,
    clear_leaf_workspace_messages, parse_leaf_workspace_reply, reopen_leaf_workspace,
    prepare_missing_leaf_handoff, repair_leaf_handoff_artifact,
    _leaf_workspace_view,
)
from livingpc.goal_ai import (
    get_goal_agent_model, promote_memory_candidate, run_goal_agent,
    run_goal_subtree, run_goal_sweep,
)
from livingpc.goals import (
    GoalStore, propose_goal_intake, propose_goal_restructure,
    record_experiment_outcome,
)
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


def test_leaf_agents_use_recognition_led_memory_reconstruction():
    for prompt in (LEAF_WORKSPACE_SYSTEM, STEP_COACH_SYSTEM):
        assert "easiest first" in prompt or "smallest" in prompt
        assert "recognition" in prompt
        assert "hypothesis" in prompt or "hypotheses" in prompt
        assert "organization" in prompt or "organizing" in prompt
    assert "one main" in LEAF_WORKSPACE_SYSTEM and "question\nper turn" in LEAF_WORKSPACE_SYSTEM
    assert "Never require a complete inventory" in LEAF_WORKSPACE_SYSTEM


def test_leaf_workspace_exposes_now_labels_only_for_an_attended_project(world):
    cfg, goals, agents, _, ids = world
    second = goals.create("task", "Practice listening", parent_id=ids["sub_a"])

    quiet = build_leaf_workspace_context(goals, agents, ids["task_a"])
    assert quiet["growth_horizon"]["roles_visible"] is False
    assert [leaf["planning_role"] for leaf in quiet["growth_horizon"]["leaves"]] == [
        None, None]

    goals.set_project_signal(ids["sub_a"], "currently_working")
    attended = build_leaf_workspace_context(goals, agents, ids["task_a"])
    assert attended["growth_horizon"]["roles_visible"] is True
    assert attended["growth_horizon"]["project_focus"]["currently_working"] is True
    assert [leaf["planning_role"] for leaf in attended["growth_horizon"]["leaves"]] == [
        "now", "tentative_next"]
    assert attended["growth_horizon"]["leaves"][1]["id"] == second


def test_leaf_workspace_scans_only_its_own_encrypted_documents(world):
    cfg, goals, _, _, ids = world
    sibling = goals.create("task", "Practice listening", parent_id=ids["sub_a"])
    documents = ContextAttachmentStore(cfg.memory_db_path)
    try:
        documents.add_text(
            "leaf_workspace", ids["task_a"], "particle-notes.md",
            "Opening context.\n\nThe energy handoff uses a blue status column and CSV export.")
        documents.add_text(
            "leaf_workspace", sibling, "sibling-secret.txt",
            "This sibling-only document must never enter the particle Leaf.")
    finally:
        documents.close()
    model = RecordingWorkspaceModel([
        "I can use the attached particle notes here.",
        "The document says the handoff uses a blue status column and CSV export.",
    ])

    opened = open_leaf_workspace(cfg, ids["task_a"], model=model)
    opening_context = model.calls[0]["context"]
    assert [item["name"] for item in opened["attachments"]] == ["particle-notes.md"]
    assert "energy handoff" in opening_context["attached_documents"]["excerpts"]
    assert "sibling-only" not in json.dumps(opening_context)

    sent = send_leaf_workspace(
        cfg, ids["task_a"], "What does the attached document say about the energy handoff?",
        model=model)
    turn_context = model.calls[-1]["context"]
    assert "blue status column" in turn_context["attached_documents"]["excerpts"]
    user_turn = sent["messages"][-2]
    assert user_turn["content"] == (
        "What does the attached document say about the energy handoff?")
    assert "blue status column" not in user_turn["content"]
    assert "attached_documents" in LEAF_WORKSPACE_SYSTEM
    assert "untrusted reference material" in LEAF_WORKSPACE_SYSTEM


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


class StaticCoachModel:
    model_name = "static-coach"

    def __init__(self):
        self.calls = []

    def coach(self, context, messages, *, opening=False):
        self.calls.append((context, messages, opening))
        if opening:
            return StepCoachReply(
                "Let’s identify the smallest useful starting point.",
                question="What have you already tried that relates to this step?")
        completed = any(
            message["role"] == "user" and "done" in message["payload"].get("text", "").lower()
            for message in messages[-2:])
        return StepCoachReply(
            "You finished this step." if completed else "Use the example you just chose.",
            "" if completed else "Write one sentence with 은/는.",
            "What sentence did you write?", ["저는 학생이에요.", "오늘은 바빠요."],
            blocker="The user needs a concrete example", decision="Start with 은/는",
            step_completed=completed)


class FailingCoachModel:
    def coach(self, context, messages, *, opening=False):
        raise RuntimeError("coach unavailable")


class RecordingWorkspaceModel:
    model_name = "recording-workspace"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def leaf_workspace(self, context, messages, *, event=None, opening=False):
        self.calls.append({"context": context, "messages": list(messages),
                           "event": event, "opening": opening})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class StaticStepDraftModel:
    model_name = "static-step-draft"

    def __init__(self):
        self.context = None

    def draft_leaf_steps(self, context):
        self.context = context
        peer = context["peer_leaves"][0]
        return LeafStepDraft(
            "The preceding Leaf's candidate list", "A scored shortlist of two",
            ["Open the candidate list.", "Score each candidate.", "Keep the top two."],
            "Do not choose the final candidate here.",
            [{"node_id": peer["id"], "score": .58,
              "reason": "Both descriptions currently select a winner.",
              "recommendation": "narrow"}])


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


def test_leaf_step_draft_context_sees_ordered_root_peers_but_not_other_roots(world):
    _, goals, _, _, ids = world
    earlier = goals.create("task", "List candidates", parent_id=ids["over_a"],
                           due_date="2026-07-13", description="Generate three candidates.")
    goals.update(ids["task_a"], due_date="2026-07-14",
                 description="Score candidates and choose one.")
    later = goals.create("task", "Publish result", parent_id=ids["over_a"],
                         due_date="2026-07-15", description="Publish the chosen result.")

    context = build_leaf_step_draft_context(goals, ids["task_a"])
    encoded = json.dumps(context)

    assert [item["id"] for item in context["peer_leaves"]] == [earlier, later]
    # Canonical tree position wins over an earlier due date; due/priority no
    # longer silently reorder the NOW/PROVISIONAL sequence.
    assert [item["relation"] for item in context["peer_leaves"]] == ["later", "later"]
    assert "Lift" not in encoded and "Build strength" not in encoded
    assert "unrelated_roots" in context["jurisdiction"]["excludes"]


def test_boundary_aware_step_draft_returns_contracts_and_peer_overlap(world):
    cfg, goals, _, _, ids = world
    peer = goals.create("task", "Choose final example", parent_id=ids["sub_a"],
                        description="Choose one final example.")
    model = StaticStepDraftModel()

    result = draft_leaf_steps(cfg, ids["task_a"], model=model)

    assert result["input_contract"] == "The preceding Leaf's candidate list"
    assert result["output_contract"] == "A scored shortlist of two"
    assert result["text"].startswith("1. Open the candidate list.")
    assert result["overlaps"][0]["node_id"] == peer
    assert result["peer_titles"][str(peer)] == "Choose final example"


def test_leaf_step_draft_parser_rejects_unknown_peer_overlap():
    draft = parse_leaf_step_draft(json.dumps({
        "input_contract": "candidate list", "output_contract": "shortlist",
        "steps": ["Open it", "Score it", "Keep two"],
        "overlaps": [
            {"node_id": 7, "score": .8, "reason": "same output", "recommendation": "merge"},
            {"node_id": 99, "score": .9, "reason": "outside scope", "recommendation": "merge"},
        ],
    }), {7})
    assert draft and [item["node_id"] for item in draft.overlaps] == [7]


def test_leaf_coach_context_is_strictly_leaf_and_ancestor_scoped(world):
    cfg, goals, agents, curiosities, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Open a practice page.\n2. Write one sentence.")
    cid = curiosities.add_curiosity("find a particle example", "Particle examples")
    curiosities.add_item(cid, "question", "Which particle is confusing?")
    goals.link_curiosity(ids["task_a"], cid)
    mem = MemoryStore(cfg.memory_db_path)
    mem.add("private", "global", "must never enter Leaf Coach")
    mem.close()

    context = build_step_coach_context(goals, agents, ids["task_a"], 0)
    encoded = json.dumps(context)
    assert [item["title"] for item in context["ancestor_intent"]] == [
        "Actualized Self", "Korean", "Grammar"]
    assert context["focused_step"]["text"] == "Open a practice page."
    assert "Particle examples" in encoded
    assert "Fitness" not in encoded and "Lift" not in encoded
    assert "must never enter Leaf Coach" not in encoded
    assert "core_profile" not in context and "recent_chat" not in context
    assert "screen_activity" in context["jurisdiction"]["excludes"]


def test_leaf_coach_opening_does_ideation_first_with_suggested_responses():
    valid = parse_step_coach(json.dumps({
        "reply": "Common options include reports, inbox sorting, and data entry.", "next_action": "",
        "question": "Which feels closest?", "examples": ["Reports", "Inbox sorting"],
        "step_completed": False, "working_update": {"status": "working"}}), opening=True)
    assert valid and valid.question and not valid.next_action and len(valid.examples) == 2
    assert parse_step_coach(json.dumps({
        "reply": "Pick one.", "question": "Which one?", "examples": ["Only one"]
    }), opening=True) is None


def test_leaf_coach_opening_repairs_common_model_field_variations():
    repaired = parse_step_coach(json.dumps({
        "response": "Here are four useful starting points.",
        "smallest_next_action": "Open a document too early.",
        "follow_up_question": "Which direction fits?",
        "suggested_responses": ["Reports", "Inbox sorting", "Data entry"],
    }), opening=True)

    assert repaired
    assert repaired.reply.startswith("Here are four")
    assert repaired.next_action == ""
    assert repaired.question == "Which direction fits?"
    assert repaired.examples == ["Reports", "Inbox sorting", "Data entry"]


def test_leaf_coach_malformed_model_reply_uses_useful_bounded_fallback():
    model = object.__new__(ClaudeGoalAgentModel)
    model._call = lambda *_args, **_kwargs: "A readable answer, but not valid JSON"
    context = {
        "language": "en",
        "leaf": {"title": "Brainstorm automation wins",
                 "description": "Find common repetitive admin work to automate."},
        "focused_step": {"text": "Choose an automation direction"},
    }

    reply = model.coach(context, [], opening=True)

    assert reply.reply == "Let’s choose a direction for Brainstorm automation wins."
    assert reply.question == "How do these options sound?"
    assert len(reply.examples) == 4
    assert reply.next_action == ""


def test_leaf_coach_strips_legacy_timer_homework_without_discarding_the_answer():
    model = object.__new__(ClaudeGoalAgentModel)
    model._call = lambda *_args, **_kwargs: json.dumps({
        "reply": "Good—repeated data entry is a real pain point. Now open a blank doc and spend 10 minutes writing down every example you can remember.",
        "next_action": "Open a blank document and stop after 10 minutes.",
        "question": "What data-entry work have you observed?",
        "examples": ["Vendor emails", "PDF extraction"],
        "step_completed": False,
        "working_update": {"status": "working", "decision": "Data entry"},
        "step_revision": None,
    })
    context = {
        "language": "en",
        "leaf": {"title": "Brainstorm automation wins",
                 "description": "Find common repetitive admin work to automate."},
        "focused_step": {"text": "Memory dump (10 min): open a blank doc."},
    }
    messages = [{"role": "user", "payload": {"text": "Repeated data entry"}}]

    reply = model.coach(context, messages, opening=False)

    assert reply.reply == "Good—repeated data entry is a real pain point."
    assert reply.question == "What data-entry work have you observed?"
    assert all("minute" not in value.lower() for value in [reply.reply, reply.next_action])
    assert reply.next_action == ""
    assert reply.examples == ["Vendor emails", "PDF extraction"]
    assert reply.decision == "Data entry"


def test_leaf_coach_detail_request_preserves_a_real_conversational_answer():
    explanation = " ".join([
        "Email-to-spreadsheet automation watches a mailbox and extracts consistent fields.",
        "Form-to-CRM automation creates or updates contacts from submitted forms.",
        "PDF-to-expense automation reads invoices and maps totals, dates, and vendors.",
        "Cross-tool synchronization keeps selected fields aligned when either system changes.",
        "The first is easiest when emails follow a template; the second is usually the most reliable;",
        "the third needs document-quality checks; and the fourth needs a clear source of truth.",
    ])
    model = object.__new__(ClaudeGoalAgentModel)
    model._call = lambda *_args, **_kwargs: json.dumps({
        "reply": explanation, "next_action": "",
        "question": "Which tradeoff matters most to you?",
        "examples": ["Show the easiest one", "Compare setup difficulty"],
        "step_completed": False, "working_update": {"status": "working"},
        "step_revision": None,
    })
    context = {
        "language": "en",
        "leaf": {"title": "Brainstorm automation wins",
                 "description": "Find common repetitive admin work to automate."},
        "focused_step": {"text": "Choose an automation direction"},
    }
    messages = [
        {"role": "assistant", "payload": {"examples": [
            "Email attachments → spreadsheet", "Form responses → CRM records"]}},
        {"role": "user", "payload": {"text": "Can you give me more info on each one?"}},
    ]

    reply = model.coach(context, messages, opening=False)

    assert reply.reply == explanation
    assert reply.question == "Which tradeoff matters most to you?"
    assert not reply.reply.startswith("Great—automation")


def test_leaf_coach_all_of_them_advances_instead_of_resetting_the_menu():
    adaptive = " ".join([
        "That breadth is useful: you do not need to pretend these are mutually exclusive.",
        "They can become one operations-automation offer with data movement as the common promise.",
        "You could lead with the easiest workflow to explain, then present reporting, inbox, and",
        "scheduling work as adjacent examples. That keeps the offer clear without discarding capabilities.",
        "The next decision is packaging rather than which skill you possess.",
    ])
    model = object.__new__(ClaudeGoalAgentModel)
    model._call = lambda *_args, **_kwargs: json.dumps({
        "reply": adaptive, "next_action": "",
        "question": "Would you rather sell one broad offer or several focused listings?",
        "examples": ["One broad offer", "Several focused listings", "One lead offer plus add-ons"],
        "step_completed": False,
        "working_update": {"status": "working", "decision": "Can offer all four categories"},
        "step_revision": None,
    })
    context = {
        "language": "en",
        "leaf": {"title": "Brainstorm automation wins",
                 "description": "Find common repetitive admin work to automate."},
        "focused_step": {"text": "Choose an automation direction"},
    }
    messages = [{"role": "user", "payload": {
        "text": "All of them, I could offer to do all of those."}}]

    reply = model.coach(context, messages, opening=False)

    assert reply.reply == adaptive
    assert reply.examples[0] == "One broad offer"
    assert reply.decision == "Can offer all four categories"
    assert not reply.reply.startswith("Great—automation")


def test_leaf_coach_all_of_them_has_an_adaptive_fallback_too():
    model = object.__new__(ClaudeGoalAgentModel)
    model._call = lambda *_args, **_kwargs: "not valid JSON"
    context = {
        "language": "en",
        "leaf": {"title": "Brainstorm automation wins",
                 "description": "Find common repetitive admin work to automate."},
        "focused_step": {"text": "Choose an automation direction"},
    }
    messages = [{"role": "user", "payload": {
        "text": "I can do all of them and could offer all of those."}}]

    reply = model.coach(context, messages, opening=False)

    assert reply.reply == "Great—you can offer all of them. Let’s decide how to package them."
    assert "broad operations-automation service" in reply.examples[0]
    assert "Repeated data entry" not in reply.examples
    assert reply.decision == "The user said they can offer all of the presented categories."


def test_leaf_coach_detail_fallback_explains_the_active_options_instead_of_resetting():
    model = object.__new__(ClaudeGoalAgentModel)
    model._call = lambda *_args, **_kwargs: "not valid JSON"
    packaging = [
        "Offer one broad operations-automation service",
        "Lead with one specialty and offer the others as add-ons",
        "Create a separate listing for each workflow type",
        "Bundle them into a recurring operations package",
    ]
    context = {
        "language": "en",
        "leaf": {"title": "Brainstorm automation wins",
                 "description": "Find common repetitive admin work to automate."},
        "focused_step": {"text": "Choose an automation direction"},
    }
    messages = [
        {"role": "assistant", "payload": {"examples": packaging}},
        {"role": "user", "payload": {
            "text": "Hmm, I'm not sure. Can you elaborate more on what each one looks like?"}},
    ]

    reply = model.coach(context, messages, opening=False)

    assert reply.reply.startswith("Here’s what each option would look like:")
    assert "One flexible listing promises" in reply.reply
    assert "Market one easy-to-understand specialty" in reply.reply
    assert "Publish a focused listing" in reply.reply
    assert "Bundle several workflows" in reply.reply
    assert reply.examples == packaging
    assert "Repeated data entry" not in reply.reply


def test_brand_new_leaf_opens_agent_before_explicit_steps_exist(world):
    cfg, _, _, _, ids = world
    model = StubGoalAgentModel()

    opened = open_step_coach(cfg, ids["task_a"], 0, model=model)
    opening = opened["messages"][-1]["payload"]

    assert opened["steps"][0]["text"] == "Practice particles"
    assert opening["text"] == "Let’s choose a direction for Practice particles."
    assert opening["question"] == "How do these options sound?"
    assert len(opening["examples"]) == 4


def test_leaf_coach_step_revision_is_reviewable_and_changes_steps_only_after_approval(world):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], description=(
        "Choose an automation opportunity.\n\nSteps:\n"
        "1. Inventory everything for 10 minutes.\n2. Pick one."))

    class RevisionModel:
        def coach(self, context, messages, *, opening=False):
            if opening:
                return StepCoachReply(
                    "Here are common admin candidates first.", question="Which fits?",
                    examples=["Recurring reports", "Inbox triage", "Data entry"])
            return StepCoachReply(
                "I agree that the current steps make you do the ideation.",
                examples=["Recurring reports", "Inbox triage"],
                step_revision={"steps": [
                    "Review AI-suggested common admin automation candidates.",
                    "Choose or revise the candidate that best fits.",
                    "Define the chosen automation's input and output."],
                    "rationale": "AI should generate the options before asking for reflection."})

    model = RevisionModel()
    opened = open_step_coach(cfg, ids["task_a"], 0, model=model)
    sent = send_step_coach(cfg, ids["task_a"], 0, "This direction is wrong", model=model)
    proposal = sent["messages"][-1]
    assert "10 minutes" in goals.get(ids["task_a"])["description"]
    revised = decide_step_coach_revision(
        cfg, ids["task_a"], proposal["id"], True,
        proposal["payload"]["step_revision"]["steps"])
    assert "10 minutes" not in goals.get(ids["task_a"])["description"]
    assert revised["steps"][0]["text"].startswith("Review AI-suggested")
    stored = agents.coach_message(ids["task_a"], proposal["id"])
    assert stored["payload"]["step_revision_status"] == "approved"


def test_leaf_coach_completion_requires_explicit_structured_confirmation():
    reply = parse_step_coach(json.dumps({
        "reply": "You finished this step.", "next_action": "", "question": "",
        "examples": [], "step_completed": True,
        "working_update": {"status": "working"}}))
    assert reply and reply.step_completed is True


def test_leaf_coach_persists_one_chat_and_does_not_duplicate_same_focus(world):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Open a practice page.\n2. Write one sentence.")
    model = StaticCoachModel()
    first = open_step_coach(cfg, ids["task_a"], 0, model=model)
    reopened = open_step_coach(cfg, ids["task_a"], 0, model=model)
    switched = open_step_coach(cfg, ids["task_a"], 1, model=model)
    assert len(model.calls) == 2
    assert first["focus_step_index"] == reopened["focus_step_index"] == 0
    assert switched["focus_step_index"] == 1
    assert [message["role"] for message in first["messages"]] == ["focus", "assistant"]
    assert len(switched["messages"]) == 4


def test_leaf_coach_updates_flow_to_ancestors_not_siblings(world):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Open a practice page.\n2. Write one sentence.")
    model = StaticCoachModel()
    open_step_coach(cfg, ids["task_a"], 0, model=model)
    send_step_coach(cfg, ids["task_a"], 0, "I need a concrete example", model=model)
    state = agents.coach_states(ids["task_a"])[0]
    assert state["update"]["decision"] == "Start with 은/는"
    assert agents.state(ids["task_a"])["dirty"] is True
    assert agents.state(ids["sub_a"])["dirty"] is True

    parent = build_agent_context(goals, agents, ids["sub_a"])
    child = parent["subtree"]["children"][0]
    sibling = json.dumps(build_agent_context(goals, agents, ids["over_b"]))
    assert child["coach_updates"][0]["update"]["decision"] == "Start with 은/는"
    assert "Start with" not in sibling
    assert "Leaf Coach conversation" not in json.dumps(parent)


def test_leaf_coach_completion_reopen_and_clear_preserve_resolution(world):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Open a practice page.")
    model = StaticCoachModel()
    open_step_coach(cfg, ids["task_a"], 0, model=model)
    candidate = send_step_coach(cfg, ids["task_a"], 0, "Okay, I'm done", model=model)
    assert candidate["steps"][0]["status"] == "working"
    assert candidate["messages"][-1]["payload"]["step_completed"] is True
    completed = confirm_step_coach_completion(cfg, ids["task_a"], 0, True)
    assert completed["steps"][0]["status"] == "completed"
    resolution = agents.coach_states(ids["task_a"])[0]["update"]["resolution"]
    set_step_coach_status(cfg, ids["task_a"], 0, "reopened")
    assert agents.coach_states(ids["task_a"])[0]["update"]["resolution"] == resolution

    api = GuiApi(cfg)
    cleared = api.goal_step_coach_clear(ids["task_a"])
    assert cleared["ok"] and agents.coach_messages(ids["task_a"]) == []
    assert agents.coach_states(ids["task_a"])[0]["update"]["resolution"] == resolution


def test_leaf_coach_confirmation_can_advance_one_conversation_to_next_step(world):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Open a practice page.\n2. Write one sentence.")
    model = StaticCoachModel()
    open_step_coach(cfg, ids["task_a"], 0, model=model)
    send_step_coach(cfg, ids["task_a"], 0, "Okay, I'm done", model=model)
    declined = confirm_step_coach_completion(cfg, ids["task_a"], 0, False)
    assert declined["steps"][0]["status"] == "working"
    assert declined["messages"][-1]["payload"]["text"] == "Step 1 left open."

    confirm_step_coach_completion(cfg, ids["task_a"], 0, True)
    advanced = open_step_coach(cfg, ids["task_a"], 1, model=model)
    assert advanced["focus_step_index"] == 1
    assert any(message["payload"].get("text", "").startswith("Beginning step 2 of 2:")
               for message in advanced["messages"] if message["role"] == "focus")
    assert len(agents.coach_messages(ids["task_a"])) == len(advanced["messages"])


def test_leaf_coach_bridge_rejects_non_leaf_and_invalid_step(world):
    cfg, goals, _, _, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Open a practice page.")
    api = GuiApi(cfg)
    assert api.goal_step_coach_open(ids["task_a"], 0)["ok"]
    assert not api.goal_step_coach_open(ids["over_a"], 0)["ok"]
    assert not api.goal_step_coach_open(ids["task_a"], 9)["ok"]


def test_leaf_coach_failure_preserves_existing_messages(world):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Open a practice page.")
    model = StaticCoachModel()
    open_step_coach(cfg, ids["task_a"], 0, model=model)
    with pytest.raises(RuntimeError, match="coach unavailable"):
        send_step_coach(cfg, ids["task_a"], 0, "Please help", model=FailingCoachModel())
    messages = agents.coach_messages(ids["task_a"])
    assert [message["role"] for message in messages] == ["focus", "assistant", "user"]
    assert messages[-1]["payload"]["text"] == "Please help"
    send_step_coach(cfg, ids["task_a"], 0, "Please help", model=StaticCoachModel())
    assert [message["role"] for message in agents.coach_messages(ids["task_a"])] == [
        "focus", "assistant", "user", "assistant"]


def test_leaf_coach_open_retry_reuses_focus_boundary(world):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Open a practice page.")
    with pytest.raises(RuntimeError, match="coach unavailable"):
        open_step_coach(cfg, ids["task_a"], 0, model=FailingCoachModel())
    assert [message["role"] for message in agents.coach_messages(ids["task_a"])] == ["focus"]
    opened = open_step_coach(cfg, ids["task_a"], 0, model=StaticCoachModel())
    assert [message["role"] for message in opened["messages"]] == ["focus", "assistant"]


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


def test_archive_preparation_reviews_context_then_commits_upward_handoff(world):
    cfg, goals, agents, _, ids = world
    goals.add_evidence(ids["task_a"], "manual_note", "proof", "A useful attached lesson")

    prepared = prepare_goal_archive(cfg, ids["task_a"], model=StubGoalAgentModel())
    api = GuiApi(cfg)
    archived = api.goal_archive(ids["task_a"], prepared["harvest"]["id"])
    parent_context = build_agent_context(goals, agents, ids["sub_a"])

    assert archived["ok"] and archived["handoff"]["status"] == "committed"
    assert goals.get(ids["task_a"])["status"] == "archived"
    assert any(item["source_node_id"] == ids["task_a"]
               for item in parent_context["committed_harvests"])
    assert goals.conn.execute(
        "SELECT COUNT(*) FROM goal_evidence_link WHERE goal_id=?", (ids["task_a"],)
    ).fetchone()[0] == 1


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


class LeafBatchProposalModel:
    model_name = "leaf-batch-model"

    def __init__(self, *titles):
        self.titles = titles

    def assess(self, context, role):
        node_id = context["node"]["id"]
        return AgentReport(
            "Several next steps are possible.", "needs-attention", .8,
            proposals=[AgentProposal(
                "create_child", node_id,
                {"type": "task", "title": title}, "Possible next step")
                for title in self.titles])


def test_proposal_cap_and_deduplication(world):
    cfg, _, agents, _, ids = world
    cfg.goal_ai_max_open_proposals = 3
    run_goal_agent(cfg, ids["sub_a"], model=ProposalModel())
    assert len(agents.proposals(ids["sub_a"])) == 3
    agents.mark_dirty(ids["sub_a"], ancestors=False)
    run_goal_agent(cfg, ids["sub_a"], model=ProposalModel())
    assert len(agents.proposals(ids["sub_a"])) == 3


def test_goal_ai_pending_leaf_reserves_remaining_horizon_slot(world):
    cfg, _, agents, _, ids = world
    cfg.goal_ai_leaf_horizon = 2

    result = run_goal_agent(cfg, ids["sub_a"], model=LeafBatchProposalModel(
        "Collect pronunciation clips", "Choose conversation partner"))

    assert result["proposals_created"] == 1
    proposals = agents.proposals(ids["sub_a"])
    assert [proposal["payload"]["title"] for proposal in proposals] == [
        "Collect pronunciation clips"]


def test_goal_ai_pending_semantic_duplicate_is_suppressed_without_using_slot(world):
    cfg, goals, agents, _, ids = world
    cfg.goal_ai_leaf_horizon = 2
    listening = goals.create("subgoal", "Listening", parent_id=ids["over_a"])

    result = run_goal_agent(cfg, listening, model=LeafBatchProposalModel(
        "Collect pronunciation clips",
        "Collect Korean pronunciation clips",
        "Choose conversation partner"))

    assert result["proposals_created"] == 2
    titles = {proposal["payload"]["title"]
              for proposal in agents.proposals(listening)}
    assert titles == {"Collect pronunciation clips", "Choose conversation partner"}


def test_create_child_task_respects_the_leaf_horizon(world):
    cfg, goals, agents, _, ids = world
    cfg.goal_ai_leaf_horizon = 2
    proposal_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "create_child", ids["sub_a"],
        {"type": "task", "title": "A third queued step"}, "Stacks the queue"))
    # A child added after staging does not change the parent's version, so this
    # specifically exercises the approval-time horizon revalidation.
    goals.create("task", "Provisional next drill", parent_id=ids["sub_a"])
    with pytest.raises(ValueError, match="horizon"):
        decide_proposal(cfg, proposal_id, "approve")
    assert agents.get_proposal(proposal_id)["status"] == "stale"
    assert goals.open_leaf_count(ids["sub_a"]) == 2


def test_create_child_approval_counts_other_pending_leaf_reservations(world):
    cfg, goals, agents, _, ids = world
    cfg.goal_ai_leaf_horizon = 2
    first_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "create_child", ids["sub_a"],
        {"type": "task", "title": "Collect pronunciation clips"},
        "Candidate one"))
    second_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "create_child", ids["sub_a"],
        {"type": "task", "title": "Choose conversation partner"},
        "Candidate two"))

    with pytest.raises(ValueError, match="horizon"):
        decide_proposal(cfg, second_id, "approve")

    assert agents.get_proposal(second_id)["status"] == "stale"
    assert agents.get_proposal(first_id)["status"] == "open"
    assert goals.open_leaf_count(ids["sub_a"]) == 1
    assert decide_proposal(cfg, first_id, "approve")["ok"]
    assert goals.open_leaf_count(ids["sub_a"]) == 2


@pytest.mark.parametrize(
    ("title", "message"),
    [
        ("  PRACTICE particles  ", "same normalized title"),
        ("Practice Korean particles", "overlap"),
    ],
)
def test_create_child_task_rejects_duplicate_or_overlapping_leaf_at_approval(
        world, title, message):
    cfg, goals, agents, _, ids = world
    cfg.goal_ai_leaf_horizon = 2
    proposal_id = agents.add_proposal(ids["sub_a"], AgentProposal(
        "create_child", ids["sub_a"],
        {"type": "task", "title": title}, "Repeats the active work"))

    with pytest.raises(ValueError, match=message):
        decide_proposal(cfg, proposal_id, "approve")

    assert agents.get_proposal(proposal_id)["status"] == "stale"
    assert goals.open_leaf_count(ids["sub_a"]) == 1
    assert goals.get(ids["task_a"])["title"] == "Practice particles"


def test_goal_ai_leaf_rename_excludes_itself_but_rejects_sibling_overlap(world):
    cfg, goals, agents, _, ids = world
    ok_id = agents.add_proposal(ids["task_a"], AgentProposal(
        "update_fields", ids["task_a"],
        {"title": "Practice particles deliberately"}, "Clarify the output"))
    assert decide_proposal(cfg, ok_id, "approve")["ok"]
    assert goals.get(ids["task_a"])["title"] == "Practice particles deliberately"

    sibling = goals.create("task", "Review vocabulary", parent_id=ids["sub_a"])
    for title in ("Review vocabulary", "Review Korean vocabulary"):
        proposal_id = agents.add_proposal(ids["task_a"], AgentProposal(
            "update_fields", ids["task_a"], {"title": title},
            "Would overlap sibling work"))
        with pytest.raises(ValueError, match="same normalized title|overlap"):
            decide_proposal(cfg, proposal_id, "approve")
        assert agents.get_proposal(proposal_id)["status"] == "stale"
        assert goals.get(ids["task_a"])["title"] == "Practice particles deliberately"
    assert goals.get(sibling)["status"] == "active"


@pytest.mark.parametrize("duplicate", [False, True])
def test_restructure_move_revalidates_destination_leaf_horizon(world, duplicate):
    cfg, goals, agents, _, ids = world
    if duplicate:
        moving = goals.create("task", "Lift", parent_id=ids["sub_a"])
        message = "same normalized title"
    else:
        moving = ids["task_a"]
        goals.create("task", "Stretch", parent_id=ids["over_b"])
        message = "horizon"
    proposal_id = agents.add_proposal(moving, AgentProposal(
        "restructure_node", moving, {
            "new_type": "task", "parent_id": ids["over_b"], "position": 0,
        }, "Move this Leaf into the other Project"))

    with pytest.raises(ValueError, match=message):
        decide_proposal(cfg, proposal_id, "approve")

    assert goals.get(moving)["parent_id"] == ids["sub_a"]
    assert agents.get_proposal(proposal_id)["status"] == "stale"


def test_restructure_retype_cannot_create_third_leaf(world):
    cfg, goals, agents, _, ids = world
    goals.create("task", "Stretch", parent_id=ids["over_b"])
    branch = goals.create("subgoal", "New fitness routine", parent_id=ids["over_a"])
    proposal_id = agents.add_proposal(branch, AgentProposal(
        "restructure_node", branch, {
            "new_type": "task", "parent_id": ids["over_b"], "position": 2,
        }, "Turn this Branch into a Leaf"))

    with pytest.raises(ValueError, match="horizon"):
        decide_proposal(cfg, proposal_id, "approve")

    assert goals.get(branch)["type"] == "subgoal"
    assert agents.get_proposal(proposal_id)["status"] == "stale"


def test_plain_language_leaf_intake_inherits_approval_time_horizon_guard(world):
    cfg, goals, agents, _, ids = world
    cfg.goal_ai_leaf_horizon = 2
    staged = propose_goal_intake(cfg, {
        "parent_id": ids["sub_a"], "new_type": "task",
        "title": "Collect pronunciation clips",
        "description": "Save a small set for the next practice session.",
    })
    # Simulate the tree changing while the intake card is waiting for approval.
    goals.create("task", "Provisional next drill", parent_id=ids["sub_a"])

    with pytest.raises(ValueError, match="horizon"):
        decide_proposal(cfg, staged["proposal_id"], "approve")

    assert agents.get_proposal(staged["proposal_id"])["status"] == "stale"
    assert not any(
        node["title"] == "Collect pronunciation clips" for node in goals.catalog())


def test_agent_cannot_propose_into_unrelated_branch(world):
    _, _, agents, _, ids = world
    created = agents.add_proposal(ids["sub_a"], AgentProposal(
        "update_fields", ids["over_b"], {"priority": "high"}, "Wrong branch"))
    assert created is None


def test_restructure_proposal_applies_atomically_and_stales_old_context(world):
    cfg, goals, agents, _, ids = world
    older = agents.add_proposal(ids["sub_a"], AgentProposal(
        "update_fields", ids["sub_a"], {"priority": "high"},
        "This was drafted under the old ancestor path."))
    staged = propose_goal_restructure(
        cfg, ids["over_a"], "subgoal", ids["over_b"],
        rationale="Korean is being moved beneath the selected durable domain for this test.")

    result = decide_proposal(cfg, staged["proposal_id"], "approve")

    moved = goals.get(ids["over_a"])
    assert result["ok"] and result["restructure"]["node_id_preserved"]
    assert moved["type"] == "subgoal" and moved["parent_id"] == ids["over_b"]
    assert goals.get(ids["sub_a"])["parent_id"] == ids["over_a"]
    assert agents.get_proposal(staged["proposal_id"])["status"] == "approved"
    assert agents.get_proposal(older)["status"] == "stale"
    history = goals.conn.execute(
        "SELECT proposal_id,old_node_type,new_node_type FROM goal_restructure_history "
        "WHERE goal_id=?", (ids["over_a"],)).fetchone()
    assert history["proposal_id"] == staged["proposal_id"]
    assert (history["old_node_type"], history["new_node_type"]) == ("overgoal", "subgoal")


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
    origin = goals.origin(result["created_goal_id"])
    assert origin["source_kind"] == "goal_ai"
    assert origin["source_proposal_id"] == proposal_id


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


def test_task_gardening_rewrite_revalidates_sibling_overlap(world):
    cfg, goals, agents, _, ids = world
    sibling = goals.create("task", "Review vocabulary", parent_id=ids["sub_a"])
    review = gardening_review(ids["task_a"], "rewrite", {
        "title": "Review Korean vocabulary",
        "description": "Review the vocabulary list once.",
    })
    result = review_goal_relevance(
        cfg, ids["task_a"], model=StaticRelevanceModel(review))
    proposal_id = result["proposal_ids"][0]

    with pytest.raises(ValueError, match="overlap"):
        decide_gardening_proposal(cfg, proposal_id, "approve")

    assert goals.get(ids["task_a"])["title"] == "Practice particles"
    assert goals.get(sibling)["status"] == "active"
    assert agents.get_gardening_proposal(proposal_id)["status"] == "stale"


def test_task_gardening_split_is_atomic_and_cannot_exceed_horizon(world):
    cfg, goals, agents, _, ids = world
    sibling = goals.create("task", "Review vocabulary", parent_id=ids["sub_a"])
    review = gardening_review(ids["task_a"], "split", {"parts": [
        {"title": "Notice particle patterns", "description": "Notice examples."},
        {"title": "Use particle patterns", "description": "Say examples aloud."},
    ]})
    result = review_goal_relevance(
        cfg, ids["task_a"], model=StaticRelevanceModel(review))
    proposal_id = result["proposal_ids"][0]

    with pytest.raises(ValueError, match="horizon"):
        decide_gardening_proposal(cfg, proposal_id, "approve")

    assert goals.get(ids["task_a"])["title"] == "Practice particles"
    assert goals.get(sibling)["status"] == "active"
    assert not any(node["title"] == "Use particle patterns" for node in goals.catalog())
    assert agents.get_gardening_proposal(proposal_id)["status"] == "stale"


def test_pending_task_gardening_split_reserves_goal_ai_horizon(world):
    cfg, goals, agents, _, ids = world
    review = gardening_review(ids["task_a"], "split", {"parts": [
        {"title": "Notice particle patterns", "description": "Notice examples."},
        {"title": "Use particle patterns", "description": "Say examples aloud."},
    ]})
    result = review_goal_relevance(
        cfg, ids["task_a"], model=StaticRelevanceModel(review))
    assert agents.get_gardening_proposal(result["proposal_ids"][0])["status"] == "open"

    blocked = agents.add_proposal(ids["sub_a"], AgentProposal(
        "create_child", ids["sub_a"],
        {"type": "task", "title": "A third particle drill"},
        "This would exceed the reserved split"), goals=goals)

    assert blocked is None
    assert goals.open_leaf_count(ids["sub_a"]) == 1


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


def test_leaf_boundary_merge_is_approval_only_and_preserves_execution_history(world):
    cfg, goals, agents, _, ids = world
    target = goals.create("task", "Evaluate candidates", parent_id=ids["sub_a"],
                          description="Score and shortlist candidates.")
    source = goals.create("task", "Choose final candidate", parent_id=ids["sub_a"],
                          description="Score candidates again and choose one.")
    agents.add_coach_message(source, 0, "Choose one", "user", {"text": "I chose option A."})
    agents.update_coach_state(source, 0, "Choose one", "completed",
                              {"resolution": "Option A selected."})
    record_experiment_outcome(cfg, source, {
        "result": "completed", "what_happened": "Option A was selected.",
        "expected_obstacle": "", "surprise": "", "helpfulness": 7,
        "changed_understanding": "", "next_adjustment": ""})

    proposal = propose_leaf_boundary_merge(
        cfg, target, source, title="Evaluate and choose one candidate",
        description="Score the candidate list once, choose one, and state its value.")

    assert proposal["proposals_created"] == 1
    assert goals.get(source)["status"] == "completed"
    decide_gardening_proposal(cfg, proposal["proposal_ids"][0], "approve")
    assert goals.get(source)["status"] == "archived"
    assert goals.get(target)["title"] == "Evaluate and choose one candidate"
    assert len(agents.coach_messages(target, 20)) == 1
    assert agents.coach_states(target, 20)[0]["status"] == "completed"
    moved = goals.conn.execute(
        "SELECT COUNT(*) FROM experiment_outcome WHERE goal_id=?", (target,)).fetchone()[0]
    assert moved == 1


def test_task_gardening_merge_rolls_back_history_transfer_on_replan_failure(
        world, monkeypatch):
    cfg, goals, agents, _, ids = world
    source = goals.create("task", "Practice example review", parent_id=ids["sub_a"])
    agents.add_coach_message(
        source, 0, "Review one example", "user", {"text": "Reviewed example A."})
    proposal = propose_leaf_boundary_merge(
        cfg, ids["task_a"], source, title="Practice and review particles",
        description="Practice particles and review one example once.")
    proposal_id = proposal["proposal_ids"][0]

    monkeypatch.setattr(
        GoalStore, "apply_replan_project",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("forced replan failure")))
    with pytest.raises(RuntimeError, match="forced replan failure"):
        decide_gardening_proposal(cfg, proposal_id, "approve")

    assert goals.get(source)["status"] == "active"
    assert len(agents.coach_messages(source, 20)) == 1
    assert agents.coach_messages(ids["task_a"], 20) == []
    assert agents.get_gardening_proposal(proposal_id)["status"] == "open"


def test_leaf_boundary_rewrite_is_approval_only_and_keeps_coaching(world):
    cfg, goals, agents, _, ids = world
    agents.add_coach_message(ids["task_a"], 0, "List examples", "user",
                             {"text": "I listed three examples."})
    proposal = propose_leaf_boundary_rewrite(
        cfg, ids["task_a"], title="List particle examples",
        description="Output exactly three unranked particle examples.",
        rationale="Selection belongs to the next Leaf.")
    assert goals.get(ids["task_a"])["title"] == "Practice particles"
    decide_gardening_proposal(cfg, proposal["proposal_ids"][0], "approve")
    assert goals.get(ids["task_a"])["title"] == "List particle examples"
    assert len(agents.coach_messages(ids["task_a"], 20)) == 1


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


def test_dismissed_question_is_not_regenerated_by_the_next_report(world):
    cfg, _, agents, _, ids = world
    question_text = "What does a small experiment mean to you?"
    report = AgentReport("Need scope", "unknown", .5, questions=[question_text])
    agents.save_report(ids["sub_a"], report, "first", "stub")
    question = agents.questions(ids["sub_a"])[0]
    agents.dismiss_question(question["id"])

    agents.save_report(
        ids["sub_a"], AgentReport(
            "Need scope", "unknown", .5,
            questions=["WHAT does a small experiment mean to you !"]),
        "second", "stub")

    history = agents.questions(ids["sub_a"], include_resolved=True)
    assert len(history) == 1
    assert history[0]["status"] == "dismissed"

    # Existing databases may already contain a later duplicate from before this
    # lifecycle rule existed. Opening the store retires that stale duplicate.
    agents.conn.execute(
        "INSERT INTO goal_agent_question (node_id,text,status,created_at) VALUES (?,?, 'open',?)",
        (ids["sub_a"], crypto.enc(question_text), "2026-01-01T00:00:00+00:00"))
    agents.conn.commit()
    reopened_store = GoalAgentStore(cfg.memory_db_path)
    try:
        assert reopened_store.questions(ids["sub_a"]) == []
    finally:
        reopened_store.close()


def test_approved_action_retires_questions_from_the_same_assessment(world):
    cfg, _, agents, _, ids = world
    question_text = "What does small mean for this Upwork experiment?"
    report = AgentReport(
        "Ready to try it", "unknown", .72, questions=[question_text],
        proposals=[AgentProposal(
            "create_child", ids["sub_a"],
            {"type": "task", "title": "Try a small Upwork automation gig"},
            "Turn the proposed experiment into an active Leaf")])
    agents.save_report(ids["sub_a"], report, "before-action", "stub")
    proposal_id = agents.proposals(ids["sub_a"])[0]["id"]

    result = decide_proposal(cfg, proposal_id, "approve")

    assert result["superseded_questions"] == 1
    assert agents.questions(ids["sub_a"]) == []
    history = agents.questions(ids["sub_a"], include_resolved=True)
    assert len(history) == 1 and history[0]["status"] == "dismissed"
    agents.save_report(
        ids["sub_a"], AgentReport("Working on it", "on-track", .8,
                                  questions=[question_text]),
        "after-action", "stub")
    assert len(agents.questions(ids["sub_a"], include_resolved=True)) == 1


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
    agents.add_coach_message(node, 0, "Private step", "user", {"text": "Private coaching"})
    agents.update_coach_state(node, 0, "Private step", "blocked",
                              {"blocker": "Private coach blocker"})
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
    raw_coach_message = agents.conn.execute(
        "SELECT payload_json FROM goal_step_coach_message WHERE node_id=?", (node,)).fetchone()[0]
    raw_coach_state = agents.conn.execute(
        "SELECT step_text,update_json FROM goal_step_coach_state WHERE node_id=?", (node,)).fetchone()
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
                                                        raw_coach_message, *raw_coach_state,
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


def test_leaf_workspace_parser_keeps_plain_prose_and_drops_only_bad_extras():
    plain = parse_leaf_workspace_reply(
        "I understand. You can offer all four services, so let’s compare packaging.")
    assert plain and plain.message.startswith("I understand")
    assert plain.suggestions == [] and plain.proposal is None

    malformed_optional = parse_leaf_workspace_reply({
        "message": "Here is a substantive explanation that must survive.",
        "suggestions": {"not": "a list"},
        "proposal": {"type": "plan", "payload": "not an object"},
        "working_patch": "not an object",
    })
    assert malformed_optional
    assert malformed_optional.message == "Here is a substantive explanation that must survive."
    assert malformed_optional.suggestions == []
    assert malformed_optional.proposal is None
    assert malformed_optional.selection_mode == "single"

    multi = parse_leaf_workspace_reply({
        "message": "Choose every category that applies.",
        "selection_mode": "multiple",
        "suggestions": [{"label": "Email"}, {"label": "Reports"}],
    })
    assert multi and multi.selection_mode == "multiple"
    invalid_mode = parse_leaf_workspace_reply({
        "message": "This remains usable.", "selection_mode": "anything"})
    assert invalid_mode and invalid_mode.selection_mode == "single"

    mixed = parse_leaf_workspace_reply({
        "message": "I need your rate preference and the tools you use.",
        "questions": [
            {"prompt": "Which rate structure fits?", "type": "single_choice",
             "options": ["Hourly", "Fixed price", "Flexible"]},
            {"prompt": "Which work types apply?", "type": "multi_select",
             "required": False,
             "options": [{"label": "Automation"}, {"label": "Data workflows"}]},
            {"prompt": "Which tools or skills should be listed?", "type": "text",
             "placeholder": "Python, Make, Zapier…"},
            {"prompt": "Broken choice", "type": "single_choice",
             "options": ["Only one"]},
        ],
    })
    assert mixed and [question["type"] for question in mixed.questions] == [
        "single_choice", "multi_select", "text"]
    assert mixed.questions[1]["required"] is False
    assert mixed.questions[2]["placeholder"] == "Python, Make, Zapier…"
    assert all(question["id"].startswith("question-") for question in mixed.questions)
    assert all(option["id"].startswith("option-")
               for question in mixed.questions for option in question["options"])

    scratch = parse_leaf_workspace_reply({
        "message": "I can preserve the conversational focus.",
        "working_patch": {"current_focus": "packaging", "decisions": ["assumed"],
                          "blockers": ["assumed"], "constraints": ["assumed"]},
    })
    assert scratch and scratch.working_patch == {"current_focus": "packaging"}

    broken_tail = parse_leaf_workspace_reply(
        'This answer is still useful.\n{"suggestions":[{"label":"truncated"}')
    assert broken_tail and broken_tail.message == "This answer is still useful."
    truncated_fenced = parse_leaf_workspace_reply(
        '```json\n{\n  "message": "Perfect. I can build the full draft.\\n\\n'
        '**About Me**\\nUse \\"clear milestones\\" and keep the profile practical')
    assert truncated_fenced
    assert truncated_fenced.message == (
        'Perfect. I can build the full draft.\n\n'
        '**About Me**\nUse "clear milestones" and keep the profile practical')
    assert "```json" not in truncated_fenced.message
    assert "\\n" not in truncated_fenced.message
    assert truncated_fenced.questions == [] and truncated_fenced.proposal is None
    assert truncated_fenced.recovered_partial is True
    broken_cards = parse_leaf_workspace_reply(
        '{"message":"The readable answer survives.",'
        '"questions":[{"prompt":"Which one?","type":"single_choice"')
    assert broken_cards and broken_cards.message == "The readable answer survives."
    assert broken_cards.questions == []
    braces_are_prose = parse_leaf_workspace_reply(
        "Use the {client_id} field as the matching key; that is the concrete answer.")
    assert braces_are_prose and braces_are_prose.message.startswith("Use the {client_id}")

    completion = parse_leaf_workspace_reply({
        "message": "Four concrete automation candidates are ready for the next Leaf.",
        "proposal": {
            "type": "complete_leaf",
            "payload": {},
            "rationale": "Starting with examples made recall easier than beginning from a blank page.",
        },
    })
    assert completion and completion.proposal
    assert completion.proposal["payload"]["result"] == (
        "Four concrete automation candidates are ready for the next Leaf.")
    assert completion.proposal["payload"]["lesson"] == (
        "Starting with examples made recall easier than beginning from a blank page.")


def test_leaf_workspace_completion_prompt_requires_editable_result_and_lesson_drafts():
    assert "always draft both payload.result and payload.lesson" in LEAF_WORKSPACE_SYSTEM
    assert "report about your own broken reply" in LEAF_WORKSPACE_SYSTEM
    assert "without asking where the JSON came from" in LEAF_WORKSPACE_SYSTEM


def test_leaf_workspace_model_has_room_for_complete_user_facing_drafts():
    model = object.__new__(ClaudeGoalAgentModel)
    captured = {}

    def call(_system, _prompt, **kwargs):
        captured.update(kwargs)
        return '{"message":"A complete draft."}'

    model._call = call
    reply = model.leaf_workspace(
        {"language": "en", "leaf": {"title": "Draft profile"}}, [], opening=False)

    assert reply.message == "A complete draft."
    assert captured["max_tokens"] == 4096


def test_leaf_workspace_repairs_an_already_stored_raw_truncated_json_reply(world):
    cfg, goals, agents, _, ids = world
    agents.ensure_leaf_workspace(goals.get(ids["task_a"]))
    raw = ('```json\n{"message":"Here is the usable profile draft.\\n\\n'
           '**Skills**\\nPython, automation, and reporting')
    stored = agents.add_leaf_workspace_message(ids["task_a"], "assistant", raw)
    model = RecordingWorkspaceModel(["I can continue from that repaired draft."])

    sent = send_leaf_workspace(
        cfg, ids["task_a"], "Please regenerate the cut-off response in full.",
        {"type": "retry_partial_response", "message_id": stored}, model=model)

    assert sent["messages"][0]["content"] == (
        "Here is the usable profile draft.\n\n**Skills**\n"
        "Python, automation, and reporting")
    assert sent["messages"][0]["payload"]["recovered_partial"] is True
    assert model.calls[-1]["messages"][0]["content"] == sent["messages"][0]["content"]
    assert model.calls[-1]["event"] == {
        "type": "retry_partial_response", "message_id": stored}
    assert "```json" not in json.dumps(model.calls[-1]["messages"])
    # Presentation repair is non-destructive; the encrypted historical row is
    # not rewritten merely because a newer reader can recover it.
    assert agents.leaf_workspace_messages(ids["task_a"])[0]["content"] == raw


def test_leaf_workspace_is_free_conversation_and_uses_prior_turns(world):
    cfg, _, agents, _, ids = world
    model = RecordingWorkspaceModel([
        "Tell me how you currently understand this Leaf, and we can shape it together.",
        "That makes sense. Doing all of them changes this from choosing a capability "
        "to deciding how the capabilities should be packaged.",
    ])
    opened = open_leaf_workspace(cfg, ids["task_a"], model=model)
    sent = send_leaf_workspace(cfg, ids["task_a"], "I can do all of them.", model=model)

    assert opened["messages"][-1]["content"].startswith("Tell me")
    assert sent["messages"][-1]["content"].startswith("That makes sense")
    assert "Great—automation" not in sent["messages"][-1]["content"]
    assert [item["content"] for item in model.calls[-1]["messages"]] == [
        opened["messages"][0]["content"], "I can do all of them."]
    assert "recent_chat" not in model.calls[-1]["context"]
    assert "siblings" in model.calls[-1]["context"]["jurisdiction"]["excludes"]
    assert agents.leaf_workspace_state(ids["task_a"])["phase"] == "shaping"


def test_leaf_workspace_stub_opening_does_first_ideation_pass_without_timer_homework(world):
    cfg, goals, _, _, ids = world
    goals.update(ids["task_a"], description=(
        "Memory dump (10 minutes): open a blank doc and list ideas."))

    opened = open_leaf_workspace(cfg, ids["task_a"], model=StubGoalAgentModel())

    opening = opened["messages"][-1]
    suggestions = opening["payload"]["suggestions"]
    assert "Practice particles" in opening["content"]
    assert "combine, reject, or correct" in opening["content"]
    assert len(suggestions) == 4
    assert all(item["id"].startswith("suggestion-") for item in suggestions)
    assert "10 minutes" not in opening["content"]
    assert "blank doc" not in opening["content"]


def test_leaf_workspace_suggestion_ids_and_selection_event_persist(world):
    cfg, _, _, _, ids = world
    model = RecordingWorkspaceModel([
        LeafWorkspaceReply("Here are relevant options.", suggestions=[
            {"label": "One broad service", "description": "Flexible front door"},
            {"label": "Separate listings", "description": "Clearer positioning"},
        ], selection_mode="multiple"),
        "You selected both. We can compare the tradeoffs without forcing one choice.",
    ])
    opened = open_leaf_workspace(cfg, ids["task_a"], model=model)
    suggestions = opened["messages"][-1]["payload"]["suggestions"]
    assert opened["messages"][-1]["payload"]["selection_mode"] == "multiple"
    ids_selected = [item["id"] for item in suggestions]
    assert all(value.startswith("suggestion-") for value in ids_selected)

    sent = send_leaf_workspace(
        cfg, ids["task_a"], "Both fit.",
        {"type": "select_suggestions", "suggestion_ids": ids_selected}, model=model)
    user = sent["messages"][-2]
    assert user["payload"]["event"]["suggestion_ids"] == ids_selected
    assert sent["working"]["selected_suggestion_ids"] == ids_selected
    assert model.calls[-1]["event"]["suggestion_ids"] == ids_selected


def test_leaf_workspace_mixed_questions_submit_as_one_structured_turn(world):
    cfg, _, _, _, ids = world
    model = RecordingWorkspaceModel([
        LeafWorkspaceReply(
            "I need two details to shape this accurately.",
            questions=[
                {"prompt": "Which rate structure fits?", "type": "single_choice",
                 "options": ["Hourly", "Fixed price", "Flexible"]},
                {"prompt": "Which tools or skills should be listed?", "type": "text",
                 "placeholder": "Python, Make, Zapier…"},
            ]),
        "Great—I have both the rate structure and the skills now.",
    ])
    opened = open_leaf_workspace(cfg, ids["task_a"], model=model)
    questions = opened["messages"][-1]["payload"]["questions"]
    rate, skills = questions
    hourly = rate["options"][0]

    with pytest.raises(ValueError, match="message or selection is required"):
        send_leaf_workspace(
            cfg, ids["task_a"], "",
            {"type": "answer_questions", "message_id": opened["messages"][-1]["id"],
             "answers": [
                 {"question_id": rate["id"], "option_ids": [hourly["id"]]},
             ]}, model=model)

    sent = send_leaf_workspace(
        cfg, ids["task_a"],
        "Which rate structure fits?: Hourly\n"
        "Which tools or skills should be listed?: Python, Make, and Zapier",
        {"type": "answer_questions", "message_id": opened["messages"][-1]["id"],
         "answers": [
             {"question_id": rate["id"], "option_ids": [hourly["id"]]},
             {"question_id": skills["id"], "text": "Python, Make, and Zapier"},
         ]}, model=model)

    event = sent["messages"][-2]["payload"]["event"]
    assert event["type"] == "answer_questions"
    assert event["answers"][0]["option_labels"] == ["Hourly"]
    assert event["answers"][1]["text"] == "Python, Make, and Zapier"
    assert model.calls[-1]["event"] == event
    assert sent["messages"][-1]["content"].startswith("Great")


def test_leaf_workspace_mutates_plan_only_after_explicit_approval(world):
    cfg, goals, agents, _, ids = world
    model = RecordingWorkspaceModel([
        "Let’s first make sure the outcome is right.",
        LeafWorkspaceReply(
            "I can turn that into a two-part plan for your review.",
            proposal={"type": "plan", "payload": {"items": [
                {"text": "Compare the packaging choices."},
                {"text": "Draft the approved offer."},
            ]}, "rationale": "The user asked for a concrete plan."}),
    ])
    open_leaf_workspace(cfg, ids["task_a"], model=model)
    agents.conn.execute(
        "UPDATE goal_agent_state SET dirty=0 WHERE node_id IN (?,?,?)",
        (ids["task_a"], ids["sub_a"], ids["over_a"]))
    agents.conn.commit()

    pending = send_leaf_workspace(
        cfg, ids["task_a"], "Please propose a plan.", model=model)
    proposal = pending["messages"][-1]["payload"]["proposal"]
    assert pending["plan"] is None and pending["phase"] == "shaping"
    assert agents.state(ids["task_a"])["dirty"] is False
    assert goals.get(ids["task_a"])["status"] == "active"

    approved = decide_leaf_workspace_proposal(
        cfg, ids["task_a"], proposal["id"], "approve")
    assert approved["phase"] == "doing"
    assert [item["text"] for item in approved["plan"]["items"]] == [
        "Compare the packaging choices.", "Draft the approved offer."]
    assert all(item["id"].startswith("item-") for item in approved["plan"]["items"])
    assert agents.state(ids["task_a"])["dirty"] is True
    assert agents.state(ids["sub_a"])["dirty"] is True


def test_leaf_workspace_rejected_proposal_changes_no_semantic_state(world):
    cfg, goals, agents, _, ids = world
    open_leaf_workspace(cfg, ids["task_a"], model=RecordingWorkspaceModel(["Hello."]))
    proposal = agents.add_leaf_workspace_proposal(
        ids["task_a"], "complete_leaf", {"result": "Finished"}, "Looks complete")
    agents.conn.execute(
        "UPDATE goal_agent_state SET dirty=0 WHERE node_id=?", (ids["task_a"],))
    agents.conn.commit()

    rejected = decide_leaf_workspace_proposal(
        cfg, ids["task_a"], proposal["id"], "reject")
    assert rejected["phase"] == "shaping"
    assert goals.get(ids["task_a"])["status"] == "active"
    assert agents.state(ids["task_a"])["dirty"] is False
    assert agents.leaf_workspace_proposal(proposal["id"])["status"] == "rejected"


def test_leaf_workspace_plan_revision_preserves_matching_stable_item_history(world):
    cfg, _, agents, _, ids = world
    open_leaf_workspace(cfg, ids["task_a"], model=RecordingWorkspaceModel(["Hello."]))
    initial = agents.add_leaf_workspace_proposal(ids["task_a"], "plan", {
        "items": [{"id": "research", "text": "Compare the options."},
                  {"id": "draft", "text": "Draft the chosen approach."}]}, "Initial plan")
    first = decide_leaf_workspace_proposal(
        cfg, ids["task_a"], initial["id"], "approve")["plan"]
    complete = agents.add_leaf_workspace_proposal(ids["task_a"], "complete_item", {
        "item_id": "research", "resolution": "Compared all four packaging choices."},
        "The user confirmed this item.")
    decide_leaf_workspace_proposal(cfg, ids["task_a"], complete["id"], "approve")
    revision = agents.add_leaf_workspace_proposal(ids["task_a"], "revise_plan", {
        "items": [{"id": "research", "text": "Compare and rank the options."},
                  {"id": "publish", "text": "Publish the approved offer."}]},
        "Replace the unstarted draft item.")

    revised = decide_leaf_workspace_proposal(
        cfg, ids["task_a"], revision["id"], "approve")["plan"]

    assert first["version"] == 1 and revised["version"] == 2
    assert revised["items"][0]["id"] == "research"
    assert revised["items"][0]["status"] == "completed"
    assert revised["items"][0]["resolution"] == "Compared all four packaging choices."
    assert revised["items"][1]["id"] == "publish"
    assert revised["items"][1]["status"] == "not_started"
    versions = agents.conn.execute(
        "SELECT version,status FROM goal_leaf_workspace_plan WHERE node_id=? ORDER BY version",
        (ids["task_a"],)).fetchall()
    assert [(row["version"], row["status"]) for row in versions] == [
        (1, "superseded"), (2, "approved")]


def test_leaf_workspace_proposal_cannot_apply_twice(world):
    cfg, _, agents, _, ids = world
    open_leaf_workspace(cfg, ids["task_a"], model=RecordingWorkspaceModel(["Hello."]))
    proposal = agents.add_leaf_workspace_proposal(
        ids["task_a"], "plan", {"items": ["One approved item."]}, "One plan")
    decide_leaf_workspace_proposal(cfg, ids["task_a"], proposal["id"], "approve")

    with pytest.raises(ValueError, match="open proposal"):
        decide_leaf_workspace_proposal(cfg, ids["task_a"], proposal["id"], "approve")
    assert agents.conn.execute(
        "SELECT COUNT(*) FROM goal_leaf_workspace_plan WHERE node_id=?",
        (ids["task_a"],)).fetchone()[0] == 1


def test_leaf_workspace_completed_leaf_can_reopen_or_return_to_shaping(world):
    cfg, goals, agents, _, ids = world
    open_leaf_workspace(cfg, ids["task_a"], model=RecordingWorkspaceModel(["Hello."]))
    plan = agents.add_leaf_workspace_proposal(
        ids["task_a"], "plan", {"items": ["Make one result."]}, "Plan")
    decide_leaf_workspace_proposal(cfg, ids["task_a"], plan["id"], "approve")
    completion = agents.add_leaf_workspace_proposal(
        ids["task_a"], "complete_leaf",
        {"result": "Result made.", "lesson": "The smaller version worked."}, "Complete")
    completed = decide_leaf_workspace_proposal(
        cfg, ids["task_a"], completion["id"], "approve")
    assert completed["phase"] == "reflecting"
    assert completed["completed"] is True
    assert goals.get(ids["task_a"])["status"] == "completed"
    rollup = agents.leaf_workspace_rollup(ids["task_a"])
    assert rollup["completion_confirmed"] is True
    assert rollup["agreement"]["result"] == "Result made."
    assert rollup["agreement"]["lesson"] == "The smaller version worked."
    outcomes = goals.outcomes(ids["task_a"])
    assert len(outcomes) == 1
    assert outcomes[0]["what_happened"] == "Result made."
    assert outcomes[0]["changed_understanding"] == "The smaller version worked."
    assert completed["completion_outcome_id"] == outcomes[0]["id"]
    evidence = goals.conn.execute(
        "SELECT source_kind FROM goal_evidence_link WHERE goal_id=?",
        (ids["task_a"],)).fetchall()
    assert any(link["source_kind"] == "experiment_outcome" for link in evidence)

    reopened = reopen_leaf_workspace(cfg, ids["task_a"])
    assert reopened["phase"] == "doing"
    assert goals.get(ids["task_a"])["status"] == "active"
    assert len(goals.outcomes(ids["task_a"])) == 1
    assert reopened["agreement"]["completion_confirmed"] is False

    reshape = agents.add_leaf_workspace_proposal(
        ids["task_a"], "reshape", {"reason": "The outcome changed."}, "Reshape")
    reshaped = decide_leaf_workspace_proposal(
        cfg, ids["task_a"], reshape["id"], "approve")
    assert reshaped["phase"] == "shaping"
    assert reshaped["agreement"]["confirmed"] is False
    assert reshaped["working"]["current_focus"] == "The outcome changed."
    assert goals.get(ids["task_a"])["status"] == "active"


@pytest.mark.parametrize(("action", "overlap"), [
    ("reopen", False), ("reshape", True),
])
def test_completed_leaf_reactivation_revalidates_horizon_and_overlap(
        world, action, overlap):
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], status="completed")
    if overlap:
        goals.create("task", "Practice particles", parent_id=ids["sub_a"])
        message = "same normalized title"
    else:
        goals.create("task", "Review vocabulary", parent_id=ids["sub_a"])
        goals.create("task", "Practice listening", parent_id=ids["sub_a"])
        message = "horizon"

    with pytest.raises(ValueError, match=message):
        if action == "reopen":
            reopen_leaf_workspace(cfg, ids["task_a"])
        else:
            proposal = agents.add_leaf_workspace_proposal(
                ids["task_a"], "reshape", {"reason": "Try a new shape."}, "Reshape")
            decide_leaf_workspace_proposal(
                cfg, ids["task_a"], proposal["id"], "approve")

    assert goals.get(ids["task_a"])["status"] == "completed"
    pending = agents.leaf_workspace_summary(ids["task_a"])["pending_proposal"]
    assert pending is None
    latest = agents.conn.execute(
        "SELECT id FROM goal_leaf_workspace_proposal WHERE node_id=? ORDER BY id DESC LIMIT 1",
        (ids["task_a"],)).fetchone()
    resolved = agents.leaf_workspace_proposal(int(latest["id"]))
    assert resolved["type"] == action and resolved["status"] == "rejected"


def test_completion_is_one_adaptive_horizon_card_and_retires_standalone_growth(world):
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Publish a study guide", parent_id=ids["over_a"])
    now_leaf = goals.create(
        "task", "Collect examples", parent_id=project,
        description="Produce three confirmed examples.")
    provisional = goals.create(
        "task", "Evaluate examples", parent_id=project,
        description="Compare the examples and choose one.")
    agents.ensure_agents()
    standalone = agents.add_proposal(project, AgentProposal(
        "create_child", project,
        {"type": "task", "title": "Write an extra appendix"},
        "A stale standalone Growth card"))
    open_leaf_workspace(cfg, now_leaf, model=RecordingWorkspaceModel([
        "Let’s collect the examples."]))
    pending = send_leaf_workspace(
        cfg, now_leaf, "The examples are ready.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "The collection is complete; here is the adaptive horizon for review.",
            proposal={"type": "complete_leaf", "payload": {
                "result": "Three examples are ready.",
                "what_happened": "We confirmed examples A, B, and C.",
                "lesson": "Concrete examples make evaluation easier.",
                "adaptive_horizon": {
                    "provisional": {
                        "leaf_id": provisional,
                        "title": "Evaluate and choose one example",
                        "description": "Score A, B, and C and choose one.",
                    },
                    "next_provisional": {
                        "title": "Publish the first study guide",
                        "description": "Publish the chosen example as a small guide.",
                    },
                    "project_continues": True,
                },
            }, "rationale": "NOW is complete and the next horizon is ready."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]

    assert goals.get(now_leaf)["status"] == "active"
    assert agents.get_proposal(standalone)["status"] == "open"
    completed = decide_leaf_workspace_proposal(
        cfg, now_leaf, proposal["id"], "approve")

    open_rows = goals.conn.execute(
        "SELECT id FROM goal_node WHERE parent_id=? AND node_type='task' "
        "AND status IN ('active','paused') ORDER BY position,id", (project,)).fetchall()
    open_ids = [int(row["id"]) for row in open_rows]
    assert goals.get(now_leaf)["status"] == "completed"
    assert open_ids[0] == provisional and len(open_ids) == 2
    assert goals.get(provisional)["title"] == "Evaluate and choose one example"
    assert goals.get(open_ids[1])["title"] == "Publish the first study guide"
    assert completed["completion_replan"]["open_leaf_ids"] == open_ids
    assert agents.get_proposal(standalone)["status"] == "stale"


def test_completing_the_last_leaf_creates_next_leaf_and_wires_its_handoff(world):
    """When no next Leaf exists, next_provisional both creates it AND the new
    Leaf receives the completion handoff — a Leaf born from its predecessor's
    completion must never open empty-handed."""
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Win a first Upwork contract", parent_id=ids["over_a"])
    last_leaf = goals.create(
        "task", "Scan postings and pick one", parent_id=project,
        description="Find one posting worth proposing on.")
    agents.ensure_agents()
    findings = ("Chosen posting: healthcare advisory intake automation. "
                "Budget fixed, few days, wants Claude integration tested on real "
                "data with a plain-English runbook. Four clarifying questions "
                "prepared: intake format, monthly volume, tester, sign-off owner.")
    open_leaf_workspace(cfg, last_leaf, model=RecordingWorkspaceModel([findings]))
    pending = send_leaf_workspace(
        cfg, last_leaf, "That's the one. Complete this Leaf and open the proposal draft.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "Scan complete — handing off to the proposal draft.",
            proposal={"type": "complete_leaf", "payload": {
                "result": "One posting was selected for a proposal.",
                "what_happened": findings,
                "lesson": "Bounded, production-minded postings fit best.",
                "adaptive_horizon": {
                    "next_provisional": {
                        "title": "Draft the advisory-intake proposal",
                        "description": "Write and send the proposal for the chosen posting.",
                    },
                    "project_continues": True,
                },
            }, "rationale": "The scan is done and the draft should start from it."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]
    target = proposal["payload"]["handoff_target"]
    assert target["pending_creation"] is True
    assert target["title"] == "Draft the advisory-intake proposal"
    assert proposal["payload"]["handoff"]["output_summary"]

    completed = decide_leaf_workspace_proposal(
        cfg, last_leaf, proposal["id"], "approve")

    assert goals.get(last_leaf)["status"] == "completed"
    created_ids = completed["completion_replan"]["created_leaf_ids"]
    assert len(created_ids) == 1
    created = goals.get(created_ids[0])
    assert created["title"] == "Draft the advisory-intake proposal"
    assert created["status"] == "active"
    incoming = agents.incoming_leaf_handoffs(int(created["id"]), 3)
    assert len(incoming) == 1
    assert incoming[0]["source_leaf_id"] == last_leaf
    material = incoming[0]["payload"]["working_material"]
    assert "healthcare advisory intake automation" in material


def test_user_pasted_posting_transfers_from_scan_leaf_to_apply_leaf(world):
    """The artifact the next Leaf needs is often pasted by the USER (a job
    posting, a client email) — not written by the assistant. A scan→apply
    handoff must carry it."""
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Run Upwork micro-test", parent_id=ids["over_a"])
    scan_leaf = goals.create(
        "task", "Publish profile and first posting scan", parent_id=project,
        description="Scan postings and pick one worth applying to.")
    apply_leaf = goals.create(
        "task", "Apply to first posting(s) using AI/novelty filter", parent_id=project,
        description="Submit a real proposal to the chosen posting.")
    agents.ensure_agents()
    posting = ("Build a Claude automation that turns our client intakes into a "
               "clean internal brief. We're a small advisory working privately "
               "with individuals and families around psychological wellbeing. "
               "When a new client comes in, their intake should become a clean "
               "internal writeup: their situation, what they need, practitioners "
               "to bring in, sensitivities, and opening questions. Built on "
               "Claude, in our own accounts, with a plain-English runbook, and "
               "tested on real examples before sign-off. Budget fixed, a few days.")
    open_leaf_workspace(cfg, scan_leaf, model=RecordingWorkspaceModel(["Let's scan."]))
    send_leaf_workspace(
        cfg, scan_leaf, "This posting is the one:\n\n" + posting,
        model=RecordingWorkspaceModel(["That posting fits your filter well."]))
    pending = send_leaf_workspace(
        cfg, scan_leaf, "Complete this Leaf and carry the posting forward.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "Scan complete — handing the chosen posting to the apply Leaf.",
            proposal={"type": "complete_leaf", "payload": {
                "result": "One posting was chosen for a proposal.",
                "what_happened": "Scanned postings and chose the advisory-intake one.",
                "lesson": "Bounded scope postings fit best.",
            }, "rationale": "The scan is done."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]
    drafted = proposal["payload"]["handoff"]
    assert proposal["payload"]["handoff_target"]["leaf_id"] == apply_leaf
    assert drafted["artifact_required"] is True
    assert posting in drafted["working_material"]

    decide_leaf_workspace_proposal(cfg, scan_leaf, proposal["id"], "approve")
    incoming = agents.incoming_leaf_handoffs(apply_leaf, 3)
    assert len(incoming) == 1
    assert posting in incoming[0]["payload"]["working_material"]


def test_completing_the_last_leaf_of_a_finished_project_creates_nothing(world):
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Ship the guide", parent_id=ids["over_a"])
    last_leaf = goals.create("task", "Publish the guide", parent_id=project)
    agents.ensure_agents()
    open_leaf_workspace(cfg, last_leaf, model=RecordingWorkspaceModel(["Publishing."]))
    pending = send_leaf_workspace(
        cfg, last_leaf, "Published. The project is done.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "Published — this project is finished.",
            proposal={"type": "complete_leaf", "payload": {
                "result": "The guide is live.", "lesson": "Shipping small worked.",
                "adaptive_horizon": {"project_continues": False},
            }, "rationale": "The project is complete."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]
    assert not (proposal["payload"].get("handoff_target") or {})
    completed = decide_leaf_workspace_proposal(
        cfg, last_leaf, proposal["id"], "approve")
    assert completed["completion_replan"]["created_leaf_ids"] == []
    open_rows = goals.conn.execute(
        "SELECT id FROM goal_node WHERE parent_id=? AND node_type='task' "
        "AND status IN ('active','paused')", (project,)).fetchall()
    assert open_rows == []


def test_stale_adaptive_completion_applies_nothing_and_retires_its_card(world):
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Publish notes", parent_id=ids["over_a"])
    now_leaf = goals.create("task", "Draft notes", parent_id=project)
    provisional = goals.create("task", "Publish notes", parent_id=project)
    open_leaf_workspace(cfg, now_leaf, model=RecordingWorkspaceModel(["Let’s draft."]))
    pending = send_leaf_workspace(
        cfg, now_leaf, "The draft is complete.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "The draft is ready for approval.", proposal={
                "type": "complete_leaf", "payload": {
                    "result": "Notes drafted.", "lesson": "A short draft was enough."},
                "rationale": "Ready."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]
    goals.update(provisional, description="The user changed the next Leaf.")

    with pytest.raises(ValueError, match="stale"):
        decide_leaf_workspace_proposal(cfg, now_leaf, proposal["id"], "approve")

    assert goals.get(now_leaf)["status"] == "active"
    assert goals.outcomes(now_leaf) == []
    assert agents.leaf_workspace_proposal(proposal["id"])["status"] == "rejected"


def test_completed_leaf_hands_approved_work_to_next_project_leaf_without_raw_chat(world):
    cfg, goals, agents, _, ids = world
    area = goals.create("subgoal", "Language work", parent_id=ids["over_a"])
    goals._set_semantic_role(area, "area", rationale="Owns language-related work.")
    project = goals.create("subgoal", "Publish a study guide", parent_id=area)
    goals._set_semantic_role(project, "project", rationale="Produces one finished guide.")
    source = goals.create(
        "task", "Collect examples", parent_id=project, priority="high",
        description="Produce three confirmed examples.")
    destination = goals.create(
        "task", "Evaluate examples", parent_id=project, priority="low",
        due_date="2026-12-01",
        description="Compare the confirmed examples and select one.")
    unrelated = goals.create(
        "task", "Design the cover", parent_id=project, priority="high",
        due_date="2026-01-01",
        description="Design a cover after the content is selected.")
    agents.ensure_agents()
    open_leaf_workspace(cfg, source, model=RecordingWorkspaceModel([
        "Let’s collect the examples together."]))
    agents.add_leaf_workspace_message(source, "user", "RAW PRIVATE SOURCE CONVERSATION")
    pending = send_leaf_workspace(
        cfg, source, "The three examples are ready.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "The confirmed examples are ready for evaluation.",
            proposal={"type": "complete_leaf", "payload": {
                "result": "Three confirmed examples are ready.",
                "what_happened": "We collected examples A, B, and C.",
                "lesson": "Concrete examples made comparison possible.",
            }, "rationale": "The Leaf’s bounded output is complete."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]

    assert proposal["payload"]["handoff_target"]["leaf_id"] == destination
    assert proposal["payload"]["handoff"]["output_summary"]
    edited = dict(proposal["payload"])
    edited["handoff"] = {
        "output_summary": "Three examples are ready to compare.",
        "working_material": "A: particles\nB: honorifics\nC: verb endings",
        "constraints": ["Choose only one for the first guide."],
        "unresolved_questions": "Which example is most useful to a beginner?",
        "suggested_start": "Score A, B, and C for clarity and usefulness.",
    }
    completed = decide_leaf_workspace_proposal(
        cfg, source, proposal["id"], "approve", edited_payload=edited)

    handoff = completed["completion_handoff"]
    assert handoff["source_leaf_id"] == source
    assert handoff["destination_leaf_id"] == destination
    assert handoff["project_id"] == project
    assert handoff["payload"]["working_material"].startswith("A: particles")
    stored = agents.conn.execute(
        "SELECT payload_json FROM goal_leaf_handoff WHERE id=?", (handoff["id"],)).fetchone()[0]
    assert "RAW PRIVATE SOURCE CONVERSATION" not in stored

    destination_context = build_leaf_workspace_context(goals, agents, destination)
    encoded = json.dumps(destination_context, ensure_ascii=False)
    assert "Three examples are ready to compare" in encoded
    assert "RAW PRIVATE SOURCE CONVERSATION" not in encoded
    assert build_leaf_workspace_context(goals, agents, unrelated)["incoming_handoffs"] == []

    destination_model = RecordingWorkspaceModel([
        "I received A, B, and C from Collect examples. Let’s score them now."])
    opened = open_leaf_workspace(cfg, destination, model=destination_model)
    assert opened["messages"][-1]["content"].startswith("I received A, B, and C")
    assert destination_model.calls[-1]["event"]["type"] == "incoming_handoff"
    assert destination_model.calls[-1]["context"]["incoming_handoffs"][0]["id"] == handoff["id"]
    assert agents.leaf_handoff(handoff["id"])["status"] == "consumed"


def test_draft_to_publish_handoff_requires_the_actual_profile_artifact(world):
    cfg, goals, agents, _, ids = world
    area = goals.create("subgoal", "Freelance work", parent_id=ids["over_a"])
    goals._set_semantic_role(area, "area", rationale="Owns freelance work.")
    project = goals.create("subgoal", "Launch Upwork profile", parent_id=area)
    goals._set_semantic_role(project, "project", rationale="Publishes one profile.")
    source = goals.create("task", "Draft Upwork profile", parent_id=project)
    destination = goals.create(
        "task", "Publish profile and first posting scan", parent_id=project)
    agents.ensure_agents()
    profile = """# Upwork Profile

Headline: Business Automation & Data Workflow Specialist

## Overview
I help business owners eliminate repetitive data work by building maintainable
Python automations, reporting workflows, and practical system integrations.

## Employment
Applications Analyst — Parsons — March 2026 to present. Designed production
workflow improvements, database automation, approval flows, and reporting tools.

## Skills
Python; Workflow Automation; Data Automation; SQL; AI Integration; Jira.

## Proposal template
I read your brief and understand the workflow problem. I would map the current
process, build the smallest maintainable automation, test it with real inputs,
and document the handoff for your team.
"""
    open_leaf_workspace(
        cfg, source, model=RecordingWorkspaceModel([profile]))
    pending = send_leaf_workspace(
        cfg, source, "The profile is drafted exactly as above. Complete this Leaf.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "The profile draft is complete.",
            proposal={"type": "complete_leaf", "payload": {
                "result": "A complete Upwork profile is drafted.",
                "what_happened": "Drafted the headline, overview, employment, skills, and proposal.",
                "lesson": "The profile should lead with business workflow outcomes.",
            }, "rationale": "The requested profile artifact is ready."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]
    drafted_handoff = proposal["payload"]["handoff"]

    assert proposal["payload"]["handoff_target"]["leaf_id"] == destination
    assert drafted_handoff["artifact_required"] is True
    assert drafted_handoff["artifact_included"] is True
    assert drafted_handoff["artifact_confidence"] >= 0.9
    assert drafted_handoff["artifact_kind"] == "profile"
    assert profile in drafted_handoff["working_material"]

    completed = decide_leaf_workspace_proposal(
        cfg, source, proposal["id"], "approve")
    handoff = completed["completion_handoff"]
    assert profile in handoff["payload"]["working_material"]

    destination_context = build_leaf_workspace_context(goals, agents, destination)
    incoming = destination_context["incoming_handoffs"][0]["payload"]
    assert incoming["artifact_required"] is True
    assert incoming["artifact_included"] is True
    assert profile in incoming["working_material"]

    # Existing summary-only handoffs are detected but repaired only after the
    # user explicitly chooses the restore action in the destination Leaf.
    legacy_summary = {
        "output_summary": "A complete Upwork profile was drafted.",
        "working_material": "Headline, overview, employment, skills, and proposal were drafted.",
        "constraints": [], "unresolved_questions": "",
        "suggested_start": "Publish the drafted profile.",
    }
    agents.conn.execute(
        "UPDATE goal_leaf_handoff SET payload_json=? WHERE id=?",
        (crypto.enc(json.dumps(legacy_summary)), handoff["id"]))
    agents.conn.commit()
    opened = open_leaf_workspace(
        cfg, destination, model=RecordingWorkspaceModel([
            "I received the older summary-only handoff."]))
    assert opened["incoming_handoffs"][0]["artifact_repair"]["available"] is True
    assert profile not in opened["incoming_handoffs"][0]["payload"]["working_material"]

    repaired = repair_leaf_handoff_artifact(cfg, destination, handoff["id"])
    restored = repaired["incoming_handoffs"][0]
    assert restored["artifact_repair"]["available"] is False
    assert restored["payload"]["artifact_included"] is True
    assert profile in restored["payload"]["working_material"]


def test_completed_legacy_leaf_can_recover_an_approved_handoff_without_duplicate_outcome(world):
    cfg, goals, agents, _, ids = world
    area = goals.create("subgoal", "Work", parent_id=ids["over_a"])
    goals._set_semantic_role(area, "area", rationale="Owns work.")
    project = goals.create("subgoal", "Choose a service", parent_id=area)
    goals._set_semantic_role(project, "project", rationale="Produces one service choice.")
    source = goals.create("task", "Brainstorm candidates", parent_id=project, priority="high")
    destination = goals.create("task", "Evaluate candidates", parent_id=project, priority="high")
    agents.ensure_agents()
    open_leaf_workspace(cfg, source, model=RecordingWorkspaceModel(["Let’s brainstorm."]))
    agents.add_leaf_workspace_message(source, "user", "RAW PRIVATE LEGACY CHAT")
    pending = send_leaf_workspace(
        cfg, source, "The list is finished.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "The list is ready.", proposal={"type": "complete_leaf", "payload": {
                "result": "Four candidates are ready.",
                "what_happened": "We identified cleanup, PDF splitting, data entry, and reports.",
                "lesson": "The candidates need comparison next.",
            }, "rationale": "Ready."})]))
    completion = pending["messages"][-1]["payload"]["proposal"]
    completed = decide_leaf_workspace_proposal(cfg, source, completion["id"], "approve")
    original_handoff = completed["completion_handoff"]
    outcome_count = agents.conn.execute(
        "SELECT COUNT(*) FROM experiment_outcome WHERE goal_id=?", (source,)).fetchone()[0]

    # Recreate the pre-handoff legacy state while preserving its real outcome.
    agents.conn.execute("DELETE FROM goal_leaf_handoff WHERE id=?", (original_handoff["id"],))
    state = agents.leaf_workspace_state(source)
    agreement = dict(state["agreement"])
    agreement.pop("handoff_id", None)
    agreement.pop("handoff_destination_id", None)
    agents.update_leaf_workspace(source, agreement=agreement)
    agents.add_leaf_workspace_message(destination, "user", "An existing destination conversation.")

    drafted = prepare_missing_leaf_handoff(cfg, source)
    proposal = drafted["messages"][-1]["payload"]["proposal"]
    assert proposal["type"] == "handoff_recovery"
    assert proposal["payload"]["handoff_target"]["leaf_id"] == destination
    assert "RAW PRIVATE LEGACY CHAT" not in json.dumps(proposal["payload"], ensure_ascii=False)
    edited = dict(proposal["payload"])
    edited["handoff"] = {
        "output_summary": "Four automation candidates are ready to compare.",
        "working_material": "Cleanup; PDF splitting; data entry; recurring reports",
        "constraints": ["Do not brainstorm again."],
        "unresolved_questions": "Which has the clearest demand?",
        "suggested_start": "Score the four candidates.",
    }
    recovered = decide_leaf_workspace_proposal(
        cfg, source, proposal["id"], "approve", edited_payload=edited)

    handoff = recovered["recovery_handoff"]
    assert goals.get(source)["status"] == "completed"
    assert handoff["destination_leaf_id"] == destination
    assert agents.conn.execute(
        "SELECT COUNT(*) FROM experiment_outcome WHERE goal_id=?", (source,)).fetchone()[0] == outcome_count
    context = build_leaf_workspace_context(goals, agents, destination)
    encoded = json.dumps(context, ensure_ascii=False)
    assert "Four automation candidates are ready to compare" in encoded
    assert "RAW PRIVATE LEGACY CHAT" not in encoded

    destination_model = RecordingWorkspaceModel(["I received the four candidates. Let’s score them."])
    opened = open_leaf_workspace(cfg, destination, model=destination_model)
    assert opened["messages"][-1]["content"].startswith("I received the four candidates")
    assert destination_model.calls[-1]["event"]["type"] == "incoming_handoff"


def test_leaf_workspace_summary_is_compact_and_never_exposes_raw_transcript(world):
    _, _, agents, _, ids = world
    agents.ensure_leaf_workspace({
        "id": ids["task_a"], "type": "task", "title": "Practice particles",
        "description": "", "status": "active"})
    agents.add_leaf_workspace_message(
        ids["task_a"], "user", "RAW PRIVATE TRANSCRIPT")
    agents.update_leaf_workspace(ids["task_a"], working={
        "current_focus": "", "selected_suggestion_ids": [],
        "conversation_summary": "The user chose a one-paragraph practice."})
    proposal = agents.add_leaf_workspace_proposal(
        ids["task_a"], "complete_leaf",
        {"result": "Paragraph written", "lesson": "Short practice worked"},
        "Ready for review")

    summary = agents.leaf_workspace_summary(ids["task_a"])

    assert summary["conversation_summary"] == (
        "The user chose a one-paragraph practice.")
    assert summary["pending_proposal"]["id"] == proposal["id"]
    assert "messages" not in summary
    assert "RAW PRIVATE TRANSCRIPT" not in json.dumps(summary)


def test_leaf_workspace_lazy_migration_preserves_legacy_plan_state_and_transcript(world):
    cfg, goals, agents, _, ids = world
    description = "Practice particles.\n\nSteps:\n1. Open a practice page.\n2. Write one sentence."
    goals.update(ids["task_a"], description=description)
    agents.update_coach_state(
        ids["task_a"], 0, "Open a practice page.", "completed",
        {"resolution": "Opened the lesson and chose 은/는."})
    agents.add_coach_message(
        ids["task_a"], 0, "Open a practice page.", "user",
        {"text": "This is my private legacy transcript."})
    agents.add_coach_message(
        ids["task_a"], 0, "Open a practice page.", "assistant",
        {"reply": "A legacy assistant reply stored under the old key."})
    agents.add_coach_message(
        ids["task_a"], 0, "Open a practice page.", "assistant", {})

    opened = open_leaf_workspace(
        cfg, ids["task_a"], model=RecordingWorkspaceModel(["We can continue from your saved work."]))

    assert opened["phase"] == "doing" and opened["plan"]["version"] == 1
    assert opened["plan"]["items"][0]["status"] == "completed"
    assert opened["plan"]["items"][0]["resolution"] == "Opened the lesson and chose 은/는."
    assert opened["plan"]["items"][1]["status"] == "not_started"
    assert opened["legacy_messages"][0]["content"] == "This is my private legacy transcript."
    assert opened["legacy_messages"][0]["read_only"] is True
    assert opened["legacy_messages"][1]["content"] == (
        "A legacy assistant reply stored under the old key.")
    assert len(opened["legacy_messages"]) == 2
    assert agents.coach_messages(ids["task_a"])[0]["payload"]["text"] == (
        "This is my private legacy transcript.")


def test_parent_context_gets_confirmed_workspace_rollup_not_raw_chat(world):
    cfg, goals, agents, _, ids = world
    open_leaf_workspace(cfg, ids["task_a"], model=RecordingWorkspaceModel(["Hello."]))
    agents.add_leaf_workspace_message(
        ids["task_a"], "user", "RAW PRIVATE LEAF TRANSCRIPT MUST STAY LOCAL")
    proposal = agents.add_leaf_workspace_proposal(ids["task_a"], "agreement", {
        "outcome": "Use particles correctly in one short paragraph.",
        "approach": "Draft, review, and revise one paragraph.",
        "definition_of_done": "The reviewed paragraph uses both target particles.",
    }, "This reflects the explicit agreement.")
    decide_leaf_workspace_proposal(cfg, ids["task_a"], proposal["id"], "approve")

    encoded = json.dumps(build_agent_context(goals, agents, ids["sub_a"]))
    assert "Use particles correctly in one short paragraph" in encoded
    assert "RAW PRIVATE LEAF TRANSCRIPT MUST STAY LOCAL" not in encoded
    assert "goal_leaf_workspace_message" not in encoded


def test_leaf_workspace_context_has_a_hard_final_budget(world):
    _, goals, agents, _, ids = world
    goals.update(ids["task_a"], description="x" * 5000, notes="y" * 5000)
    context = build_leaf_workspace_context(
        goals, agents, ids["task_a"], max_chars=450)
    assert len(json.dumps(context, ensure_ascii=False, sort_keys=True)) <= 450


def test_leaf_workspace_model_failure_preserves_user_turn(world):
    cfg, _, agents, _, ids = world
    open_leaf_workspace(cfg, ids["task_a"], model=RecordingWorkspaceModel(["Ready."]))
    failing = RecordingWorkspaceModel([RuntimeError("workspace unavailable")])

    with pytest.raises(RuntimeError, match="workspace unavailable"):
        send_leaf_workspace(
            cfg, ids["task_a"], "Please explain what you mean.", model=failing)

    messages = agents.leaf_workspace_messages(ids["task_a"])
    assert [message["role"] for message in messages] == ["assistant", "user"]
    assert messages[-1]["content"] == "Please explain what you mean."


def test_leaf_workspace_assistant_reply_persists_atomically(world, monkeypatch):
    cfg, _, agents, _, ids = world
    open_leaf_workspace(cfg, ids["task_a"], model=RecordingWorkspaceModel(["Ready."]))
    original = GoalAgentStore.add_leaf_workspace_message

    def fail_assistant(self, node_id, role, content, payload=None, *, commit=True):
        if role == "assistant" and content == "Here is the proposed plan.":
            raise RuntimeError("simulated assistant save failure")
        return original(self, node_id, role, content, payload, commit=commit)

    monkeypatch.setattr(GoalAgentStore, "add_leaf_workspace_message", fail_assistant)
    reply = LeafWorkspaceReply(
        "Here is the proposed plan.",
        proposal={"type": "plan", "payload": {"items": ["One item"]},
                  "rationale": "Explicit review"})
    with pytest.raises(RuntimeError, match="simulated assistant save failure"):
        send_leaf_workspace(
            cfg, ids["task_a"], "Please draft the plan.",
            model=RecordingWorkspaceModel([reply]))

    assert agents.conn.execute(
        "SELECT COUNT(*) FROM goal_leaf_workspace_proposal WHERE node_id=?",
        (ids["task_a"],)).fetchone()[0] == 0
    assert [message["role"] for message in agents.leaf_workspace_messages(
        ids["task_a"])] == ["assistant", "user"]


def test_clearing_v2_messages_resets_the_workspace_but_keeps_legacy_history(world):
    """Clear is a full workspace reset (conversation, working state, plan,
    agreement) while durable records — legacy coach history, outcomes,
    handoffs — survive."""
    cfg, goals, agents, _, ids = world
    goals.update(ids["task_a"], description="Steps:\n1. Write one sentence.")
    agents.add_coach_message(ids["task_a"], 0, "Write one sentence.", "user",
                             {"text": "legacy remains"})
    open_leaf_workspace(cfg, ids["task_a"], model=RecordingWorkspaceModel(["v2 opening"]))
    agents.update_leaf_workspace(ids["task_a"], working={
        "current_focus": "temporary focus", "selected_suggestion_ids": ["suggestion-1"],
        "conversation_summary": "temporary summary"})
    pending = agents.add_leaf_workspace_proposal(
        ids["task_a"], "agreement", {"outcome": "Hidden draft"}, "Pending")

    cleared = clear_leaf_workspace_messages(cfg, ids["task_a"])

    assert cleared["messages"] == []
    assert cleared["plan"] is None
    assert cleared["phase"] == "shaping"
    assert cleared["legacy_messages"][0]["content"] == "legacy remains"
    assert cleared["working"] == {
        "current_focus": "", "selected_suggestion_ids": [], "conversation_summary": ""}
    assert not cleared["agreement"].get("confirmed")
    assert agents.leaf_workspace_proposal(pending["id"])["status"] == "rejected"


def test_leaf_workspace_private_payloads_are_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVINGPC_DB_KEY", "leaf-workspace-test-key")
    monkeypatch.setenv("LIVINGPC_SALT_FILE", str(tmp_path / "salt"))
    crypto._fernet.cache_clear()
    cfg = cfg_for(str(tmp_path))
    curiosities = CuriosityStore(cfg.memory_db_path)
    goals = GoalStore(cfg.memory_db_path)
    parent = goals.create("overgoal", "Private parent")
    leaf_id = goals.create("task", "Private Leaf", parent_id=parent)
    agents = GoalAgentStore(cfg.memory_db_path)
    model = RecordingWorkspaceModel([LeafWorkspaceReply(
        "Private workspace reply", suggestions=[{"label": "Private option"}],
        proposal={"type": "plan", "payload": {"items": ["Private plan item"]},
                  "rationale": "Private rationale"})])
    opened = open_leaf_workspace(cfg, leaf_id, model=model)
    proposal_id = opened["messages"][-1]["payload"]["proposal"]["id"]
    decide_leaf_workspace_proposal(cfg, leaf_id, proposal_id, "approve")

    state = agents.conn.execute(
        "SELECT agreement_json,working_json FROM goal_leaf_workspace WHERE node_id=?",
        (leaf_id,)).fetchone()
    message = agents.conn.execute(
        "SELECT content,payload_json FROM goal_leaf_workspace_message WHERE node_id=?",
        (leaf_id,)).fetchone()
    proposal = agents.conn.execute(
        "SELECT payload_json,rationale FROM goal_leaf_workspace_proposal WHERE id=?",
        (proposal_id,)).fetchone()
    item = agents.conn.execute(
        "SELECT text FROM goal_leaf_workspace_plan_item ORDER BY plan_id DESC LIMIT 1"
    ).fetchone()
    stored = [*state, *message, *proposal, *item]
    assert all(crypto.is_encrypted(value) for value in stored), [
        (type(value).__name__, crypto.is_encrypted(value), str(value)[:4]) for value in stored]
    agents.close(); goals.close(); curiosities.close()
    crypto._fernet.cache_clear()


def test_renamed_destination_still_detects_and_restores_missing_artifact(world):
    """A rename ('Apply to postings' → 'Draft proposal for advisory intake
    automation') must not hide a summary-only handoff's missing artifact: the
    handoff's own words carry the dependency."""
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Run Upwork micro-test", parent_id=ids["over_a"])
    source = goals.create(
        "task", "Publish profile and first posting scan", parent_id=project)
    destination = goals.create(
        "task", "Draft proposal for advisory intake automation", parent_id=project,
        description="Write and send the response for the chosen work.")
    agents.ensure_agents()
    agents.ensure_leaf_workspace(goals.get(source))
    posting = ("Build a Claude automation that turns our client intakes into a "
               "clean internal brief. Small advisory, privacy matters, fixed "
               "budget, a few days, tested on real examples with a runbook.")
    agents.add_leaf_workspace_message(source, "user", "The posting: " + posting)
    goals.update(source, status="completed")
    outcome_id = agents.conn.execute(
        "INSERT INTO experiment_outcome (goal_id,result,what_happened,created_at) "
        "VALUES (?,?,?,?)",
        (source, "completed", crypto.enc("Scanned postings and chose one."),
         "2026-07-17T00:00:00+00:00")).lastrowid
    # The legacy summary-only handoff, as created before the artifact fixes.
    agents.add_leaf_handoff(source, destination, project, int(outcome_id), {
        "output_summary": "You scanned postings and found one that resonates — "
                          "a bounded Claude automation posting for an advisory.",
        "working_material": "Positioning and four clarifying questions were prepared.",
        "constraints": [], "unresolved_questions": "",
        "suggested_start": "Begin drafting the proposal for the chosen posting.",
    })

    view = _leaf_workspace_view(goals, agents, destination)
    repair = view["incoming_handoffs"][0]["artifact_repair"]
    assert repair["available"] is True

    repaired = repair_leaf_handoff_artifact(
        cfg, destination, view["incoming_handoffs"][0]["id"])
    restored = repaired["incoming_handoffs"][0]
    assert posting in restored["payload"]["working_material"]
    assert restored["artifact_repair"]["available"] is False


def test_prepare_missing_handoff_carries_user_pasted_posting_to_renamed_leaf(world):
    """Luke's live state: scan Leaf completed with NO handoff ever created,
    downstream Leaf renamed to 'Draft proposal…'. Prepare Missing Handoff must
    draft a handoff whose working material includes the user-pasted posting."""
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Run Upwork micro-test", parent_id=ids["over_a"])
    source = goals.create(
        "task", "Publish profile and first posting scan", parent_id=project)
    destination = goals.create(
        "task", "Draft proposal for advisory intake automation", parent_id=project,
        description="Submit proposals to one or more postings that pass the "
                    "cool/novel/techy filter. Use the proposal template from "
                    "the draft phase.")
    agents.ensure_agents()
    agents.ensure_leaf_workspace(goals.get(source))
    posting = ("Build a Claude automation that turns our client intakes into a "
               "clean internal brief. Small advisory, privacy matters, fixed "
               "budget, a few days, tested on real examples with a runbook.")
    agents.add_leaf_workspace_message(source, "user", "The posting: " + posting)
    goals.update(source, status="completed")
    agents.conn.execute(
        "INSERT INTO experiment_outcome (goal_id,result,what_happened,created_at) "
        "VALUES (?,?,?,?)",
        (source, "completed", crypto.enc("Scanned postings and chose one."),
         "2026-07-17T00:00:00+00:00"))
    agents.conn.commit()

    view = _leaf_workspace_view(goals, agents, source)
    assert view["handoff_recovery"]["eligible"] is True
    assert view["handoff_recovery"]["target"]["leaf_id"] == destination

    prepared = prepare_missing_leaf_handoff(cfg, source)
    proposal = prepared["messages"][-1]["payload"]["proposal"]
    assert proposal["type"] == "handoff_recovery"
    drafted = proposal["payload"]["handoff"]
    assert drafted["artifact_required"] is True
    assert posting in drafted["working_material"]

    approved = decide_leaf_workspace_proposal(cfg, source, proposal["id"], "approve")
    assert approved["recovery_handoff"]["destination_leaf_id"] == destination
    incoming = agents.incoming_leaf_handoffs(destination, 3)
    assert posting in incoming[0]["payload"]["working_material"]


def test_clearing_a_leaf_workspace_resets_agreement_plan_and_phase(world):
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Reset test project", parent_id=ids["over_a"])
    leaf = goals.create("task", "Reset me", parent_id=project)
    agents.ensure_agents()
    open_leaf_workspace(cfg, leaf, model=RecordingWorkspaceModel(["Shaping."]))
    pending = send_leaf_workspace(
        cfg, leaf, "Agree and plan it.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "Here is the agreement.", proposal={"type": "agreement", "payload": {
                "outcome": "A tidy outcome.", "approach": "Small steps.",
            }, "rationale": "Shaped."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]
    decide_leaf_workspace_proposal(cfg, leaf, proposal["id"], "approve")
    assert agents.leaf_workspace_state(leaf)["agreement"].get("confirmed") is True

    cleared = clear_leaf_workspace_messages(cfg, leaf)
    assert cleared["messages"] == []
    state = agents.leaf_workspace_state(leaf)
    assert state["phase"] == "shaping"
    assert not state["agreement"].get("confirmed")
    assert agents.leaf_workspace_plan(leaf) is None
    # Reopening restarts the conversation with a fresh opening message.
    reopened = open_leaf_workspace(
        cfg, leaf, model=RecordingWorkspaceModel(["Fresh start — what fits?"]))
    assert len(reopened["messages"]) == 1


def test_leaf_workspace_context_carries_the_voice_profile(world):
    cfg, goals, agents, _, ids = world
    context = build_leaf_workspace_context(goals, agents, ids["task_a"])
    profile = context.get("voice_profile") or ""
    assert "Luke's Writing Voice" in profile
    assert "NEVER invent experience" in profile
    assert "voice_profile" in LEAF_WORKSPACE_SYSTEM
    assert "never" in LEAF_WORKSPACE_SYSTEM and "invent experience" in LEAF_WORKSPACE_SYSTEM


def test_voice_profile_survives_prompt_budget_truncation(world):
    """A Leaf with a huge restored handoff must not silently lose the user's
    voice rules to the context budget."""
    cfg, goals, agents, _, ids = world
    project = goals.create("subgoal", "Voice budget project", parent_id=ids["over_a"])
    source = goals.create("task", "Scan for postings", parent_id=project)
    destination = goals.create("task", "Draft the proposal", parent_id=project)
    agents.ensure_agents()
    agents.conn.execute(
        "INSERT INTO experiment_outcome (goal_id,result,what_happened,created_at) "
        "VALUES (?,?,?,?)", (source, "completed", crypto.enc("Chose a posting."),
                             "2026-07-17T00:00:00+00:00"))
    agents.conn.commit()
    goals.update(source, status="completed")
    agents.add_leaf_handoff(source, destination, project, 1, {
        "output_summary": "A posting was chosen.",
        "working_material": "posting detail line\n" * 700,   # ~12KB artifact
        "constraints": [], "unresolved_questions": "",
        "suggested_start": "Draft the proposal from the posting.",
    })
    context = build_leaf_workspace_context(goals, agents, destination, max_chars=6000)
    assert context.get("prompt_budget_truncated") is True
    assert "Luke's Writing Voice" in (context.get("voice_profile") or "")


def test_approved_leaf_completion_awards_xp_once(world):
    cfg, goals, agents, _, ids = world
    from livingpc.curiosity_metrics import MetricStore
    project = goals.create("subgoal", "XP project", parent_id=ids["over_a"])
    leaf = goals.create("task", "Earn some XP", parent_id=project)
    second = goals.create("task", "Later step", parent_id=project)
    agents.ensure_agents()
    open_leaf_workspace(cfg, leaf, model=RecordingWorkspaceModel(["Working."]))
    pending = send_leaf_workspace(
        cfg, leaf, "Done. Complete it.",
        model=RecordingWorkspaceModel([LeafWorkspaceReply(
            "Complete.", proposal={"type": "complete_leaf", "payload": {
                "result": "Done.", "lesson": "Small steps."},
                "rationale": "Finished."})]))
    proposal = pending["messages"][-1]["payload"]["proposal"]
    decide_leaf_workspace_proposal(cfg, leaf, proposal["id"], "approve")

    metrics = MetricStore(cfg.memory_db_path)
    try:
        rows = metrics.conn.execute(
            "SELECT xp, source_key FROM curiosity_metric_event").fetchall()
        assert [(r["xp"], r["source_key"]) for r in rows] == [
            (25, f"leaf-completion:{leaf}")]
        assert metrics.global_xp() == 25
        # Completing the project's last Leaf is milestone-tier.
        open_leaf_workspace(cfg, second, model=RecordingWorkspaceModel(["Go."]))
        pending2 = send_leaf_workspace(
            cfg, second, "Also done — project finished.",
            model=RecordingWorkspaceModel([LeafWorkspaceReply(
                "Complete.", proposal={"type": "complete_leaf", "payload": {
                    "result": "Done.", "lesson": "Ship.",
                    "adaptive_horizon": {"project_continues": False},
                }, "rationale": "Project over."})]))
        proposal2 = pending2["messages"][-1]["payload"]["proposal"]
        decide_leaf_workspace_proposal(cfg, second, proposal2["id"], "approve")
        assert metrics.global_xp() == 75  # 25 + 50 milestone
    finally:
        metrics.close()
