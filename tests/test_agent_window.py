import os
import tempfile

from agent_window import AgentWindowApi
from livingpc.config import Config
from livingpc.curiosity import CuriosityStore
from livingpc.goal_ai import AgentProposal, GoalAgentStore
from livingpc.goals import GoalStore
from livingpc.inference import InferenceStore


def _cfg(folder):
    return Config(memory_db_path=os.path.join(folder, "memory.db"),
                  db_path=os.path.join(folder, "events.db"),
                  inference_backend="stub", goal_ai_backend="stub")


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
