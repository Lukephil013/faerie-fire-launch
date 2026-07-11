"""Tests for the inference store (Phase A of the inference engine)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.inference import InferenceStore, CORE_BELIEF_CONFIRMATIONS  # noqa: E402


def _store(d):
    return InferenceStore(os.path.join(d, "memory.db"))


def test_add_and_review_stack():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        a = s.add_candidate("attention", "You gravitate to strategy content",
                            confidence=0.8, evidence={"dwell_s": 720},
                            source_refs=[1, 2])
        s.add_candidate("attention", "You research before deciding", confidence=0.6)
        # test the raw stacking primitive (no confidence gate)
        stack = s.to_review(min_confidence=0.0)
        assert len(stack) == 2
        # higher confidence surfaces first
        assert stack[0]["id"] == a
        assert stack[0]["evidence"]["dwell_s"] == 720
        assert stack[0]["source_refs"] == [1, 2]
        s.close()


def test_confirm_raises_confidence_and_reaches_core_belief():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        i = s.add_candidate("values", "You value autonomy", confidence=0.5)
        prev = 0.5
        for _ in range(CORE_BELIEF_CONFIRMATIONS):
            s.confirm(i)
            row = s.get(i)
            assert row["confidence"] >= prev      # confidence climbs each yes
            prev = row["confidence"]
        conf = s.confirmed()
        assert len(conf) == 1 and conf[0]["is_core_belief"] is True
        assert conf[0]["times_confirmed"] == CORE_BELIEF_CONFIRMATIONS
        # not shown in the review stack anymore
        assert s.to_review() == []
        s.close()


def test_reject_becomes_negative_constraint_and_parks_theme():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        for k in range(4):
            i = s.add_candidate("motivation", f"You do X because reason {k}")
            s.reject(i)
        # rejected statements are available to steer the next loop
        neg = s.rejected_for_theme("motivation")
        assert len(neg) == 4
        assert "reason 3" in neg[0]                 # newest first
        # four rejections -> the theme is parked
        assert "motivation" in s.parked_themes()
        assert s.to_review() == []                  # rejected leave the stack
        s.close()


def test_kind_of_flags_partial():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        i = s.add_candidate("identity", "You see yourself as a builder", confidence=0.4)
        s.kind_of(i)
        assert s.get(i)["status"] == "partial"
        assert [p["id"] for p in s.partials()] == [i]
        assert s.to_review() == []                  # partials leave the candidate stack
        s.close()


def test_refine_retires_guess_and_stores_user_truth():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        i = s.add_candidate("identity", "You want to be seen as smart", confidence=0.6)
        new_id = s.refine(i, "I want to be seen as capable, not just smart")
        assert s.get(i)["status"] == "retired"
        new = s.get(new_id)
        assert new["status"] == "confirmed"
        assert new["refines_id"] == i
        assert "capable" in new["statement"]
        assert new["confidence"] >= 0.9             # your wording ~= truth
        s.close()


def test_skip_temporarily_hides_but_keeps_candidate():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        a = s.add_candidate("focus", "A", confidence=0.9)
        b = s.add_candidate("focus", "B", confidence=0.9)
        s.skip(a)                                   # a skipped once
        stack = s.to_review()
        assert [x["id"] for x in stack] == [b]      # hidden for this review window
        assert s.get(a)["status"] == "candidate"    # but still available later
        s.close()


def test_refine_lineage_for_re_hypothesis():
    """A rejected guess can be replaced by a new candidate that references it."""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        first = s.add_candidate("why-lol", "You play LoL to relax")
        s.reject(first)
        second = s.add_candidate("why-lol", "You play LoL to compete",
                                 refines_id=first)
        assert s.get(second)["refines_id"] == first
        assert s.rejected_for_theme("why-lol") == ["You play LoL to relax"]
        s.close()


def test_last_confirmed_at_and_evidence_count_since():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        assert s.last_confirmed_at("lol") is None    # never confirmed yet
        s.add_evidence("lol", "old evidence before confirmation")
        cid = s.add_candidate("lol", "You play to test yourself", confidence=0.9)
        s.confirm(cid)
        confirmed_at = s.last_confirmed_at("lol")
        assert confirmed_at is not None
        # nothing NEW since confirmation yet
        assert s.evidence_count_since("lol", confirmed_at) == 0
        s.add_evidence("lol", "fresh evidence after confirmation")
        assert s.evidence_count_since("lol", confirmed_at) == 1
        s.close()


def test_stats():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        s.confirm(s.add_candidate("t", "a"))
        s.reject(s.add_candidate("t", "b"))
        s.add_candidate("t", "c")
        st = s.stats()
        assert st.get("confirmed") == 1 and st.get("rejected") == 1
        assert st.get("candidate") == 1 and st.get("core_beliefs") == 0
        s.close()


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
