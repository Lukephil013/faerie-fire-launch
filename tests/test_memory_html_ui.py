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
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "livingpc" / "ui" / "memory.html"


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _script() -> str:
    text = _html()
    match = re.search(r"<script>\s*(.*?)\s*</script>", text, re.DOTALL)
    assert match, "memory.html should contain one inline script block"
    return match.group(1)


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
    result = subprocess.run(
        ["node", "--check", "-"],
        input=_script(),
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        capture_output=True,
        check=False,
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
