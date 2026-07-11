"""Tests for redaction, aggregation, JSON parsing, and the stub pipeline."""
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.storage import EventLog  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.triage.redact import redact  # noqa: E402
from livingpc.triage.aggregate import build_day_summary, is_internal_ui  # noqa: E402
from livingpc.triage.llm import (  # noqa: E402
    build_user_prompt,
    get_backend,
    parse_response,
    StubBackend,
)
from livingpc.triage.pipeline import run_triage  # noqa: E402


def test_redact_email_and_card():
    out = redact("contact me@example.com card 4111 1111 1111 1111 ok")
    assert "me@example.com" not in out
    assert "4111" not in out
    assert "[REDACTED]" in out


def test_redact_keeps_normal_text():
    text = "Played League of Legends as Jinx for 40 minutes."
    assert redact(text) == text


def test_redact_keeps_dates_and_runtogether_words():
    assert redact("Activity summary for 2026-06-24") == "Activity summary for 2026-06-24"
    assert redact("on 6/24/2026 at 3:22 PM") == "on 6/24/2026 at 3:22 PM"
    assert redact("Aldesktopscreeninterpreter Progress") == \
        "Aldesktopscreeninterpreter Progress"


def test_redact_still_catches_secrets():
    assert "555" not in redact("call +1 (555) 867-5309 now")
    assert "4111" not in redact("card 4111 1111 1111 1111")
    assert "sk-" not in redact("key sk-abcd1234efgh5678ijkl9012")


def test_parse_response_strips_fences():
    raw = '```json\n{"statements":[{"category":"X","attribute":"a","value":"v"}],' \
          '"supersessions":[],"questions":[]}\n```'
    res = parse_response(raw)
    assert len(res.statements) == 1
    assert res.statements[0].category == "X"


def test_parse_bad_json_is_safe():
    assert parse_response("not json at all").is_empty()


def test_triage_prompt_has_selected_values_and_complete_catalog():
    selected = [{"id": 1, "category": "Games", "attribute": "main", "value": "Caitlyn"}]
    all_memories = selected + [
        {"id": 2, "category": "Study", "attribute": "language", "value": "Korean"}
    ]
    prompt = build_user_prompt("summary", selected, all_memories=all_memories)
    assert "Caitlyn" in prompt
    assert "Korean" not in prompt
    assert "id=1 [Games] main" in prompt
    assert "id=2 [Study] language" in prompt


def test_claude_backend_uses_configured_timeout(monkeypatch):
    captured = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=FakeAnthropic))
    cfg = SimpleNamespace(
        llm_backend="claude",
        llm_model="test-model",
        llm_timeout_seconds=12.5,
        llm_max_retries=0,
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    backend = get_backend(cfg)

    assert backend.model == "test-model"
    assert captured["timeout"] == 12.5
    assert captured["max_retries"] == 0
    assert backend.memory_max_items == 30
    assert backend.memory_max_chars == 1600


def test_aggregate_and_stub_pipeline():
    with tempfile.TemporaryDirectory() as d:
        ev = EventLog(os.path.join(d, "e.db"))
        sid = ev.start_session("LeagueClient.exe", "in-game", ts="2026-06-24T10:00:00")
        ev.end_session(sid, "2026-06-24T10:40:00")
        ev.log_event("ocr", app="LeagueClient.exe", window_title="in-game",
                     text_payload="Jinx\nInfinity Edge\nVictory",
                     session_id=sid, ts="2026-06-24T10:20:00")
        summary = build_day_summary(ev, "2026-06-24")
        assert "LeagueClient.exe" in summary
        assert "Jinx" in summary
        assert "~40 min" in summary
        mem = MemoryStore(os.path.join(d, "m.db"))
        ctx = run_triage(ev, mem, StubBackend(), "2026-06-24")
        assert any("LeagueClient" in s.category for s in ctx.result.statements)
        ev.close(); mem.close()


def test_internal_review_ui_is_excluded_from_triage_summary():
    with tempfile.TemporaryDirectory() as d:
        ev = EventLog(os.path.join(d, "e.db"))
        internal_session = ev.start_session(
            "pythonw.exe", "Faerie Fire", ts="2026-06-24T09:59:00",
        )
        ev.end_session(internal_session, "2026-06-24T10:03:00")
        ev.log_event(
            "ocr", app="pythonw.exe", window_title="Faerie Fire",
            text_payload="answer visible only as coding a...",
            ts="2026-06-24T10:00:00",
        )
        ev.log_event(
            "ocr", app="pythonw.exe", window_title="Faerie Fire Capture Control",
            text_payload="diagnostic UI text",
            ts="2026-06-24T10:01:00",
        )
        ev.log_event(
            "ocr", app="Editor.exe", window_title="Project",
            text_payload="implemented the actual feature",
            ts="2026-06-24T10:02:00",
        )
        summary = build_day_summary(ev, "2026-06-24")
        assert "implemented the actual feature" in summary
        assert "coding a" not in summary
        assert "diagnostic UI" not in summary
        assert "pythonw.exe" not in summary
        ev.close()


def test_internal_ui_detection_is_narrow():
    assert is_internal_ui("pythonw.exe", "Faerie Fire")
    assert is_internal_ui(r"C:\\Python\\python.exe", "Faerie Fire Memory Graph")
    assert not is_internal_ui("pythonw.exe", "My Python Tool")
    assert not is_internal_ui("chrome.exe", "Faerie Fire documentation")


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
