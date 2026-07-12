"""Fast checks for memory.html UI behavior without launching pywebview.

These tests intentionally avoid a browser dependency.  They cover the fragile
parts of the inline Growth UI script that are easy to regress while editing the
single large HTML file:

- the embedded JavaScript must parse;
- submitted Growth focus questions are filtered from future cards;
- the submit flow replaces answered boxes with pending/loading states before
  GoalAI refreshes follow-up questions.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "livingpc" / "ui" / "memory.html"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _scripts() -> list[str]:
    scripts = re.findall(r"<script>\s*(.*?)\s*</script>", _html(), re.DOTALL)
    assert scripts, "memory.html should contain inline script blocks"
    return scripts


def _script() -> str:
    """The application block, rather than the small early crash banner."""
    return max(_scripts(), key=len)


def _function_body(script: str, name: str) -> str:
    start_match = re.search(rf"\bfunction\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", script)
    assert start_match, f"{name}() not found"
    start = start_match.end()
    depth = 1
    idx = start
    while idx < len(script) and depth:
        char = script[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        idx += 1
    assert depth == 0, f"{name}() body did not close"
    return script[start:idx - 1]


def test_memory_html_inline_script_parses_with_node():
    node = shutil.which("node")
    if not node:
        return
    for script in _scripts():
        result = subprocess.run(
            [node, "--check", "-"], input=script, text=True, encoding="utf-8",
            cwd=ROOT, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr or result.stdout


def test_growth_focus_questions_filter_already_submitted_answers():
    script = _script()
    answered = _function_body(script, "goalAnsweredQuestionKeys")
    evidence_questions = _function_body(script, "goalEvidenceQuestions")

    assert "ev.source_kind!=='focus_answer'" in answered
    assert "match(/^Question:" in answered
    assert "goalFocusPending" in answered
    assert "goalQuestionKey(match[1])" in answered

    assert "goalAnsweredQuestionKeys(node)" in evidence_questions
    assert ".filter(q=>!answered.has(goalQuestionKey(q.question)))" in evidence_questions


def test_submit_answers_shows_pending_state_before_goalai_review():
    body = _function_body(_script(), "submitGoalFocusAnswers")

    first_pending = body.find("title:'Saving answers…'")
    first_render = body.find("renderGoalFocusPanel();", first_pending)
    first_api = body.find("pywebview.api.goal_add_evidence")
    assert -1 not in (first_pending, first_render, first_api)
    assert first_pending < first_render < first_api

    review_pending = body.find("title:'Generating follow-up questions…'")
    review_call = body.find("pywebview.api.goal_ai_review")
    reload_call = body.find("pywebview.api.goal_state")
    agent_reload = body.find("pywebview.api.goal_ai_state")
    clear_pending = body.find("goalFocusPending=null")
    assert -1 not in (review_pending, review_call, reload_call, agent_reload, clear_pending)
    assert review_pending < review_call < reload_call < agent_reload < clear_pending


def test_investigation_synthesis_is_explicit_and_reviewable():
    script = _script()
    render = _function_body(script, "curSynthesisHtml")
    bind = _function_body(script, "bindCurCard")

    assert "cur.synthesis_due" in render
    assert "new experiment outcome" in render
    assert "Previous approved interpretation" in render
    assert "Approve edited interpretation" in render
    assert "Review with new evidence" in render
    assert "curiosity_synthesize" in bind
    assert "curiosity_synthesis_decide" in bind
    assert "curiosity_person_reconcile" in bind
    assert "curiosity_person_proposal" in bind
    assert "prior wording remains in history" in bind


def test_suggested_investigations_are_bounded_and_never_autostart():
    script = _script()
    render = _function_body(script, "curCandidatePanelHtml")
    bind = _function_body(script, "bindCandidatePanel")

    assert "2 shown" in render
    assert "Nothing starts automatically" in render
    assert "Never suggest this topic" in render
    assert "What this could change" in render
    assert "curiosity_candidate_suggest" in bind
    assert "curiosity_candidate_action" in bind
    assert "candidate-start" in bind
    assert "sensitive topic" in bind


def test_tree_gardening_explains_new_evidence_and_requires_approval():
    script = _script()
    relevance = _function_body(script, "goalRelevanceHtml")
    bind = _function_body(script, "bindGoalRelevanceControls")
    history = _function_body(script, "goalArchivedHistoryHtml")

    assert "Newer evidence behind this prompt" in relevance
    assert "Gardening proposals" in relevance
    assert "Prior relevance reviews" in relevance
    assert "goal_relevance_review" in bind
    assert "goal_gardening_proposal" in bind
    assert "Tree change approved; history was preserved." in bind
    assert "Archived history" in history
    assert "without cluttering the current map" in history


def test_leaf_outcomes_capture_learning_and_feed_the_next_experiment():
    script = _script()
    render = _function_body(script, "goalOutcomeHtml")
    bind = _function_body(script, "bindGoalOutcomeControls")
    focus = _function_body(script, "bindGoalFocusPanel")

    for phrase in ("What happened?", "What obstacle did you expect?",
                   "What surprised you?", "How helpful was this?",
                   "What changed in your understanding?",
                   "What should the next experiment adjust?"):
        assert phrase in render
    assert "Completed" in render and "Avoided" in render and "Abandoned intentionally" in render
    assert "Next experiment should reflect this" in render
    assert "goal_experiment_outcome" in bind
    assert "lower-confidence interpretation is ready for review" in bind
    assert "Record what happened so Faerie can learn" in focus


def test_new_growth_nodes_use_definition_first_progressive_disclosure():
    script = _script()
    gate = _function_body(script, "goalNodeNeedsDefinition")
    starter = _function_body(script, "goalDefinitionStarterHtml")
    render = _function_body(script, "renderGoalFocusPanel")

    assert "node.type==='umbrella'" in gate
    assert "node.description" in gate and "node.notes" in gate
    assert "node.children" in gate and "node.evidence" in gate
    assert "node.curiosities" in gate and "node.outcomes" in gate
    assert "origin.summary" in gate and "origin.source_label" in gate
    assert "What this Leaf asks you to do" in _function_body(script, "goalDefinitionLabel")
    assert "goal-focus-draft-steps" in starter
    assert "goalNodeNeedsDefinition(node)" in render
    assert render.find("goalNodeNeedsDefinition(node)") < render.find("goalRecapHtml(")
    assert "return;" in render[render.find("goalNodeNeedsDefinition(node)"):render.find("goalRecapHtml(")]


def test_outcome_form_fields_stack_at_full_width():
    html = _html()
    assert ".outcome-form > label { display:grid; grid-template-columns:1fr" in html
    assert ".outcome-form textarea,.outcome-form select { display:block; width:100%; min-width:0;" in html
    assert ".outcome-form textarea { min-height:68px; resize:vertical;" in html


def test_soul_calibration_plain_enter_remains_multiline():
    script = _script()
    bind_calibration = _function_body(script, "bindSoulCalDrawer")
    render = _function_body(script, "renderSoulCalDrawer")
    generic_submit = _function_body(script, "bindShiftEnterSubmit")
    command_center = _function_body(script, "bindCommandCenter")

    assert '<textarea id="soul-cal-answer"' in render
    assert "bindShiftEnterSubmit(textarea" not in bind_calibration
    assert "e.ctrlKey||e.metaKey" in bind_calibration
    assert "!e.isComposing" in bind_calibration

    # The rest of the app retains its existing Enter-to-submit behavior.
    assert "e.key==='Enter' && !e.shiftKey" in generic_submit
    assert "e.preventDefault()" in generic_submit
    assert "bindShiftEnterSubmit(input, sendCommandMessage)" in command_center


def test_soul_calibration_sections_follow_the_active_question():
    checklist = _function_body(_script(), "soulCalChecklistHtml")

    assert '<details class="soul-calibration-section' in checklist
    assert 'data-cal-section="' in checklist
    assert "const activeSection=active&&active.section" in checklist
    assert "sec.section===activeSection?' open':''" in checklist
    assert "covered===attrs.length" in checklist


def test_documents_attach_to_calibration_and_investigation_context():
    script = _script()
    calibration = _function_body(script, "renderSoulCalDrawer")
    question = _function_body(script, "curQuestionHtml")
    card = _function_body(script, "curCardHtml")
    binder = _function_body(script, "bindContextDocuments")

    assert "active.attachments" in calibration
    assert "active.attachment_key" in calibration
    assert "item.context_attachments" in question
    assert "curDocumentContextHtml(cur)" in card
    assert "context_attachment_add" in binder
    assert "context_attachment_remove" in binder
    assert "setRangeText" in binder
