"""Tests for the ears orchestration (wake-phrase + listen loop) with fakes."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.companion.ears import strip_wake, Ears  # noqa: E402


def test_strip_wake():
    assert strip_wake("Hey Faerie, what's up?", "hey faerie") == ("what's up?", True)
    assert strip_wake("hey faerie build me a deck", "hey faerie") == ("build me a deck", True)
    assert strip_wake("just talking normally", "hey faerie") == ("just talking normally", False)
    # punctuation/casing tolerant
    assert strip_wake("HEY FAERIE!  coach mode", "hey faerie")[1] is True


class FakeTranscriber:
    def __init__(self, text): self.text = text
    def transcribe(self, audio, sr=16000): return self.text


def test_listen_once():
    ears = Ears(transcriber=FakeTranscriber("hello there"),
                recorder=lambda **k: [0.0])           # non-None audio
    assert ears.listen_once() == "hello there"
    # nothing said -> empty
    ears2 = Ears(transcriber=FakeTranscriber("x"), recorder=lambda **k: None)
    assert ears2.listen_once() == ""


def test_wake_loop_fires_callback():
    got = []
    ears = Ears(transcriber=FakeTranscriber("hey faerie what time is it"),
                recorder=lambda **k: [0.0],
                on_wake=lambda msg: got.append(msg),
                wake_phrase="hey faerie")
    ears.start_wake_loop()
    # give the loop a moment to run and fire
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    ears.stop_wake_loop()
    assert got and got[0] == "what time is it"


def test_wake_loop_ignores_non_wake():
    got = []
    ears = Ears(transcriber=FakeTranscriber("just me talking to myself"),
                recorder=lambda **k: [0.0],
                on_wake=lambda msg: got.append(msg),
                wake_phrase="hey faerie")
    ears.start_wake_loop()
    time.sleep(0.15)
    ears.stop_wake_loop()
    assert got == []          # no wake phrase -> never fires


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
