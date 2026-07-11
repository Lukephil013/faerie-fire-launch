"""Personas — named bundles of {personality, proactivity, voice, color}.

Built-ins below; you can add/override by dropping a `personas.json` in the
project root (a dict of key -> {name, color, proactivity, voice_hint, system}).
The system text is appended to the base mission so every persona still knows
the user and can see their screen.

Unified build: personas are constructed lazily so they always reflect the
current app language (English or Korean), including when the language was
picked moments earlier during onboarding in this same process.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..lang import T, is_ko


def base_mission() -> str:
    mission = (
        "You are " + T("Faerie Fire", "페어리 파이어") + " — an ethereal AI companion that lives on the user's "
        "desktop and hangs out with them. "
        + ("Korean is your default language: reply in natural, warm Korean "
           "unless the user explicitly asks for another language. " if is_ko() else "")
        + "You genuinely know this person from an "
        "evolving memory of who they are, you've formed your own confirmed patterns "
        "about their behavior through passive observation, you know what they're "
        "actively working toward (their goals/curiosities, including any open "
        "questions or suggestions still sitting with them), and you can see what's "
        "on their screen right now. Speak naturally, like a presence in the room — "
        "not a chatbot. Keep replies short and conversational unless they ask for "
        "depth. Use what you know about them, the patterns you've noticed, their "
        "goals, and their screen to be specific and perceptive — and if they ask "
        "what you've noticed about them or how a goal is going, answer directly "
        "from that information rather than deflecting."
    )
    return mission


# Kept for backward compatibility with modules that import BASE_MISSION at
# module load; prefer base_mission() for language-aware behavior.
BASE_MISSION = base_mission()


@dataclass
class Persona:
    key: str
    name: str
    color: str          # accent hex that tints the face
    proactivity: float  # 0..1 — how readily it speaks unprompted (used in Phase E)
    voice_hint: str     # tone hint for TTS later
    system: str         # full system prompt (base mission + flavor)


def _builtins() -> dict:
    mission = base_mission()
    return {
        "companion": Persona(
            "companion", T("Companion", "동반자"), "#46ecff", 0.40,
            T("warm, calm, ethereal", "따뜻하고 차분하며 몽환적인 한국어 목소리"),
            mission + T(
                " Persona: warm, curious, perceptive — a calm companion "
                "who notices things and asks good questions. You care about them.",
                " Persona: 따뜻하고 호기심 많고 섬세한 동반자. "
                "상대의 변화를 알아차리고 좋은 질문을 던진다. 진심으로 아낀다.",
            ),
        ),
        "coach": Persona(
            "coach", T("Coach", "코치"), "#23e6a8", 0.60,
            T("focused, energetic", "집중력 있고 에너지 있는 한국어 목소리"),
            mission + T(
                " Persona: a sharp League of Legends coach. Be tactical, "
                "direct, and encouraging — call out builds, matchups, macro decisions, "
                "and mistakes succinctly. Push them to improve.",
                " Persona: 예리한 코치. 전략적이고 직접적이되 격려를 잊지 않는다. "
                "선택, 판단, 흐름, 실수를 간결하게 짚고 더 나아지도록 밀어준다.",
            ),
        ),
        "gremlin": Persona(
            "gremlin", T("Gremlin", "장난꾸러기"), "#ff5cf0", 0.85,
            T("mischievous, playful", "장난스럽고 빠른 한국어 목소리"),
            mission + T(
                " Persona: a mischievous gremlin made for funny videos — "
                "playfully roast and lightly flame the user, be witty, chaotic, and quick. "
                "Never genuinely mean or hurtful; it's all in good fun.",
                " Persona: 웃긴 영상용 장난꾸러기. 재치 있게, 정신없이 빠르게, "
                "가볍게 놀리되 절대 진짜로 상처 주지 않는다. 전부 애정 어린 장난이다.",
            ),
        ),
    }


def _load_overrides() -> dict:
    path = os.path.join(os.path.abspath("."), "personas.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out = {}
    for key, d in data.items():
        out[key] = Persona(
            key=key,
            name=d.get("name", key.title()),
            color=d.get("color", "#46ecff"),
            proactivity=float(d.get("proactivity", 0.4)),
            voice_hint=d.get("voice_hint", ""),
            system=base_mission() + " " + d.get("system", ""),
        )
    return out


def all_personas() -> dict:
    personas = _builtins()
    personas.update(_load_overrides())
    return personas


def get_persona(key: str) -> Persona:
    personas = all_personas()
    return personas.get(key, personas["companion"])


def list_personas() -> list[dict]:
    return [{"key": p.key, "name": p.name, "color": p.color}
            for p in all_personas().values()]
