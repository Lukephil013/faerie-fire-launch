import os
import tempfile

from livingpc.config import Config
from livingpc.memory import MemoryStore
from livingpc.today_focus import get_today_focus


def _tree(title="Write a draft", updated_at="2026-07-11T10:00:00+00:00"):
    return {
        "id": 1, "type": "umbrella", "children": [{
            "id": 2, "type": "task", "title": title, "description": "A short concrete draft.",
            "status": "active", "priority": "high", "due_date": None,
            "completion": None, "mastery": None, "updated_at": updated_at, "children": [],
        }],
    }


def test_focus_cache_invalidates_when_task_context_changes():
    with tempfile.TemporaryDirectory() as directory:
        cfg = Config(memory_db_path=os.path.join(directory, "memory.db"),
                     inference_backend="stub")
        mem = MemoryStore(cfg.memory_db_path)
        try:
            first = get_today_focus(cfg, mem, _tree())
            cached = get_today_focus(cfg, mem, _tree())
            changed = get_today_focus(cfg, mem, _tree("Revise the draft", "2026-07-11T11:00:00+00:00"))
        finally:
            mem.close()
        assert first["source"] == "fallback"
        assert cached["source"] == "cached"
        assert changed["source"] == "fallback"
        assert changed["picks"][0]["title"] == "Revise the draft"
