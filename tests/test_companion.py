"""Tests for the companion's persona system and context-aware prompt building."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.config import Config  # noqa: E402
from livingpc.storage import EventLog  # noqa: E402
from livingpc.memory import MemoryStore  # noqa: E402
from livingpc.companion import personas  # noqa: E402
from livingpc.companion.brain import Companion, StubChat  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


def test_personas_exist():
    keys = [p["key"] for p in personas.list_personas()]
    assert "companion" in keys and "coach" in keys and "gremlin" in keys
    assert personas.get_persona("gremlin").name == "Gremlin"
    assert personas.get_persona("nope").key == "companion"   # fallback


def test_companion_prompt_uses_memory_and_screen():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        # seed memory + a screen event
        mem = MemoryStore(cfg.memory_db_path)
        mem.add("League of Legends", "champion pool", "Caitlyn, Tristana")
        mem.close()
        ev = EventLog(cfg.db_path)
        ev.log_event("ocr", app="LeagueClient.exe", window_title="in-game",
                     text_payload="Baron is up in 30 seconds")
        ev.close()

        c = Companion(cfg=cfg, persona_key="coach", chat=StubChat())
        sysp = c.system_prompt()
        assert "Caitlyn, Tristana" in sysp           # knows them
        assert "Baron is up" in sysp                  # sees the screen
        assert "coach" in sysp.lower()                # persona flavor present

        out = c.reply("what should I do?")
        assert out.startswith("(stub)")
        assert len(c.history) == 2                     # user + assistant recorded
        c.close()


def test_companion_prompt_has_read_only_lifecycle_context():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        prompt = c.system_prompt()
        assert "read-only architecture reference" in prompt
        assert "Cultivation Lifecycle" in prompt
        c.close()


def test_companion_persists_and_switches_multiple_chats():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        first = c.chat_id
        c.reply("first conversation")
        second = c.new_chat()
        c.reply("second conversation")
        assert second != first
        assert len(c.list_chats()) == 2
        assert c.switch_chat(first) is True
        assert c.history[0]["content"] == "first conversation"
        c.close()

        reopened = Companion(cfg=cfg, chat=StubChat(), chat_id=second)
        assert reopened.history[0]["content"] == "second conversation"
        reopened.close()


def test_companion_deletes_conversations_and_falls_back_from_active_chat():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        first = c.chat_id
        c.reply("first conversation")
        second = c.new_chat()
        c.reply("second conversation")

        assert c.delete_chat(second) is True
        assert c.chat_id != second
        assert all(chat["id"] != second for chat in c.list_chats())
        assert c.switch_chat(second) is False

        assert c.delete_chat(first) is True
        assert c.chat_id
        assert c.list_chats()
        c.close()


def test_companion_prompt_includes_confirmed_inferences():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        from livingpc.inference import InferenceStore
        inf = InferenceStore(cfg.memory_db_path)
        cid = inf.add_candidate("focus", "You lock in late at night.")
        inf.confirm(cid)
        inf.close()

        c = Companion(cfg=cfg, chat=StubChat())
        sysp = c.system_prompt()
        assert "You lock in late at night." in sysp
        assert "PATTERNS YOU'VE CONFIRMED" in sysp
        c.close()


def test_companion_prompt_shows_nothing_confirmed_yet_when_empty():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, chat=StubChat())
        assert "(nothing confirmed yet)" in c.system_prompt()
        c.close()


def test_companion_prompt_includes_active_curiosity_and_open_question():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(cfg.memory_db_path)
        cur_id = store.add_curiosity("help me get fit", "fitness")
        store.add_item(cur_id, "question", "How many days a week can you realistically train?")
        store.close()

        c = Companion(cfg=cfg, chat=StubChat())
        sysp = c.system_prompt()
        assert "fitness" in sysp
        assert "help me get fit" in sysp
        assert "How many days a week can you realistically train?" in sysp
        assert "GOALS / CURIOSITIES" in sysp
        c.close()


def test_companion_prompt_excludes_archived_curiosities():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        from livingpc.curiosity import CuriosityStore
        store = CuriosityStore(cfg.memory_db_path)
        archived_id = store.add_curiosity("learn piano", "piano")
        store.set_status(archived_id, "archived")
        store.close()

        c = Companion(cfg=cfg, chat=StubChat())
        sysp = c.system_prompt()
        assert "learn piano" not in sysp
        assert "(no active goals/curiosities yet)" in sysp
        c.close()


def test_persona_switch():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"))
        c = Companion(cfg=cfg, persona_key="companion", chat=StubChat())
        assert c.persona.key == "companion"
        c.set_persona("gremlin")
        assert c.persona.key == "gremlin"
        assert "roast" in c.system_prompt().lower() or "gremlin" in c.system_prompt().lower()
        c.close()


def test_companion_selects_relevant_memory_with_limit():
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(
            db_path=os.path.join(d, "e.db"),
            memory_db_path=os.path.join(d, "m.db"),
            companion_memory_max_items=1,
            companion_memory_max_chars=500,
        )
        mem = MemoryStore(cfg.memory_db_path)
        mem.add("Cooking", "favorite meal", "ramen")
        mem.add("League of Legends", "champion pool", "Caitlyn")
        mem.close()
        c = Companion(cfg=cfg, chat=StubChat())
        prompt = c.system_prompt("How should I play Caitlyn?")
        assert "Caitlyn" in prompt
        assert "ramen" not in prompt
        c.close()


def test_companion_uses_local_avatar_asset():
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    asset = ROOT / "livingpc/companion/assets/companion_avatar.jpg"
    assert asset.read_bytes().startswith(b"\xff\xd8\xff")   # JPEG signature
    assert '{{AVATAR_DATA_URL}}' in html
    assert 'id="avatarArt"' in html
    assert '<canvas id="cv"' not in html


def test_companion_uses_shared_leafy_background():
    # Same background photo + drifting motes as the Memory GUI, embedded as a
    # data URL (like the raccoon avatar) since this window loads html=... as
    # a raw string rather than a url=..., so relative asset paths won't resolve.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    asset = ROOT / "livingpc/ui/assets/backgrounds/forest-ruins-main.jpg"
    assert asset.read_bytes().startswith(b"\xff\xd8\xff")   # JPEG signature
    assert '{{BACKGROUND_DATA_URL}}' in html
    assert 'id="motes"' in html
    assert 'mote-rise' in html


def test_companion_ui_has_no_blue_accent_left():
    # The panel used to be a plain dark-blue box; asked to switch fully to the
    # green/leafy palette that matches the background photo.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    for removed in ("--cyan", "rgba(70,236,255", "rgba(110,140,240", "#46ecff"):
        assert removed not in html, removed
    assert "--green" in html


def test_companion_api_has_no_voice_methods():
    # The Python-side bridge dropped listen/set_listening/set_voice/poll/
    # hotkey_talk along with the UI change — plain send() is all that's left
    # for getting a reply out of the brain.
    import companion
    api = companion.Api()
    for removed in ("listen", "set_listening", "set_voice", "poll",
                    "hotkey_talk", "_ensure_ears", "_on_wake", "_hotkey_work"):
        assert not hasattr(api, removed), removed
    assert hasattr(api, "send")
    assert hasattr(api, "get_reflection")
    assert hasattr(api, "refine_reflection")


def test_companion_ui_is_plain_text_chat_no_voice():
    # Companion is now a normal text chat window — no mic/listen/mute/wake
    # controls, no audio playback wiring. See companion.html + companion.py.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    for removed in ("pywebview.api.listen", "pywebview.api.set_listening",
                    "pywebview.api.set_voice", "pywebview.api.poll",
                    "id=\"wake\"", "id=\"talk\"", "id=\"mute\"", "AudioContext"):
        assert removed not in html, removed
    assert 'id="textIn"' in html
    assert 'id="textSend"' in html
    assert 'pywebview.api.send' in html
    assert '<textarea id="textIn"' in html
    assert 'id="sidebar"' in html
    assert 'id="newChat"' in html
    assert 'e.key===\'Enter\'&&!e.shiftKey' in html
    assert 'user-select:text' in html


def test_companion_ui_has_no_persona_picker():
    # Companion/Coach/Gremlin buttons removed — always the default persona,
    # no switcher UI. The backend persona system itself (personas.py,
    # Companion.set_persona) is untouched; only this chat window's picker is gone.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    for removed in ('id="personas"', "buildPersonas", "refreshPersonas",
                    "pywebview.api.set_persona", "pywebview.api.list_personas"):
        assert removed not in html, removed


def test_companion_api_toggle_maximize_without_window_is_a_safe_noop():
    import companion
    api = companion.Api()
    assert api.window is None
    assert api.toggle_maximize() is False


def test_companion_api_toggle_maximize_calls_window_toggle_fullscreen():
    import companion

    class _FakeWindow:
        def __init__(self):
            self.calls = 0

        def toggle_fullscreen(self):
            self.calls += 1

    api = companion.Api()
    api.window = _FakeWindow()
    assert api.toggle_maximize() is True
    assert api.window.calls == 1


def test_companion_api_minimize_calls_window_minimize():
    import companion

    class _FakeWindow:
        def __init__(self):
            self.calls = 0

        def minimize(self):
            self.calls += 1

    api = companion.Api()
    assert api.minimize() is False
    api.window = _FakeWindow()
    assert api.minimize() is True
    assert api.window.calls == 1


def test_companion_ui_has_maximize_button():
    # Frameless window, no native title bar — a custom button in the header
    # drives window.toggle_fullscreen() via the Api bridge. See companion.py.
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    assert 'id="maximize"' in html
    assert 'pywebview.api.toggle_maximize' in html
    assert 'id="minimize"' in html
    assert 'pywebview.api.minimize' in html


def test_companion_retries_history_restore_if_ready_event_was_missed():
    html = (ROOT / "livingpc/companion/companion.html").read_text(encoding="utf-8")
    assert "chatStateLoaded" in html
    assert "setTimeout(loadChatState" in html
    assert "pywebview.api.chat_state" in html


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
