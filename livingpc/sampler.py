"""The four-rule screenshot sampler.

`decide()` is a pure function -- no I/O, no PIL -- so it's trivially testable.
`ahash()` / `hamming()` are the cheap perceptual-hash helpers it relies on.
SamplerState holds the small amount of state the service threads between ticks.

Rules (priority order), matching the design doc:
  1. AFK            -> skip
  2. window change  -> capture (event-driven; never missed)
  3. screen changed -> capture (perceptual-hash distance > threshold)
  4. heartbeat      -> capture (max_interval elapsed while active)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Decision:
    capture: bool
    reason: str


@dataclass
class SamplerState:
    last_window: str | None = None
    last_hash: int | None = None
    last_capture_ts: float = 0.0


def decide(
    *,
    now: float,
    window: str,
    last_window: str | None,
    idle_seconds: float,
    idle_limit: float,
    frame_distance: int | None,
    threshold: int,
    seconds_since_capture: float,
    max_interval: float,
) -> Decision:
    """Pure decision core. `frame_distance` is the hamming distance between the
    current frame hash and the last captured one (None if not computed, e.g.
    on a window change where we capture regardless)."""
    if idle_seconds > idle_limit:
        return Decision(False, "afk")
    if window != last_window:
        return Decision(True, "window_change")
    if frame_distance is not None and frame_distance > threshold:
        return Decision(True, "screen_changed")
    if seconds_since_capture >= max_interval:
        return Decision(True, "heartbeat")
    return Decision(False, "no_change")


# --- perceptual hash ------------------------------------------------------
_AHASH_SIZE = 8  # 8x8 -> 64-bit hash


def ahash(image, size: int = _AHASH_SIZE) -> int:
    """Average hash: downscale to size x size grayscale, bit per pixel vs mean.

    Accepts a PIL.Image. Returns a 64-bit int (for the default size).
    """
    small = image.convert("L").resize((size, size))
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    bits = 0
    for i, p in enumerate(pixels):
        if p >= avg:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two hashes (0-64 for 8x8)."""
    return bin(a ^ b).count("1")
