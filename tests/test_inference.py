"""Tests for the inference store (Phase A of the inference engine)."""
import os
import sqlite3
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


def _person_payload(statement="I focus better with a clear handoff.", **changes):
    payload = {
        "theme": "energy", "statement": statement, "scope": "situational",
        "sensitivity": "normal", "confidence": .78,
        "rationale": "Two Investigation answers point in this direction.",
        "evidence": ["answer one", "answer two"],
        "counterevidence": ["Not true on travel days"],
        "change_over_time": "This is newer and narrower than the prior belief.",
    }
    payload.update(changes)
    return payload


def test_person_model_proposal_is_inert_until_separately_approved():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        proposal = s.add_person_proposal(3, 7, "new", _person_payload())
        assert proposal["status"] == "open"
        assert s.confirmed() == []
        applied = s.decide_person_proposal(proposal["id"], "approve")
        assert applied["status"] == "approved"
        belief = s.confirmed()[0]
        assert belief["statement"] == "I focus better with a clear handoff."
        assert belief["scope"] == "situational"
        assert belief["source_kind"] == "curiosity_synthesis"
        assert belief["source_id"] == 7
        assert belief["counterevidence"] == ["Not true on travel days"]
        s.close()


def test_narrowing_preserves_old_belief_as_retired_history():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        old = s.add_candidate("energy", "My energy always crashes after lunch", confidence=.9)
        s.confirm(old)
        proposal = s.add_person_proposal(
            3, 8, "narrow",
            _person_payload("My energy sometimes dips after low-protein lunches."),
            target_inference_id=old)
        applied = s.decide_person_proposal(proposal["id"], "approve")
        assert s.get(old)["status"] == "retired"
        new = s.get(applied["applied_inference_id"])
        assert new["status"] == "confirmed"
        assert new["refines_id"] == old
        assert "sometimes" in new["statement"]
        s.close()


def test_contradiction_does_not_leave_both_claims_current():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        old = s.add_candidate("social energy", "Meeting new people always drains me", confidence=.9)
        s.confirm(old)
        proposal = s.add_person_proposal(
            4, 13, "contradict",
            _person_payload(
                "Meeting new people can energize me when the setting feels voluntary.",
                theme="social energy", confidence=.82),
            target_inference_id=old)
        applied = s.decide_person_proposal(proposal["id"], "approve")
        current_ids = {belief["id"] for belief in s.confirmed()}
        assert old not in current_ids
        assert applied["applied_inference_id"] in current_ids
        assert s.get(old)["status"] == "retired"
        s.close()


def test_support_strengthens_existing_belief_without_duplicate():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        belief_id = s.add_candidate("energy", "Clear handoffs help me focus", confidence=.7)
        s.confirm(belief_id)
        before = s.get(belief_id)["confidence"]
        proposal = s.add_person_proposal(
            3, 9, "support", _person_payload(""), target_inference_id=belief_id)
        applied = s.decide_person_proposal(proposal["id"], "approve")
        assert applied["applied_inference_id"] == belief_id
        assert s.get(belief_id)["confidence"] > before
        assert len(s.confirmed()) == 1
        s.close()


def test_rejected_person_update_is_durable_and_does_not_change_beliefs():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        proposal = s.add_person_proposal(3, 10, "new", _person_payload())
        rejected = s.decide_person_proposal(
            proposal["id"], "reject", note="This overgeneralizes me")
        assert rejected["status"] == "rejected"
        assert rejected["decision_note"] == "This overgeneralizes me"
        assert s.confirmed() == []
        s.close()


def test_identity_scope_requires_stronger_evidence_than_situational_scope():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        weak_identity = _person_payload(
            "I am fundamentally avoidant.", scope="identity", confidence=.89,
            evidence=["one", "two"])
        try:
            s.add_person_proposal(3, 11, "new", weak_identity)
            assert False, "weak identity proposal should be rejected"
        except ValueError as error:
            assert "identity-level" in str(error)
        strong_identity = _person_payload(
            "I consistently value autonomy across domains.", scope="identity",
            confidence=.93, evidence=["work", "relationships", "creative choices"])
        proposal = s.add_person_proposal(3, 12, "new", strong_identity)
        assert proposal["payload"]["scope"] == "identity"
        s.close()


def test_edited_proposal_is_revalidated_and_situational_is_not_core_identity():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        proposal = s.add_person_proposal(3, 14, "new", _person_payload())
        edited = dict(proposal["payload"]); edited["statement"] = ""
        try:
            s.decide_person_proposal(proposal["id"], "approve", payload=edited)
            assert False, "blank edited statement should not apply"
        except ValueError as error:
            assert "requires a proposed statement" in str(error)
        applied = s.decide_person_proposal(proposal["id"], "approve")
        belief = s._dict(s.get(applied["applied_inference_id"]))
        assert belief["scope"] == "situational"
        assert belief["is_core_belief"] is False
        s.close()


def test_legacy_person_model_migrates_without_losing_beliefs():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "memory.db")
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE inference (
          id INTEGER PRIMARY KEY, theme TEXT NOT NULL, statement TEXT NOT NULL,
          confidence REAL, status TEXT DEFAULT 'candidate', evidence TEXT,
          refines_id INTEGER, source_refs TEXT, times_confirmed INTEGER DEFAULT 0,
          times_skipped INTEGER DEFAULT 0, created_at TEXT, validated_at TEXT,
          last_shown_at TEXT);
        INSERT INTO inference VALUES
          (1,'values','I value autonomy',.9,'confirmed','{}',NULL,'[]',1,0,NULL,NULL,NULL);
        """)
        conn.commit(); conn.close()
        s = InferenceStore(db)
        belief = s.confirmed()[0]
        assert belief["statement"] == "I value autonomy"
        assert belief["scope"] == "general"
        proposal = s.add_person_proposal(1, 1, "new", _person_payload())
        assert proposal["status"] == "open"
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
