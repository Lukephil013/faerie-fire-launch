"""Tests for rejection memory + the declined block in the prompt."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ensure no key so values store/compare as plaintext for these tests
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.triage.llm import build_user_prompt, StubBackend  # noqa: E402


def test_rejection_roundtrip_and_clear():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        m.add_rejection("statement", "Music", "Huberman Lab podcast")
        m.add_rejection("question", "Music", "do you listen regularly?")
        assert m.count_rejections() == 2
        recent = m.recent_rejections()
        labels = [r["label"] for r in recent]
        assert "Huberman Lab podcast" in labels
        m.clear_rejections()
        assert m.count_rejections() == 0
        assert m.recent_rejections() == []
        m.close()


def test_rejection_cap():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        for i in range(40):
            m.add_rejection("statement", "X", f"fact {i}")
        recent = m.recent_rejections(limit=25)
        assert len(recent) == 25                  # capped
        # newest first
        assert recent[0]["label"] == "fact 39"
        m.close()


def test_prompt_includes_declined_block():
    active = [{"id": 1, "category": "Music", "attribute": "x", "value": "y"}]
    declined = [{"kind": "statement", "category": "Music", "label": "Huberman Lab podcast"}]
    prompt = build_user_prompt("SUMMARY HERE", active, declined)
    assert "RECENTLY DECLINED" in prompt
    assert "Huberman Lab podcast" in prompt
    assert "do NOT re-propose" in prompt
    # without declined, no block
    p2 = build_user_prompt("S", active, None)
    assert "RECENTLY DECLINED" not in p2


def test_generate_replaces_not_stacks():
    """Simulate the clear-before-add behavior the generate path now uses."""
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        # first batch
        m.clear_pending()
        for k in range(3):
            m.add_pending("statement", {"category": "X", "attribute": "a",
                                        "value": f"v{k}"}, "2026-06-24")
        assert m.count_pending() == 3
        # second "generate": clear then add -> should NOT stack to 6
        m.clear_pending()
        for k in range(2):
            m.add_pending("statement", {"category": "X", "attribute": "a",
                                        "value": f"w{k}"}, "2026-06-24")
        assert m.count_pending() == 2
        m.close()


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            fails += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns)-fails}/{len(fns)} passed")
    sys.exit(1 if fails else 0)
