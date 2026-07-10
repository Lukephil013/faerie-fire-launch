"""Tests for browser-history + clipboard collectors (platform-independent parts)."""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.storage import EventLog  # noqa: E402
from livingpc.capture import extras  # noqa: E402


def test_chromium_epoch_conversion():
    # 13350998400000000 us since 1601 == 2023-... ; just check round-trippable shape
    iso = extras._chromium_to_iso(13297919999000000)
    assert iso.startswith("20")           # a plausible year
    # 0 -> 1601 epoch start minus offset -> 1970-ish? just ensure it doesn't crash
    assert isinstance(extras._firefox_to_iso(1_700_000_000_000_000), str)


def test_clipboard_collector_logs_and_skips_blocklist(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        store = EventLog(os.path.join(d, "e.db"))
        cc = extras.ClipboardCollector()

        # stub the OS clipboard read
        seq = {"v": "hello world"}
        extras_read = extras.read_clipboard_text
        extras.read_clipboard_text = lambda: seq["v"]
        try:
            # normal app -> logged
            assert cc.poll(store, "chrome.exe", ["1Password.exe"]) is True
            assert store.count("clipboard") == 1
            # same text again -> no duplicate
            assert cc.poll(store, "chrome.exe", ["1Password.exe"]) is False
            assert store.count("clipboard") == 1
            # new text but blocklisted foreground -> skipped (and remembered)
            seq["v"] = "super secret password"
            assert cc.poll(store, "1Password.exe", ["1Password.exe"]) is False
            assert store.count("clipboard") == 1
            # switching away now should NOT re-log it (already remembered)
            assert cc.poll(store, "chrome.exe", ["1Password.exe"]) is False
            assert store.count("clipboard") == 1
        finally:
            extras.read_clipboard_text = extras_read
        store.close()


def test_browser_reader_with_synthetic_db():
    with tempfile.TemporaryDirectory() as d:
        # build a fake Chromium History db
        hist = os.path.join(d, "History")
        c = sqlite3.connect(hist)
        c.execute("CREATE TABLE urls (url TEXT, title TEXT, last_visit_time INTEGER)")
        c.execute("INSERT INTO urls VALUES (?,?,?)",
                  ("https://op.gg/champ/jinx", "Jinx build - OP.GG", 13350000000000000))
        c.execute("INSERT INTO urls VALUES (?,?,?)",
                  ("https://ttmik.com/lesson3", "TTMIK Lesson 3", 13350000000005000))
        c.commit(); c.close()

        store = EventLog(os.path.join(d, "e.db"))
        # point the chromium-path finder at our fake db
        import livingpc.capture.extras as ex
        orig = ex._chromium_history_paths
        ex._chromium_history_paths = lambda: [hist]
        ex._firefox_history_paths = lambda: []
        try:
            bh = ex.BrowserHistoryCollector()
            n = bh.poll(store)
            assert n == 2
            assert store.count("browser") == 2
            # second poll: watermark prevents re-logging
            assert bh.poll(store) == 0
            assert store.count("browser") == 2
        finally:
            ex._chromium_history_paths = orig
        store.close()


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
