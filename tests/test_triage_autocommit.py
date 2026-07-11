"""Triage: confident facts auto-commit; low-confidence facts + questions drop."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.triage.types import TriageResult, Statement, Supersession, Question  # noqa: E402
from livingpc.triage.pipeline import apply_result  # noqa: E402


def test_confident_facts_autocommit_low_confidence_dropped():
    with tempfile.TemporaryDirectory() as d:
        mem = MemoryStore(os.path.join(d, "m.db"))
        base = mem.add("Work", "editor", "VS Code")     # target for supersession

        result = TriageResult(
            statements=[
                Statement("Music", "favorite genre", "jazz", 0.90, ""),   # auto
                Statement("Food", "maybe likes", "sushi?", 0.50, ""),     # dropped
            ],
            supersessions=[
                Supersession(memory_id=base, value="Neovim", confidence=0.95),  # auto
            ],
            questions=[Question("Why jazz lately?", "Music")],            # dropped
        )

        counts = apply_result(mem, result, "2026-07-01")
        assert counts == {"auto_committed": 2, "dropped": 2}

        active = {(m["category"], m["attribute"]): m["value"]
                  for m in mem.active_as_dicts()}
        assert active[("Music", "favorite genre")] == "jazz"
        assert active[("Work", "editor")] == "Neovim"        # supersession applied
        assert ("Food", "maybe likes") not in active         # low-confidence dropped
        assert mem.list_pending() == []                      # nothing parked for review
        mem.close()


def test_threshold_is_configurable():
    with tempfile.TemporaryDirectory() as d:
        mem = MemoryStore(os.path.join(d, "m.db"))
        result = TriageResult(statements=[Statement("X", "y", "z", 0.80, "")])
        counts = apply_result(mem, result, "2026-07-01", auto_commit_confidence=0.90)
        assert counts == {"auto_committed": 0, "dropped": 1}
        assert mem.active_as_dicts() == []
        mem.close()


def test_clears_any_legacy_pending():
    with tempfile.TemporaryDirectory() as d:
        mem = MemoryStore(os.path.join(d, "m.db"))
        mem.add_pending("question", {"text": "old", "category": ""}, "2026-06-01")
        assert mem.list_pending()                            # a legacy row exists
        apply_result(mem, TriageResult(), "2026-07-01")      # empty result
        assert mem.list_pending() == []                      # purged
        mem.close()


def test_apply_and_watermark_roll_back_together_on_failure():
    with tempfile.TemporaryDirectory() as d:
        mem = MemoryStore(os.path.join(d, "m.db"))
        result = TriageResult(statements=[
            Statement("A", "one", "first", 0.99, ""),
            Statement("B", "two", "second", 0.99, ""),
        ])
        original_add = mem.add
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated write failure")
            return original_add(*args, **kwargs)

        mem.add = fail_second
        with pytest.raises(RuntimeError):
            apply_result(mem, result, "2026-07-01", watermark="end")
        assert mem.active_as_dicts() == []
        assert mem.get_meta("last_triage_ts") is None
        mem.close()


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn(); print("PASS " + fn.__name__)
        except Exception:
            fails += 1; print("FAIL " + fn.__name__); traceback.print_exc()
    print("%d/%d passed" % (len(fns) - fails, len(fns)))
    sys.exit(1 if fails else 0)
