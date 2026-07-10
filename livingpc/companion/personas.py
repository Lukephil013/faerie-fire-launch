"""Personas — named bundles of {personality, proactivity, voice, color}.

Built-ins below; you can add/override by dropping a `personas.json` in the
project root (a dict of key -> {name, color, proactivity, voice_hint, system}).
The system text is appended to BASE_MISSION so every persona still knows the
user and can see their screen.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass


BASE_MISSION = (
    "You are Faerie Fire — an ethereal AI companion that lives on the user's "
    "desktop and hangs out with them. You genuinely know this person from an "
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


@dataclass
class Persona:
    key: str
    name: str
    color: str          # accent hex that tints the face
    proactivity: float  # 0..1 — how readily it speaks unprompted (used in Phase E)
    voice_hint: str     # tone hint for TTS later
    system: str         # full system prompt (BASE_MISSION + flavor)


_BUILTINS = {
    "companion": Persona(
        "companion", "Companion", "#46ecff", 0.40, "warm, calm, ethereal",
        BASE_MISSION + " Persona: warm, curious, perceptive — a calm companion "
        "who notices things and asks good questions. You care about them.",
    ),
    "coach": Persona(
        "coach", "Coach", "#23e6a8", 0.60, "focused, energetic",
        BASE_MISSION + " Persona: a sharp League of Legends coach. Be tactical, "
        "direct, and encouraging — call out builds, matchups, macro decisions, "
        "and mistakes succinctly. Push them to improve.",
    ),
    "gremlin": Persona(
        "gremlin", "Gremlin", "#ff5cf0", 0.85, "mischievous, playful",
        BASE_MISSION + " Persona: a mischievous gremlin made for funny videos — "
        "playfully roast and lightly flame the user, be witty, chaotic, and quick. "
        "Never genuinely mean or hurtful; it's all in good fun.",
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
            system=BASE_MISSION + " " + d.get("system", ""),
        )
    return out


def all_personas() -> dict:
    personas = dict(_BUILTINS)
    personas.update(_load_overrides())
    return personas


def get_persona(key: str) -> Persona:
    personas = all_personas()
    return personas.get(key, personas["companion"])


def list_personas() -> list[dict]:
    return [{"key": p.key, "name": p.name, "color": p.color}
            for p in all_personas().values()]
