import os
import tempfile
from pathlib import Path

from agent_window import AgentWindowApi
from livingpc.config import Config
from livingpc.curiosity import CuriosityStore
from livingpc.goal_ai import AgentProposal, GoalAgentStore
from livingpc.goals import (GoalStore, StubGoalPlanner, start_planning,
                            summarize_plan)
from livingpc.inference import InferenceStore


def _cfg(folder):
    return Config(memory_db_path=os.path.join(folder, "memory.db"),
                  db_path=os.path.join(folder, "events.db"),
                  inference_backend="stub", curiosity_backend="stub",
                  goal_ai_backend="stub")


def test_inference_native_window_commits_explicit_decision():
    with tempfile.TemporaryDirectory() as folder:
        cfg = _cfg(folder)
        inf = InferenceStore(cfg.memory_db_path)
        candidate = inf.add_candidate("focus", "You focus when progress is visible.",
                                      confidence=.9)
        inquiry = inf.start_inquiry("address", "You focus when progress is visible.",
                                    inference_id=candidate)
        inf.update_inquiry_draft(inquiry, "I focus when progress is visible.", .8)
        inf.close()
        api = AgentWindowApi("inference", inquiry, cfg)
        assert api.state()["ok"]
        committed = api.commit({"outcome": "accepted",
                                "statement": "I focus when progress is visible."})
        assert committed["ok"] and committed["canonical_id"]


def test_goal_agent_native_window_stages_then_commits_selected_proposal():
    with tempfile.TemporaryDirectory() as folder:
        cfg = _cfg(folder)
        curiosities = CuriosityStore(cfg.memory_db_path)
        goals = GoalStore(cfg.memory_db_path)
        root = goals.create("overgoal", "Korean")
        agents = GoalAgentStore(cfg.memory_db_path)
        proposal = agents.add_proposal(
            root, AgentProposal("create_child", root,
                                {"type": "task", "title": "Practice"}, "Next step"))
        agents.close(); goals.close(); curiosities.close()
        api = AgentWindowApi("goal-agent", root, cfg)
        before = api.state()
        assert before["agent"]["proposals"][0]["status"] == "open"
        result = api.commit({"proposal_ids": [proposal], "memory_ids": []})
        assert result["ok"]
        goals = GoalStore(cfg.memory_db_path)
        try:
            assert any(c["title"] == "Practice" for c in goals.tree()["children"][0]["children"])
        finally:
            goals.close()


def test_planner_window_reviews_placement_after_chat_before_commit():
    with tempfile.TemporaryDirectory() as folder:
        cfg = _cfg(folder)
        curiosities = CuriosityStore(cfg.memory_db_path)
        cid = curiosities.add_curiosity("protect energy", "Energy")
        item = curiosities.add_item(
            cid, "suggestion", "Map work tasks that drain health and energy.")
        goals = GoalStore(cfg.memory_db_path)
        health = goals.create("overgoal", "Health & Energy")
        session = start_planning(goals, StubGoalPlanner(), item, health, {
            "mode": "existing", "parent_id": health,
            "parent_path": "Actualized Self › Health & Energy",
            "user_confirmed": False, "review_required": True,
        })
        summarize_plan(goals, StubGoalPlanner(), session["id"])
        goals.close(); curiosities.close()

        api = AgentWindowApi("goal-planner", session["id"], cfg)
        state = api.state()
        assert state["ok"] and state["session"]["status"] == "ready"
        assert state["placement_review"]["recommended_parent_id"] == health
        committed = api.commit({
            "draft": state["session"]["draft"],
            "placement": {"target_parent_id": health, "mode": "existing",
                          "parent_id": health, "user_confirmed": True},
        })
        assert committed["ok"] and committed["goal_id"]


def test_harvest_native_window_commits_edited_draft():
    with tempfile.TemporaryDirectory() as folder:
        cfg = _cfg(folder)
        goals = GoalStore(cfg.memory_db_path)
        node = goals.create("overgoal", "Korean")
        agents = GoalAgentStore(cfg.memory_db_path)
        harvest = agents.create_harvest(node, {"summary": "first", "insights": [], "routes": []})
        agents.close(); goals.close()
        api = AgentWindowApi("goal-harvest", harvest["id"], cfg)
        result = api.commit({"draft": {"summary": "edited", "insights": [], "routes": []}})
        assert result["ok"] and result["harvest"]["draft"]["summary"] == "edited"


def test_native_agent_window_can_minimize_and_close_even_while_working():
    class FakeWindow:
        minimized = False
        destroyed = False

        def minimize(self):
            self.minimized = True

        def destroy(self):
            self.destroyed = True

    with tempfile.TemporaryDirectory() as folder:
        api = AgentWindowApi("goal-planner", 1, _cfg(folder))
        window = FakeWindow()
        api._window = window
        assert api.minimize()["ok"] and window.minimized
        assert api.close() and window.destroyed

    html = Path("livingpc/ui/agent_window.html").read_text(encoding="utf-8")
    assert 'id="minimize"' in html
    assert 'id="close"' in html and 'aria-label="Close agent">×' in html
    assert "['close','minimize','expand']" in html
    assert "user-select:text" in html and "text-context-menu" in html
    assert "clipboard_write" in html and "clipboard_read" in html
    assert 'class="drag-handle pywebview-drag-region"' in html
    assert "Drag to move this agent" in html
    assert "thinkingHtml" in html and "thinking-dots" in html
    assert "Faerie is crafting a response" in html
    assert "e.key==='Enter'&&!e.shiftKey&&!e.isComposing" in html
    assert "AtkinsonHyperlegible-Regular.ttf" in html
    assert "AtkinsonHyperlegible-Bold.ttf" in html
    assert "conversationHtml" in html and r"\*\*((?:[^*\n]|\*(?!\*))+)\*\*" in html
    assert "<strong>$1</strong>" in html and "<em>$2</em>" in html
    assert "<strong><em>$1</em></strong>" in html
    assert "Summarize & review" in html and "Confirm placement & create plan" in html

    source = Path("agent_window.py").read_text(encoding="utf-8")
    assert "text_select=True" in source
    assert "on_top=False" in source
    assert "on_top=True" not in source


def test_agent_window_pins_composer_to_the_bottom_across_all_modes():
    """The reply composer sits in a fixed bottom bar while the conversation
    scrolls above it, matching the main chats — for every agent mode."""
    html = Path("livingpc/ui/agent_window.html").read_text(encoding="utf-8")

    # Body is a full-height flex column; the composer is a non-shrinking bottom bar.
    assert "height:100vh;display:flex;flex-direction:column;overflow:hidden" in html
    assert ".chat-scroll{flex:1 1 auto;min-height:0;overflow:auto" in html
    assert ".composer{flex:0 0 auto;border-top:1px solid var(--line)" in html

    # A single shell() wraps scroll content + composer; every mode routes through it.
    assert "function shell(scrollHtml,composerHtml)" in html
    assert html.count("app.innerHTML=shell(") == 4  # inference, goal-agent, harvest, planning

    # The reply box lives in the composer, not inline after the messages.
    assert "id=\"reply\"" in html
    # Auto-scroll targets the new scroll container, not the messages list.
    assert "function scrollMsgs(){const m=document.querySelector('.chat-scroll')" in html
