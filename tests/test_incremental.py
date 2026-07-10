"""Tests for the meta watermark, windowed summaries, and blob purge."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.storage import EventLog  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.triage.aggregate import build_summary  # noqa: E402
from livingpc.triage.llm import StubBackend  # noqa: E402
from livingpc.triage.pipeline import apply_result, run_triage  # noqa: E402


def test_meta_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        m = MemoryStore(os.path.join(d, "m.db"))
        assert m.get_meta("k", "default") == "default"
        m.set_meta("k", "v1"); assert m.get_meta("k") == "v1"
        m.set_meta("k", "v2"); assert m.get_meta("k") == "v2"   # upsert
        m.close()


def test_windowed_summary_excludes_outside():
    with tempfile.TemporaryDirectory() as d:
        ev = EventLog(os.path.join(d, "e.db"))
        s1 = ev.start_session("Morning.exe", "am", ts="2026-06-25T09:00:00")
        ev.log_event("ocr", app="Morning.exe", window_title="am",
                     text_payload="morning stuff", session_id=s1, ts="2026-06-25T09:00:00")
        s2 = ev.start_session("Afternoon.exe", "pm", ts="2026-06-25T14:00:00")
        ev.log_event("ocr", app="Afternoon.exe", window_title="pm",
                     text_payload="afternoon stuff", session_id=s2, ts="2026-06-25T14:00:00")
        # window covering only the afternoon
        summ = build_summary(ev, "2026-06-25T13:00:00", "2026-06-25T15:00:00", "Window")
        assert "Afternoon.exe" in summ and "afternoon stuff" in summ
        assert "Morning.exe" not in summ
        ev.close()


def test_purge_blobs_with_cutoff():
    with tempfile.TemporaryDirectory() as d:
        ev = EventLog(os.path.join(d, "e.db"))
        old_blob = os.path.join(d, "old.jpg"); open(old_blob, "w").write("x")
        new_blob = os.path.join(d, "new.jpg"); open(new_blob, "w").write("y")
        ev.log_event("ocr", app="a", text_payload="old", blob_ref=old_blob,
                     ts="2026-06-01T00:00:00")
        ev.log_event("ocr", app="a", text_payload="new", blob_ref=new_blob,
                     ts="2026-06-25T00:00:00")
        purged = ev.purge_blobs(before_ts="2026-06-10T00:00:00")
        assert purged == 1
        assert not os.path.exists(old_blob)    # old removed
        assert os.path.exists(new_blob)         # new kept
        ev.close()


def test_triage_watermark_advances_only_when_results_are_applied():
    with tempfile.TemporaryDirectory() as d:
        ev = EventLog(os.path.join(d, "e.db"))
        s = ev.start_session("App.exe", "w", ts="2026-06-25T09:00:00")
        ev.log_event("ocr", app="App.exe", window_title="w",
                     text_payload="did things", session_id=s, ts="2026-06-25T09:00:00")
        m = MemoryStore(os.path.join(d, "m.db"))
        assert m.get_meta("last_triage_ts") is None
        ctx = run_triage(ev, m, StubBackend(), "2026-06-25", incremental=True)
        assert m.get_meta("last_triage_ts") is None
        apply_result(m, ctx.result, ctx.date, watermark=ctx.window_end)
        assert m.get_meta("last_triage_ts") == ctx.window_end
        # a second run starts where the first ended (no overlap)
        ctx2 = run_triage(ev, m, StubBackend(), "2026-06-25", incremental=True)
        assert ctx2.window_start == ctx.window_end
        ev.close(); m.close()


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
