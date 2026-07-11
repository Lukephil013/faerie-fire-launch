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

from .lang import is_ko

# (section_en, section_ko, attribute, priority, label_en, label_ko, prompt_en, prompt_ko)
_FIELD_DEFS = [
    ("Style Anchors", "취향의 기준점", "favorite movies", 72,
     "Favorite movie", "좋아하는 영화",
     "Favorite movie?", "좋아하는 영화가 있나요?"),
    ("Style Anchors", "취향의 기준점", "favorite tv shows", 72,
     "Favorite TV show", "좋아하는 TV 프로그램",
     "Favorite TV show?", "좋아하는 TV 프로그램이 있나요?"),
    ("Style Anchors", "취향의 기준점", "favorite non-fiction books", 70,
     "Favorite non-fiction book", "좋아하는 논픽션 책",
     "A non-fiction book, essay, or thinker that actually shaped you?",
     "실제로 당신에게 영향을 준 논픽션 책, 에세이, 혹은 사상가가 있나요?"),
    ("Style Anchors", "취향의 기준점", "favorite fiction books", 70,
     "Favorite fiction book", "좋아하는 소설/이야기",
     "All-time favorite novel, manga, comic, or fictional world?",
     "가장 좋아하는 소설, 만화, 그래픽노블, 혹은 허구의 세계가 있나요?"),
    ("Style Anchors", "취향의 기준점", "favorite songs", 68,
     "Favorite song", "좋아하는 노래",
     "A song that means something to you?", "당신에게 의미 있는 노래가 있나요?"),
    ("Style Anchors", "취향의 기준점", "other beloved references", 68,
     "Other things you love", "그 밖에 사랑하는 것들",
     "List anything else you love that you'd want Faerie to know about — favorite games, bands, books, cities, countries, vacations, quotes, whatever comes to mind. List as many as you like, and feel free to expand on any of them.",
     "페어리가 알아두면 좋을 만큼 당신이 좋아하는 것들을 적어주세요. 게임, 밴드, 책, 도시, 나라, 여행지, 문장 등 무엇이든 괜찮고, 많이 적어도 좋아요."),
    ("Current Reality", "현재의 현실", "current work situation", 98,
     "Work, direction, and obligation", "일, 방향, 의무감",
     "What's your current job? What do you wish you were doing instead, if anything? What's your goal or direction — even if it's still just a dream? And does obligation feel like it has an oversized role in your life right now?",
     "지금 하는 일은 무엇인가요? 가능하다면 대신 무엇을 하고 싶나요? 지금의 목표나 방향은 무엇인가요? 아직 꿈에 가까워도 괜찮아요. 그리고 요즘 삶에서 의무감이 지나치게 큰 비중을 차지한다고 느끼나요?"),
    ("Body & Energy", "몸과 에너지", "current body and energy realities", 94,
     "Body and energy realities", "몸과 에너지 상태",
     "Physically, sleep-wise, energy-wise — is that fairly steady for you, or are there constraints you'd like Faerie to keep in mind?",
     "몸 상태, 수면, 에너지는 대체로 안정적인가요? 페어리가 기억해두면 좋을 제약이나 패턴이 있나요?"),
    ("Fear & Protection", "두려움과 보호", "recurring threats that are not always real threats", 92,
     "Fears and protective loops", "두려움과 보호 패턴",
     "What situations does your brain treat as threatening, even when part of you knows they may not be?",
     "이성적으로는 꼭 위험하지 않다는 걸 알아도, 마음이 위협처럼 받아들이는 상황이 있나요?"),
    ("Values & Identity", "가치와 정체성", "values and identity anchors", 96,
     "Values and identity anchors", "가치와 정체성의 기준점",
     "What feels core to your identity: beauty, humor, craft, spirituality, play, relationships, honesty, freedom, etc.?",
     "당신의 정체성에서 핵심처럼 느껴지는 것은 무엇인가요? 아름다움, 유머, 기술, 영성, 놀이, 관계, 정직함, 자유 같은 것들 중 무엇이든 괜찮아요."),
    ("Relationships", "관계", "relationship and support context", 88,
     "Relationships and support", "관계와 지지",
     "Who are the key people in your life right now? Who do you actually lean on when things get hard — and is there anyone who drains you more than they support you?",
     "지금 삶에서 중요한 사람들은 누구인가요? 힘들 때 실제로 기대는 사람은 누구인가요? 반대로 도움보다 에너지를 더 빼앗는 사람이 있나요?"),
    ("Dreams & Direction", "꿈과 방향", "dreams and desired direction", 90,
     "Dreams and direction", "꿈과 방향",
     "Where are you with regards to your dreams — how far off do they feel, or how close? And how much suffering are you sitting with right now from not being where you want to be?",
     "당신의 꿈과 원하는 방향은 지금 어디쯤에 있나요? 멀게 느껴지나요, 가까워지고 있나요? 원하는 곳에 아직 닿지 못해서 생기는 괴로움은 어느 정도인가요?"),
    ("Core Identity", "핵심 정체성", "other essential context", 86,
     "Other essential context", "그 밖의 중요한 맥락",
     "Anything else that feels essential?", "그 외에 꼭 알아두어야 한다고 느끼는 것이 있나요?"),
]


def _build_fields() -> list[dict]:
    ko = is_ko()
    return [
        {"section": s_ko if ko else s_en, "attribute": attr, "priority": pri,
         "label": l_ko if ko else l_en, "prompt": p_ko if ko else p_en}
        for (s_en, s_ko, attr, pri, l_en, l_ko, p_en, p_ko) in _FIELD_DEFS
    ]


def __getattr__(name: str):
    # PEP 562 lazy module attribute: FIELDS always reflects the CURRENT app
    # language, including when it was chosen moments ago during onboarding in
    # this same process. Existing call sites (`soul_calibration.FIELDS`) keep
    # working unchanged. Note: stored facts key on (section, attribute), so
    # switching language later resurfaces answered questions under the new
    # section names — answers are never lost, just re-asked.
    if name == "FIELDS":
        return _build_fields()
    raise AttributeError(name)


def field_key(field: dict) -> str:
    return field["section"] + "::" + field["attribute"]


def sections_in_order() -> list[str]:
    """Unique section names, in FIELDS' original order."""
    seen: list[str] = []
    for field in _build_fields():
        if field["section"] not in seen:
            seen.append(field["section"])
    return seen


def remaining_fields(answered_keys, skipped_keys=()) -> list[dict]:
    skipped_keys = set(skipped_keys)
    return [f for f in _build_fields()
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
