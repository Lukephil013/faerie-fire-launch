"""Tests for the pure decision core and the perceptual-hash helpers."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livingpc.sampler import decide, hamming, ahash  # noqa: E402


def _base(**over):
    kw = dict(
        now=100.0,
        window="app|win",
        last_window="app|win",
        idle_seconds=0.0,
        idle_limit=60.0,
        frame_distance=0,
        threshold=8,
        seconds_since_capture=0.0,
        max_interval=45.0,
    )
    kw.update(over)
    return kw


def test_afk_skips():
    d = decide(**_base(idle_seconds=120.0))
    assert d.capture is False and d.reason == "afk"


def test_afk_beats_window_change():
    # AFK is rule 1, evaluated before window change
    d = decide(**_base(idle_seconds=120.0, window="x", last_window="y"))
    assert d.capture is False and d.reason == "afk"


def test_window_change_captures():
    d = decide(**_base(window="new|w", last_window="old|w"))
    assert d.capture is True and d.reason == "window_change"


def test_screen_changed_captures():
    d = decide(**_base(frame_distance=20, threshold=8))
    assert d.capture is True and d.reason == "screen_changed"


def test_below_threshold_no_capture():
    d = decide(**_base(frame_distance=3, threshold=8, seconds_since_capture=1.0))
    assert d.capture is False and d.reason == "no_change"


def test_heartbeat_captures():
    d = decide(**_base(frame_distance=0, seconds_since_capture=60.0, max_interval=45.0))
    assert d.capture is True and d.reason == "heartbeat"


def test_hamming():
    assert hamming(0b0000, 0b0000) == 0
    assert hamming(0b1010, 0b0000) == 2
    assert hamming(0b1111, 0b0000) == 4


def test_ahash_identical_and_different():
    from PIL import Image

    black = Image.new("RGB", (64, 64), (0, 0, 0))
    white = Image.new("RGB", (64, 64), (255, 255, 255))
    half = Image.new("RGB", (64, 64), (0, 0, 0))
    # paint right half white
    for x in range(32, 64):
        for y in range(64):
            half.putpixel((x, y), (255, 255, 255))

    assert hamming(ahash(black), ahash(black)) == 0          # identical -> 0
    assert hamming(ahash(black), ahash(half)) > 0            # different -> nonzero


if __name__ == "__main__":
    # tiny runner so `python tests/test_sampler.py` works without pytest
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
