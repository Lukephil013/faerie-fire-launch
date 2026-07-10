"""Tests for the inference review controller (Phase C, UI-agnostic)."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.config import Config  # noqa: E402
from livingpc.storage import EventLog  # noqa: E402
from livingpc.inference import InferenceStore  # noqa: E402
from livingpc.inference_review import InferenceReview, ACTIONS  # noqa: E402
from livingpc.inference_loop import StubInferenceModel  # noqa: E402

T = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed(rev: InferenceReview, theme="focus"):
    # seed above the surface gate so it shows in the gated stack
    return rev.store.add_candidate(theme, "You chase mastery over novelty",
                                   confidence=0.85)


def test_answer_dispatches_each_action():
    with tempfile.TemporaryDirectory() as d:
        rev = InferenceReview(os.path.join(d, "m.db"))
        yes = _seed(rev); rev.answer("yes", yes)
        assert rev.store.get(yes)["status"] == "confirmed"
        no = _seed(rev); rev.answer("no", no)
        assert rev.store.get(no)["status"] == "rejected"
        kof = _seed(rev); rev.answer("kind_of", kof)
        assert rev.store.get(kof)["status"] == "partial"
        skp = _seed(rev); rev.answer("skip", skp)
        assert rev.store.get(skp)["times_skipped"] == 1
        rev.close()


def test_refine_returns_new_id_and_stores_user_wording():
    with tempfile.TemporaryDirectory() as d:
        rev = InferenceReview(os.path.join(d, "m.db"))
        i = _seed(rev)
        new_id = rev.answer("refine", i, "I chase mastery, but novelty hooks me first")
        assert isinstance(new_id, int) and new_id != i
        assert rev.store.get(i)["status"] == "retired"
        new = rev.store.get(new_id)
        assert new["status"] == "confirmed" and "novelty hooks me" in new["statement"]
        rev.close()


def test_invalid_and_empty_actions_raise():
    with tempfile.TemporaryDirectory() as d:
        rev = InferenceReview(os.path.join(d, "m.db"))
        i = _seed(rev)
        try:
            rev.answer("maybe", i); assert False, "unknown action should raise"
        except ValueError:
            pass
        try:
            rev.answer("refine", i, "   "); assert False, "empty refine should raise"
        except ValueError:
            pass
        assert set(ACTIONS) == {"yes", "no", "kind_of", "skip", "refine"}
        rev.close()


def test_stack_and_beliefs_reflect_state():
    with tempfile.TemporaryDirectory() as d:
        rev = InferenceReview(os.path.join(d, "m.db"))
        a = _seed(rev, "focus")
        _seed(rev, "identity")
        assert len(rev.stack()) == 2
        rev.answer("yes", a)
        assert len(rev.stack()) == 1                     # confirmed leaves the stack
        assert any("mastery" in b["statement"] for b in rev.confirmed())
        assert rev.stats().get("confirmed") == 1
        rev.close()


def test_skip_parks_card_for_today():
    with tempfile.TemporaryDirectory() as d:
        rev = InferenceReview(os.path.join(d, "m.db"))
        i = _seed(rev, "focus")
        assert [x["id"] for x in rev.stack()] == [i]
        rev.answer("skip", i)
        assert rev.stack() == []
        rev.close()


def test_run_now_files_evidence_below_gate():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"),
                     inference_backend="stub")
        # run_now uses the real clock for its window, so seed within the last hour
        now = datetime.now(timezone.utc)
        ev = EventLog(cfg.db_path)
        s = ev.start_session("Blender.exe", "sculpt",
                             (now - timedelta(minutes=40)).isoformat())
        ev.end_session(s, (now - timedelta(minutes=15)).isoformat())
        ev.close()
        rev = InferenceReview(cfg.memory_db_path)
        summary = rev.run_now(cfg, model=StubInferenceModel())
        # one pass files evidence but should NOT surface a yes/no card yet
        assert summary["evidence_added"] >= 1
        assert rev.stack(gate=0.80) == []
        forming = rev.forming(gate=0.80)
        assert any(f["theme"] == "Blender.exe" for f in forming)
        rev.close()


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
