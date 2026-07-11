"""Loop tests: dwell, hybrid confidence, evidence accumulation, and graduation."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.config import Config  # noqa: E402
from livingpc.storage import EventLog  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.inference import InferenceStore  # noqa: E402
from livingpc.inference_loop import (  # noqa: E402
    derive_dwell, hybrid_confidence, synthesize_theme, run_inference,
    parse_evidence, parse_claim, StubInferenceModel, InferenceContext,
    _due_for_resynthesis, build_context, build_synthesize_prompt,
)

T = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
GATE = 0.80
MINEV = 3


def _iso(mins_before):
    return (T - timedelta(minutes=mins_before)).isoformat()


def _seed_sessions(db_path):
    ev = EventLog(db_path)
    s1 = ev.start_session("LeagueClient.exe", "Ranked", _iso(50)); ev.end_session(s1, _iso(38))
    s2 = ev.start_session("Code.exe", "editor", _iso(20)); ev.end_session(s2, _iso(17))
    s3 = ev.start_session("pythonw.exe", "Faerie Fire", _iso(15)); ev.end_session(s3, _iso(5))
    ev.close()


def test_derive_dwell_excludes_internal():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "e.db"); _seed_sessions(db)
        ev = EventLog(db)
        apps = [x["app"] for x in derive_dwell(ev, _iso(120), T.isoformat())]
        ev.close()
        assert "pythonw.exe" not in apps
        assert apps[0] == "LeagueClient.exe"


def test_hybrid_confidence_needs_enough_evidence_to_graduate():
    # strong model opinion but too little evidence -> capped below the gate
    assert hybrid_confidence(0.95, 1, gate=GATE, min_evidence=MINEV) == round(GATE - 0.01, 4)
    assert hybrid_confidence(0.95, 2, gate=GATE, min_evidence=MINEV) < GATE
    # decent model + enough independent evidence -> crosses the gate
    assert hybrid_confidence(0.72, 5, gate=GATE, min_evidence=MINEV) >= GATE
    # more evidence never lowers confidence (monotonic boost)
    assert (hybrid_confidence(0.7, 6, gate=GATE, min_evidence=MINEV)
            >= hybrid_confidence(0.7, 3, gate=GATE, min_evidence=MINEV))


def test_evidence_is_idempotent_per_inference_run_item():
    with tempfile.TemporaryDirectory() as d:
        inf = InferenceStore(os.path.join(d, "m.db"))
        try:
            first = inf.add_evidence("focus", "worked deeply", run_id="window-1", item_index=0)
            duplicate = inf.add_evidence(
                "focus", "worked deeply", run_id="window-1", item_index=0)
            inf.add_evidence("focus", "worked deeply again", run_id="window-2", item_index=0)
            assert first is not None and duplicate is None
            assert len(inf.evidence_for_theme("focus")) == 2
            assert inf.evidence_episode_count("focus") == 2
        finally:
            inf.close()


def test_parse_evidence_and_claim():
    ev = parse_evidence('```json\n{"evidence":[{"theme":"focus","observation":"42 min deep work"}]}\n```')
    assert len(ev) == 1 and ev[0].theme == "focus"
    claim = parse_claim("focus", '{"statement":"You crave flow","confidence":1.4}')
    assert claim.statement == "You crave flow" and claim.confidence == 1.0
    assert parse_claim("x", "not json") is None


def test_parse_claim_is_redundant_gate():
    # model self-reports the claim just restates an existing thesis -> no claim,
    # regardless of what statement text came with it
    redundant = parse_claim(
        "learning",
        '{"statement":"You watch yourself learn to feel in control",'
        '"confidence":0.9,"is_redundant":true}')
    assert redundant is None
    # absent field stays backward compatible with older prompts/tests
    fine = parse_claim("learning", '{"statement":"You crave flow","confidence":0.9}')
    assert fine is not None
    # explicit false behaves the same as absent
    also_fine = parse_claim(
        "learning", '{"statement":"You crave flow","confidence":0.9,"is_redundant":false}')
    assert also_fine is not None


def test_build_context_collects_open_candidates_across_themes():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "m.db")
        mem = MemoryStore(db)
        inf = InferenceStore(db)
        inf.add_candidate("system_design", "You watch yourself design.", confidence=0.9)
        inf.add_candidate("learning", "You watch yourself learn.", confidence=0.85)
        ctx = build_context(mem, inf)
        assert ctx.open_candidates_by_theme["system_design"] == "You watch yourself design."
        assert ctx.open_candidates_by_theme["learning"] == "You watch yourself learn."
        mem.close()
        inf.close()


def test_synthesize_prompt_shows_other_themes_pending_claims_not_current_theme():
    ctx = InferenceContext(open_candidates_by_theme={
        "system_design": "You watch yourself design to feel in control.",
        "learning": "You watch yourself learn to feel in control.",
    })
    prompt = build_synthesize_prompt("learning", ["some evidence"], ctx)
    assert "[system_design] You watch yourself design to feel in control." in prompt
    # the current theme's own pending statement isn't echoed back as "other"
    assert "You watch yourself learn to feel in control." not in prompt


def test_synthesize_prompt_shows_other_themes_rejections_not_current_theme():
    # a thesis rejected under "projects" should surface as an off-limits idea
    # when synthesizing "korean_study", even though it was never rejected there
    ctx = InferenceContext(rejections_by_theme={
        "projects": ["You build things to feel recursive self-visibility into your own process."],
        "korean_study": ["some unrelated rejection for this theme"],
    })
    prompt = build_synthesize_prompt("korean_study", ["some evidence"], ctx)
    assert "[projects] You build things to feel recursive self-visibility into your own process." in prompt
    # the current theme's OWN rejection appears only in the per-theme section,
    # not duplicated into the "other themes" section
    assert prompt.count("some unrelated rejection for this theme") == 1


def test_single_run_files_evidence_but_does_not_surface():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"),
                     inference_backend="stub")
        _seed_sessions(cfg.db_path)
        res = run_inference(cfg, now=T)
        assert res.evidence_added >= 1
        assert res.graduated == []                       # nothing crosses the gate on pass 1
        inf = InferenceStore(cfg.memory_db_path)
        assert inf.to_review() == []                     # gated stack is empty
        forming = inf.forming()
        assert any(f["theme"] == "LeagueClient.exe" for f in forming)
        assert inf.stats().get("evidence", 0) >= 1
        inf.close()


def test_accumulated_evidence_graduates_into_the_stack():
    with tempfile.TemporaryDirectory() as d:
        inf = InferenceStore(os.path.join(d, "m.db"))
        for i in range(6):                               # pile up independent evidence
            inf.add_evidence("focus", f"deep-work block {i}")
        res = synthesize_theme(inf, StubInferenceModel(), "focus",
                               InferenceContext(), gate=GATE, min_evidence=MINEV)
        assert res["graduated"] is True
        stack = inf.to_review()
        assert len(stack) == 1 and stack[0]["theme"] == "focus"
        assert stack[0]["confidence"] >= GATE
        assert inf.forming() == []                       # no longer "forming"
        inf.close()


def test_no_keeps_evidence_and_reforms_a_different_claim():
    with tempfile.TemporaryDirectory() as d:
        inf = InferenceStore(os.path.join(d, "m.db"))
        for i in range(6):
            inf.add_evidence("focus", f"block {i}")
        first = synthesize_theme(inf, StubInferenceModel(), "focus",
                                 InferenceContext(), gate=GATE, min_evidence=MINEV)
        original = inf.get(first["id"])["statement"]
        inf.reject(first["id"])                           # user says No

        ctx = InferenceContext(rejections_by_theme={"focus": inf.rejected_for_theme("focus")})
        second = synthesize_theme(inf, StubInferenceModel(), "focus", ctx,
                                  gate=GATE, min_evidence=MINEV)
        assert second["id"] != first["id"]
        assert inf.get(second["id"])["statement"] != original   # genuinely different
        # the evidence behind it was kept, not discarded
        assert len(inf.evidence_for_theme("focus")) == 6
        inf.close()


def test_due_for_resynthesis_true_when_never_confirmed():
    with tempfile.TemporaryDirectory() as d:
        inf = InferenceStore(os.path.join(d, "m.db"))
        assert _due_for_resynthesis(inf, "focus", MINEV) is True
        inf.close()


def test_due_for_resynthesis_gated_until_enough_new_evidence():
    with tempfile.TemporaryDirectory() as d:
        inf = InferenceStore(os.path.join(d, "m.db"))
        for i in range(6):
            inf.add_evidence("focus", f"old evidence {i}")
        cid = inf.add_candidate("focus", "You crave structural self-visibility",
                                confidence=0.9)
        inf.confirm(cid)
        # right after confirming, nothing new has come in yet
        assert _due_for_resynthesis(inf, "focus", MINEV) is False
        for i in range(MINEV - 1):
            inf.add_evidence("focus", f"new evidence {i}")
        assert _due_for_resynthesis(inf, "focus", MINEV) is False   # still short
        inf.add_evidence("focus", "one more")
        assert _due_for_resynthesis(inf, "focus", MINEV) is True    # now earned it
        inf.close()


def test_confirmed_theme_is_not_reasked_without_fresh_evidence():
    """The bug this guards against: a daily-use theme (e.g. League of Legends)
    keeps generating a little evidence every run forever. Without the gate,
    that alone would rebuild and re-surface a near-identical claim on every
    single pass even though the user already said Yes."""
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"),
                     inference_backend="stub")
        _seed_sessions(cfg.db_path)   # yields ~1 dwell-derived evidence item per theme
        inf = InferenceStore(cfg.memory_db_path)
        for i in range(6):
            inf.add_evidence("LeagueClient.exe", f"old evidence {i}")
        cid = inf.add_candidate("LeagueClient.exe", "You use LoL to test yourself",
                                confidence=0.9)
        inf.confirm(cid)
        inf.close()

        run_inference(cfg, now=T)

        inf = InferenceStore(cfg.memory_db_path)
        rows = inf.conn.execute(
            "SELECT COUNT(*) c FROM inference WHERE theme=?",
            ("LeagueClient.exe",)).fetchone()["c"]
        assert rows == 1                              # no fresh candidate created
        # (Code.exe is untouched by the gate — never confirmed — and may still
        # synthesize from the seeded session; only LeagueClient.exe is asserted.)
        league = [c for c in inf.to_review(min_confidence=0.0)
                 if c["theme"] == "LeagueClient.exe"]
        assert league == []                            # nothing new to ask about
        inf.close()


def test_confirmed_theme_is_reasked_once_enough_new_evidence_piles_up():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"),
                     inference_backend="stub")
        _seed_sessions(cfg.db_path)
        inf = InferenceStore(cfg.memory_db_path)
        for i in range(6):
            inf.add_evidence("LeagueClient.exe", f"old evidence {i}")
        cid = inf.add_candidate("LeagueClient.exe", "You use LoL to test yourself",
                                confidence=0.9)
        inf.confirm(cid)
        for i in range(MINEV):                        # enough NEW evidence since Yes
            inf.add_evidence("LeagueClient.exe", f"new evidence {i}")
        inf.close()

        run_inference(cfg, now=T)

        inf = InferenceStore(cfg.memory_db_path)
        rows = inf.conn.execute(
            "SELECT COUNT(*) c FROM inference WHERE theme=?",
            ("LeagueClient.exe",)).fetchone()["c"]
        assert rows == 2                              # confirmed + a fresh candidate
        league = [c for c in inf.to_review(min_confidence=0.0)
                 if c["theme"] == "LeagueClient.exe"]
        assert len(league) == 1
        inf.close()


def test_parked_theme_gets_no_evidence():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"),
                     inference_backend="stub")
        _seed_sessions(cfg.db_path)
        inf = InferenceStore(cfg.memory_db_path)
        for _ in range(4):
            inf.reject(inf.add_candidate("LeagueClient.exe", "throwaway"))
        assert "LeagueClient.exe" in inf.parked_themes()
        inf.close()
        run_inference(cfg, now=T)
        inf = InferenceStore(cfg.memory_db_path)
        assert "LeagueClient.exe" not in inf.evidence_count_by_theme()   # parked -> ignored
        assert "Code.exe" in inf.evidence_count_by_theme()
        inf.close()


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn(); print("PASS " + fn.__name__)
        except Exception:
            fails += 1; print("FAIL " + fn.__name__); traceback.print_exc()
    print("\n%d/%d passed" % (len(fns) - fails, len(fns)))
    sys.exit(1 if fails else 0)
