"""Tests for the SQLite event log + session grouping + blob purge."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.storage import EventLog  # noqa: E402
from livingpc.capture.window import WindowTracker  # noqa: E402


def test_events_and_counts():
    with tempfile.TemporaryDirectory() as d:
        store = EventLog(os.path.join(d, "t.db"))
        sid = store.start_session("a.exe", "win", ts="2026-06-24T10:00:00")
        store.log_event("window", app="a.exe", window_title="win", session_id=sid,
                        ts="2026-06-24T10:00:00")
        store.log_event("ocr", app="a.exe", window_title="win",
                        text_payload="hello", session_id=sid, ts="2026-06-24T10:00:05")
        assert store.count() == 2
        assert store.count("ocr") == 1
        assert store.count("window") == 1
        store.close()


def test_session_grouping():
    with tempfile.TemporaryDirectory() as d:
        store = EventLog(os.path.join(d, "t.db"))
        tracker = WindowTracker(store)
        s1 = tracker.update("a.exe", "win1", "2026-06-24T10:00:00")
        s1b = tracker.update("a.exe", "win1", "2026-06-24T10:00:02")  # same -> same session
        s2 = tracker.update("b.exe", "win2", "2026-06-24T10:01:00")   # change -> new session
        assert s1 == s1b
        assert s2 != s1
        # first session should now be closed
        sessions = {row["id"]: row for row in store.recent_sessions()}
        assert sessions[s1]["end_ts"] is not None
        assert sessions[s2]["end_ts"] is None
        store.close()


def test_purge_blobs_keeps_text():
    with tempfile.TemporaryDirectory() as d:
        store = EventLog(os.path.join(d, "t.db"))
        blob = os.path.join(d, "shot.jpg")
        with open(blob, "w") as f:
            f.write("fake image")
        store.log_event("ocr", app="a", text_payload="keep me", blob_ref=blob,
                        ts="2026-06-24T10:00:00")
        purged = store.purge_blobs()
        assert purged == 1
        assert not os.path.exists(blob)                       # file removed
        row = store.conn.execute(
            "SELECT text_payload, blob_ref FROM events"
        ).fetchone()
        assert row["text_payload"] == "keep me"               # text retained
        assert row["blob_ref"] is None                        # ref cleared
        store.close()


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failures += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
