"""Soul Calibration: a deliberately sequential reflective inventory in its
own popout drawer, independent of Command Center chat.

Design: FIELDS below is asked strictly one at a time, in order. The questions
are grouped into balanced sections so the current section can stay open while
the rest remain collapsed. Answers may be long and multiline. This module
holds only the fixed question list and pure helpers for ordering/progress; no
model call happens per question — the drawer saves each answer directly (see
Companion.calibration_save in brain.py). The model is invoked exactly once,
after the last question is answered or skipped, to produce a single
synthesis/reflection message posted into chat (see
Companion.calibration_synthesis).

Progress is derived from saved facts plus the persisted skip set rather than
a separate counter (see MemoryStore.core_profile_facts and Companion's
calibration helpers): "what's left" is always just "which attributes have no
saved fact and have not been skipped." That self-heals across restarts and
works the same whether it is mid-onboarding or months later. /recalibrate
clears both answers and skips so every question resurfaces.
"""
from __future__ import annotations

from .lang import is_ko

# (section_en, section_ko, storage_section, attribute, priority,
#  label_en, label_ko, prompt_en, prompt_ko)
_FIELD_DEFS = [
    # 1. Work, Dreams & Freedom
    ("Work, Dreams & Freedom", "일, 꿈과 자유", "Work, Dreams & Freedom",
     "work and livelihood", 98, "Current work and livelihood", "현재의 일과 생계",
     "Do you currently have a job or another primary way of supporting yourself? How do you feel about it? Describe the parts you enjoy and the parts you dislike. If you could earn a living doing anything else, what would it be?",
     "현재 직업이나 주된 생계 수단이 있나요? 그것에 대해 어떻게 느끼나요? 좋아하는 부분과 싫어하는 부분을 설명해주세요. 다른 어떤 일로든 생계를 꾸릴 수 있다면 무엇을 하고 싶나요?"),
    ("Work, Dreams & Freedom", "일, 꿈과 자유", "Work, Dreams & Freedom",
     "recurring life dream", 95, "A recurring life dream", "계속 돌아오는 삶의 꿈",
     "What is one dream for your life that always comes back around? Maybe you fantasize about it or work on it a little, but work and other obligations keep getting in the way.",
     "계속 마음속으로 돌아오는 삶의 꿈 하나는 무엇인가요? 자주 상상하거나 조금씩 해보지만 일과 다른 의무 때문에 뒤로 밀리는 것일 수도 있어요."),
    ("Work, Dreams & Freedom", "일, 꿈과 자유", "Work, Dreams & Freedom",
     "oversized obligations", 92, "The weight of obligation", "의무감의 무게",
     "Does obligation feel like it has an oversized role in your life right now—whether to a job, relationship, family role, or something else? If so, please elaborate.",
     "지금 삶에서 의무가 지나치게 큰 역할을 한다고 느끼나요? 직장, 관계, 가족 역할 또는 다른 무엇이든 괜찮습니다. 그렇다면 자세히 이야기해주세요."),
    ("Work, Dreams & Freedom", "일, 꿈과 자유", "Work, Dreams & Freedom",
     "freedom and constraint", 90, "Freedom and constraint", "자유와 제약",
     "Where in your life do you feel most free? Where do you feel least free?",
     "삶의 어느 부분에서 가장 자유롭다고 느끼나요? 어디에서 가장 자유롭지 못하다고 느끼나요?"),

    # 2. Body & Regulation
    ("Body & Regulation", "몸과 조절", "Body & Regulation",
     "physical conditions and symptoms", 94, "Physical conditions and symptoms", "신체 상태와 증상",
     "Do you have any physical conditions or symptoms that affect your daily life—such as chronic pain, recurring digestive issues, fatigue, or anything else? Please share what you know about them and how they affect you.",
     "일상에 영향을 주는 신체적 질환이나 증상이 있나요? 만성 통증, 반복되는 소화 문제, 피로 또는 그 밖의 무엇이든 괜찮습니다. 알고 있는 내용과 일상에 미치는 영향을 알려주세요."),
    ("Body & Regulation", "몸과 조절", "Body & Regulation",
     "sleep schedule and relationship", 91, "Sleep schedule and relationship", "수면 일정과 수면에 대한 느낌",
     "What is your sleep schedule like? Is it consistent, or does it shift from day to day? How do you feel about sleep in general: does it feel restorative, enjoyable, inconvenient, difficult, or something else?",
     "수면 일정은 어떤가요? 일정한가요, 아니면 날마다 달라지나요? 잠은 회복이 되거나 즐겁거나, 불편하거나 어렵거나, 혹은 다른 느낌인가요?"),
    ("Body & Regulation", "몸과 조절", "Body & Regulation",
     "daily energy rhythm", 92, "Daily energy rhythm", "하루의 에너지 리듬",
     "What is your energy like throughout a typical day? When do you feel most alive, and when do you tend to fade? Do you usually feel excited to begin your daily tasks?",
     "보통 하루 동안 에너지가 어떻게 변하나요? 언제 가장 생생하게 느끼고, 언제 기운이 떨어지나요? 대개 하루의 할 일을 시작할 때 기대감이 드나요?"),
    ("Body & Regulation", "몸과 조절", "Body & Regulation",
     "response to threat and overwhelm", 90, "Response to threat and overwhelm", "위협과 압도감에 대한 반응",
     "When you feel threatened, overwhelmed, or uncertain, what do you usually do—withdraw, overthink, appease, control, distract yourself, become angry, sleep, or something else?",
     "위협받거나 압도되거나 확신이 없을 때 보통 어떻게 하나요? 물러나거나, 지나치게 생각하거나, 맞춰주거나, 통제하거나, 주의를 돌리거나, 화를 내거나, 잠을 자거나, 혹은 다른 반응을 하나요?"),

    # 3. Fear, Patterns & Loss
    ("Fear, Patterns & Loss", "두려움, 패턴과 상실", "Fear, Patterns & Loss",
     "biggest current fear", 93, "Biggest current fear", "지금 가장 큰 두려움",
     "If you feel comfortable sharing, what is your biggest fear right now?",
     "편하게 나눌 수 있다면, 지금 가장 큰 두려움은 무엇인가요?"),
    ("Fear, Patterns & Loss", "두려움, 패턴과 상실", "Fear, Patterns & Loss",
     "recurring stuck patterns", 94, "Recurring stuck patterns", "반복해서 발목을 잡는 패턴",
     "Are you aware of any ongoing patterns or habits that may be keeping you stuck? Even if you are unsure, describe any issues that repeatedly appear in your life and feel painful, confusing, or unwanted. For example: “I am afraid of planes; I think it may be because they make me feel out of control.”",
     "계속 제자리에 머물게 하는 패턴이나 습관을 알고 있나요? 확실하지 않아도 괜찮습니다. 삶에서 반복해서 나타나며 고통스럽거나 혼란스럽거나 원치 않는 문제를 설명해주세요. 예: ‘비행기가 무서워요. 통제할 수 없다고 느끼게 해서인 것 같아요.’"),
    ("Fear, Patterns & Loss", "두려움, 패턴과 상실", "Fear, Patterns & Loss",
     "difficult emotions", 89, "Difficult emotions", "다루기 어려운 감정",
     "Which emotions are hardest for you to feel, express, or tolerate?",
     "어떤 감정을 느끼거나 표현하거나 견디는 것이 가장 어렵나요?"),
    ("Fear, Patterns & Loss", "두려움, 패턴과 상실", "Fear, Patterns & Loss",
     "current grief", 90, "Current grief", "지금의 애도와 상실",
     "Is there anything you are grieving, even if it is not a death—such as a lost future, identity, relationship, opportunity, or version of yourself?",
     "죽음이 아니더라도 애도하고 있는 것이 있나요? 잃어버린 미래, 정체성, 관계, 기회 또는 예전의 자신 같은 것일 수 있어요."),

    # 4. Identity & Aspirations
    ("Identity & Aspirations", "정체성과 지향", "Identity & Aspirations",
     "self admiration", 90, "What you admire about yourself", "스스로에게서 존경하는 점",
     "What do you admire about yourself?", "자신의 어떤 점을 존경하나요?"),
    ("Identity & Aspirations", "정체성과 지향", "Identity & Aspirations",
     "heroes and shared traits", 86, "Heroes and shared traits", "영웅과 닮은 특성",
     "Who are your heroes? What trait do you admire most in each of them? Do you recognize any of those traits in yourself?",
     "당신의 영웅은 누구인가요? 각 사람에게서 가장 존경하는 특성은 무엇인가요? 그 특성 중 자신에게도 있다고 느끼는 것이 있나요?"),
    ("Identity & Aspirations", "정체성과 지향", "Identity & Aspirations",
     "qualities admired in others", 84, "Qualities admired in others", "다른 사람에게서 존경하는 자질",
     "What other qualities do you consistently admire in people?",
     "사람들에게서 꾸준히 존경하게 되는 다른 자질은 무엇인가요?"),
    ("Identity & Aspirations", "정체성과 지향", "Identity & Aspirations",
     "distance from ideal self", 92, "Distance from your ideal self", "이상적인 자신과의 거리",
     "How far away would you say you are from your “ideal self”—the person or self you want to be?",
     "되고 싶은 사람, 즉 ‘이상적인 자신’과 지금의 자신 사이에는 얼마나 거리가 있다고 느끼나요?"),

    # 5. Connection & Aliveness
    ("Connection & Aliveness", "연결과 생동감", "Connection & Aliveness",
     "most important relationship", 93, "Most important relationship", "가장 중요한 관계",
     "Who is the most important person in your life? How would you describe the health of your relationship with them? What quality do you appreciate most in them, and which of their qualities do you find the most frustrating?",
     "삶에서 가장 중요한 사람은 누구인가요? 그 사람과의 관계는 얼마나 건강하다고 느끼나요? 가장 고마운 자질과 가장 답답하게 느끼는 자질은 무엇인가요?"),
    ("Connection & Aliveness", "연결과 생동감", "Connection & Aliveness",
     "energy draining relationship", 88, "An energy-draining relationship", "에너지를 소모시키는 관계",
     "Is there someone in your life who consistently drains your energy? What seems to create that dynamic, and what keeps the relationship in your life?",
     "계속해서 에너지를 소모시키는 사람이 있나요? 무엇이 그런 관계의 역학을 만들고, 무엇 때문에 그 관계를 삶에 계속 두고 있나요?"),
    ("Connection & Aliveness", "연결과 생동감", "Connection & Aliveness",
     "receiving and giving in relationships", 89, "Receiving and giving", "관계에서 받고 주는 것",
     "In your closest relationships, what do you most want to receive? What do you most naturally give?",
     "가장 가까운 관계에서 무엇을 가장 받고 싶나요? 자신은 무엇을 가장 자연스럽게 주나요?"),
    ("Connection & Aliveness", "연결과 생동감", "Connection & Aliveness",
     "moments of excitement and aliveness", 91, "Moments of excitement and aliveness", "흥분과 생동감을 느끼는 순간",
     "Have you ever done something that made you think, “That was really cool—I want to do more of that”? This can be a moment of excitement, fascination, pride, or aliveness. For example, while programming this application, seeing code move quickly through the terminal gives me a momentary feeling of “That’s awesome.” What gives you that feeling?",
     "무언가를 하고 나서 ‘정말 멋졌어. 이걸 더 하고 싶다’고 느낀 적이 있나요? 흥분, 매혹, 자부심 또는 생동감을 느낀 순간일 수 있어요. 예를 들어 이 앱을 만들면서 터미널에서 코드가 빠르게 움직이는 모습을 보면 잠깐 ‘멋지다’는 느낌이 들어요. 무엇이 당신에게 그런 느낌을 주나요?"),

    # 6. Joy, Meaning & Inner Life
    ("Joy, Meaning & Inner Life", "기쁨, 의미와 내면", "Joy, Meaning & Inner Life",
     "childhood joy", 82, "Childhood joy", "어린 시절의 기쁨",
     "What did you love doing as a child?", "어린 시절 무엇을 하는 것을 좋아했나요?"),
    ("Joy, Meaning & Inner Life", "기쁨, 의미와 내면", "Joy, Meaning & Inner Life",
     "current source of meaning", 96, "Current source of meaning", "지금 삶의 의미",
     "What gives you the most meaning in life right now? Do you know why?",
     "지금 삶에서 가장 큰 의미를 주는 것은 무엇인가요? 그 이유를 알고 있나요?"),
    ("Joy, Meaning & Inner Life", "기쁨, 의미와 내면", "Joy, Meaning & Inner Life",
     "spiritual philosophical or intuitive practice", 84, "Spiritual or philosophical practice", "영적·철학적 실천",
     "Do you have a spiritual, philosophical, or intuitive practice? If so, how does it shape your choices?",
     "영적, 철학적 또는 직관적인 실천이 있나요? 있다면 그것이 선택에 어떤 영향을 주나요?"),
    ("Joy, Meaning & Inner Life", "기쁨, 의미와 내면", "Joy, Meaning & Inner Life",
     "calibration guiding question", 95, "Your guiding question", "이 조율을 이끌 질문",
     "What is the one question you most want this calibration to help you answer?",
     "이 조율을 통해 가장 답을 찾고 싶은 질문 하나는 무엇인가요?"),

    # 7. Choices & Reflection
    ("Choices & Reflection", "선택과 성찰", "Choices & Reflection",
     "calibration surprise", 82, "What surprised you", "놀라웠던 점",
     "After answering these questions, what surprised you?",
     "이 질문들에 답한 뒤 무엇이 놀랍게 느껴졌나요?"),
    ("Choices & Reflection", "선택과 성찰", "Choices & Reflection",
     "answer carrying the most energy", 88, "The answer carrying the most energy", "가장 큰 에너지가 담긴 답",
     "Which answer carries the most energy for you, whether comfortable or uncomfortable?",
     "편안하든 불편하든, 어떤 답에 가장 큰 에너지가 담겨 있나요?"),
    ("Choices & Reflection", "선택과 성찰", "Choices & Reflection",
     "most impactful life choice", 94, "The choice that changed your life most", "삶에 가장 큰 영향을 준 선택",
     "What is the choice that has made the most impact on your life? For example: choosing to study abroad at university, or choosing to quit drinking.",
     "당신의 삶에 가장 큰 영향을 준 선택은 무엇인가요? 예를 들면 대학에서 유학을 선택한 것, 술을 끊기로 선택한 것 등이 있어요."),
    ("Choices & Reflection", "선택과 성찰", "Choices & Reflection",
     "best life choice", 92, "The best choice you ever made", "가장 잘한 선택",
     "What do you see as the best choice you have ever made?",
     "지금까지 했던 선택 중 가장 잘한 선택은 무엇이라고 생각하나요?"),
    ("Choices & Reflection", "선택과 성찰", "Choices & Reflection",
     "worst life choice and wisdom", 92, "The worst choice and what it taught you", "가장 힘들었던 선택과 그 지혜",
     "What do you see as the worst choice you have ever made, and why? Would you undo it, or was the wisdom you gained worth it?",
     "가장 잘못했다고 느끼는 선택은 무엇이며, 그 이유는 무엇인가요? 되돌리고 싶나요, 아니면 그 선택에서 얻은 지혜가 그만한 가치가 있었나요?"),

    # 8. Favorites & Open Space — intentionally just two questions.
    ("Favorites & Open Space", "좋아하는 것과 열린 공간", "Favorites & Open Space",
     "favorite media and creators", 72, "Favorite media and creators", "좋아하는 미디어와 크리에이터",
     "What are some of your favorite movies, TV shows, books, podcasts, YouTubers, games, or other media and creators? Tell us as much or as little as you like.",
     "가장 좋아하는 영화, TV 프로그램, 책, 팟캐스트, 유튜버, 게임 또는 그 밖의 미디어와 크리에이터는 무엇인가요? 원하는 만큼 편하게 적어주세요."),
    ("Favorites & Open Space", "좋아하는 것과 열린 공간", "Core Identity",
     "other essential context", 86, "Anything else", "그 밖에 알려주고 싶은 것",
     "Is there anything else you want Faerie to know about you?",
     "페어리가 당신에 대해 알아두면 좋을 다른 것이 있나요?"),
]


_LEGACY_SECTION_ALIASES = {
    "favorite media and creators": {
        "Style Anchors", "취향의 기준점",
        "Voices, Books & Open Space", "목소리, 책과 열린 공간",
    },
    "other essential context": {"Core Identity", "핵심 정체성"},
}

_LEGACY_ATTRIBUTE_ALIASES = {
    "favorite movies": "favorite media and creators",
    "favorite tv shows": "favorite media and creators",
    "favorite podcaster": "favorite media and creators",
    "favorite book": "favorite media and creators",
    "favorite youtube creator": "favorite media and creators",
}


def _build_fields() -> list[dict]:
    ko = is_ko()
    return [
        {"section": s_ko if ko else s_en, "section_en": s_en, "section_ko": s_ko,
         # Storage stays language-neutral; only labels are localized.
         "storage_section": storage_section, "attribute": attr, "priority": pri,
         "label": l_ko if ko else l_en, "prompt": p_ko if ko else p_en}
        for (s_en, s_ko, storage_section, attr, pri,
             l_en, l_ko, p_en, p_ko) in _FIELD_DEFS
    ]


def __getattr__(name: str):
    # PEP 562 lazy module attribute: FIELDS always reflects the CURRENT app
    # language, including when it was chosen moments ago during onboarding in
    # this same process. Existing call sites (`soul_calibration.FIELDS`) keep
    # working unchanged while field_key() remains language-neutral.
    if name == "FIELDS":
        return _build_fields()
    raise AttributeError(name)


def field_key(field: dict) -> str:
    return str(field.get("storage_section") or field.get("section_en") or field["section"]) + \
        "::" + field["attribute"]


def resolve_field(section: str, attribute: str) -> dict | None:
    """Find a field from either a localized display section or old storage."""
    section = str(section or "").strip()
    attribute = str(attribute or "").strip()
    attribute = _LEGACY_ATTRIBUTE_ALIASES.get(attribute, attribute)
    for field in _build_fields():
        if attribute == field["attribute"] and section in {
                field["section"], field["section_en"], field["section_ko"],
                field["storage_section"],
                *_LEGACY_SECTION_ALIASES.get(attribute, set())}:
            return field
    return None


def canonical_key(section: str, attribute: str) -> str:
    field = resolve_field(section, attribute)
    return field_key(field) if field else str(section or "") + "::" + str(attribute or "")


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
