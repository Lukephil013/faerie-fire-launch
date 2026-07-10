"""Soul Calibration: a deliberately mechanical, sequential Q&A in its own
popout drawer, independent of Command Center chat.

Design: FIELDS below is asked strictly one at a time, in order, numbered
("3/13") — the machinery stays visible on purpose, the same way a controller
or camera calibration would. Each question is direct and closed enough to
answer in one line. This module holds only the fixed question list and pure
helpers for ordering/progress; no model call happens per question — the
drawer saves each answer directly (see Companion.calibration_save in
brain.py). The model is invoked exactly once, after the last question is
answered or skipped, to produce a single synthesis/reflection message posted
into chat (see Companion.calibration_synthesis).

Progress is deliberately not tracked separately from the facts themselves
(see livingpc.memory.MemoryStore.core_profile_facts/.upsert_core_profile_fact):
"what's left" is always just "which attributes have no saved fact yet, and
weren't skipped this session." That self-heals across restarts, works the
same whether it's mid-onboarding or months in, and a skipped topic simply
resurfaces in a later session (or after /recalibrate) instead of needing its
own tracked state.
"""
from __future__ import annotations

FIELDS = [
    {"section": "Style Anchors", "attribute": "favorite movies", "priority": 72,
     "label": "Favorite movie",
     "prompt": "Favorite movie?"},
    {"section": "Style Anchors", "attribute": "favorite tv shows", "priority": 72,
     "label": "Favorite TV show",
     "prompt": "Favorite TV show?"},
    {"section": "Style Anchors", "attribute": "favorite non-fiction books", "priority": 70,
     "label": "Favorite non-fiction book",
     "prompt": "A non-fiction book, essay, or thinker that actually shaped you?"},
    {"section": "Style Anchors", "attribute": "favorite fiction books", "priority": 70,
     "label": "Favorite fiction book",
     "prompt": "All-time favorite novel, manga, comic, or fictional world?"},
    {"section": "Style Anchors", "attribute": "favorite songs", "priority": 68,
     "label": "Favorite song",
     "prompt": "A song that means something to you?"},
    {"section": "Style Anchors", "attribute": "other beloved references", "priority": 68,
     "label": "Other things you love",
     "prompt": "List anything else you love that you'd want Faerie to know about — favorite games, bands, books, cities, countries, vacations, quotes, whatever comes to mind. List as many as you like, and feel free to expand on any of them."},
    {"section": "Current Reality", "attribute": "current work situation", "priority": 98,
     "label": "Work, direction, and obligation",
     "prompt": "What's your current job? What do you wish you were doing instead, if anything? What's your goal or direction — even if it's still just a dream? And does obligation feel like it has an oversized role in your life right now?"},
    {"section": "Body & Energy", "attribute": "current body and energy realities", "priority": 94,
     "label": "Body and energy realities",
     "prompt": "Physically, sleep-wise, energy-wise — is that fairly steady for you, or are there constraints you'd like Faerie to keep in mind?"},
    {"section": "Fear & Protection", "attribute": "recurring threats that are not always real threats", "priority": 92,
     "label": "Fears and protective loops",
     "prompt": "What situations does your brain treat as threatening, even when part of you knows they may not be?"},
    {"section": "Values & Identity", "attribute": "values and identity anchors", "priority": 96,
     "label": "Values and identity anchors",
     "prompt": "What feels core to your identity: beauty, humor, craft, spirituality, play, relationships, honesty, freedom, etc.?"},
    {"section": "Relationships", "attribute": "relationship and support context", "priority": 88,
     "label": "Relationships and support",
     "prompt": "Who are the key people in your life right now? Who do you actually lean on when things get hard — and is there anyone who drains you more than they support you?"},
    {"section": "Dreams & Direction", "attribute": "dreams and desired direction", "priority": 90,
     "label": "Dreams and direction",
     "prompt": "Where are you with regards to your dreams — how far off do they feel, or how close? And how much suffering are you sitting with right now from not being where you want to be?"},
    {"section": "Core Identity", "attribute": "other essential context", "priority": 86,
     "label": "Other essential context",
     "prompt": "Anything else that feels essential?"},
]


def field_key(field: dict) -> str:
    return field["section"] + "::" + field["attribute"]


def sections_in_order() -> list[str]:
    """Unique section names, in FIELDS' original order."""
    seen: list[str] = []
    for field in FIELDS:
        if field["section"] not in seen:
            seen.append(field["section"])
    return seen


def remaining_fields(answered_keys, skipped_keys=()) -> list[dict]:
    skipped_keys = set(skipped_keys)
    return [f for f in FIELDS
            if field_key(f) not in answered_keys and field_key(f) not in skipped_keys]


def next_field(answered_keys, skipped_keys=()) -> dict | None:
    """The single next question to ask — strictly sequential, one at a time."""
    remaining = remaining_fields(answered_keys, skipped_keys)
    return remaining[0] if remaining else None


def remaining_by_section(answered_keys, skipped_keys=()) -> dict[str, list[dict]]:
    """Remaining fields grouped by section — used only by the progress
    widget (calibration_status), not for pacing the conversation."""
    grouped: dict[str, list[dict]] = {}
    for field in remaining_fields(answered_keys, skipped_keys):
        grouped.setdefault(field["section"], []).append(field)
    return {section: grouped[section] for section in sections_in_order() if section in grouped}
