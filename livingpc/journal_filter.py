"""Relevance pre-filter for the journal import — free signal, fewer tokens.

Years of raw journals contain a lot the model doesn't need: pasted AI-advice
blocks, near-verbatim repeats of the same passage, one-line fragments, URLs.
Sending it all doesn't just cost tokens — it dilutes each batch, so the model
extracts weaker facts. This stage runs locally (zero API cost) before batching:

  1. drops entries that are too short to carry a durable fact;
  2. drops near-duplicate entries (token Jaccard) — journals repeat themselves,
     both within a file (pinned notes) and across files;
  3. down-ranks pasted advice (text talking AT the user: "you should", "your
     nervous system...") vs. the user's own first-person writing, keeping an
     entry only if enough of it is theirs or it's insight-dense;
  4. trims extremely long entries (head + tail — journals put the raw feeling
     first and the conclusion last; the middle is usually elaboration).

Everything is tunable (config: journal_filter_*) and `--no-filter` bypasses it.
The filter never touches stored files or memory — it only shapes what one
import run sends to the model.
"""
from __future__ import annotations

import re

from .memory import _jaccard, _tokens

# words that mark self-authored insight (identity, feeling, motive, change)
_INSIGHT = (
    "i feel", "i felt", "i am ", "i'm ", "i was", "i want", "i need",
    "i realize", "i realized", "i learned", "i think", "i believe", "i hate",
    "i love", "i keep", "my ", "angry", "anger", "fear", "afraid", "grief",
    "shame", "anxious", "anxiety", "goal", "dream", "trapped", "free",
    "because", "why ", "pattern", "trauma", "healing", "energy", "body",
)
# second-person coaching = probably pasted advice, not the user's own words
_ADVICE = ("you should", "your nervous system", "you are allowed",
           "you were", "you might", "you tend", "your body", "you do not",
           "you can start", "this is not", "that is the anger")

_URL_ONLY = re.compile(r"^\s*(?:-\s*)?https?://\S+\s*$")


def _clean(text: str) -> str:
    lines = [l for l in text.splitlines() if not _URL_ONLY.match(l)]
    return "\n".join(lines).strip()


def score_entry(text: str) -> float:
    """Heuristic insight density: distinct first-person/insight markers,
    penalized by pasted-advice markers. Higher = more worth sending."""
    lower = " " + " ".join(text.lower().split()) + " "
    insight = sum(1 for m in _INSIGHT if m in lower)
    advice = sum(1 for m in _ADVICE if m in lower)
    return float(insight - 1.5 * advice)


def trim_entry(text: str, max_chars: int) -> str:
    """Keep the head (raw feeling) and tail (the conclusion) of huge entries."""
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return (text[:head].rsplit(" ", 1)[0] + "\n[... trimmed for import ...]\n"
            + text[-tail:].split(" ", 1)[-1])


def filter_entries(entries: list[dict], *, min_chars: int = 80,
                   min_score: float = 1.0, similarity: float = 0.90,
                   max_chars: int = 6000) -> tuple[list[dict], dict]:
    """Filter a run's entries (dicts with 'text'; other keys pass through).

    Returns (kept_entries, stats). Order is preserved; dedupe keeps the FIRST
    occurrence (oldest, since batches run chronologically).
    """
    stats = {"in": len(entries), "kept": 0, "dropped_short": 0,
             "dropped_duplicate": 0, "dropped_low_signal": 0, "trimmed": 0,
             "chars_in": 0, "chars_out": 0}
    kept: list[dict] = []
    seen_tokens: list[set] = []
    for entry in entries:
        text = _clean(entry.get("text") or "")
        stats["chars_in"] += len(entry.get("text") or "")
        if len(text) < min_chars:
            stats["dropped_short"] += 1
            continue
        tokens = _tokens(text)
        if any(_jaccard(tokens, prior) >= similarity for prior in seen_tokens):
            stats["dropped_duplicate"] += 1
            continue
        if score_entry(text) < min_score:
            stats["dropped_low_signal"] += 1
            continue
        trimmed = trim_entry(text, max_chars)
        if trimmed is not text:
            stats["trimmed"] += 1
        seen_tokens.append(tokens)
        stats["chars_out"] += len(trimmed)
        stats["kept"] += 1
        kept.append({**entry, "text": trimmed})
    return kept, stats
