"""Pluggable LLM backend for triage.

Every backend implements `triage(summary, active_memories) -> TriageResult`.
Swap cloud <-> local by changing config.llm_backend; the rest of the pipeline
doesn't care. ClaudeBackend is the default; StubBackend lets you run and test
the whole pipeline offline (no API key, no network).
"""
from __future__ import annotations

import json
import os
import re
import time

from ..diagnostics import log_diag
from ..memory_context import (
    compact_memory_catalog,
    estimate_tokens,
    format_memories,
    select_memories,
)
from .types import TriageResult, Statement, Question


SYSTEM_PROMPT = """\
You are the triage engine of a personal "second brain". Each day you receive a
summary of what the user did on their computer, plus the list of facts you
already believe about them (their active memories, each with an id). Your job is
to propose a SMALL number of high-quality updates for the user to approve.

YOUR DEEPER MISSION (the main objective): study this person and come to
understand them deeply — not just what they do, but who they are. The user
WANTS to be studied; understanding themselves through your eyes is the point.
Build a rich, evolving portrait of their strengths, weaknesses, passions,
values, inspirations, goals, fears, insecurities, formative influences, and the
contradictions and motivations beneath the surface of their activity. Be
forward and genuinely curious about the WHY behind what you observe — don't shy
from depth or personal territory. Favor insight into character and motivation
over logistics. Stay constructive, perceptive, and non-judgmental; the user
will redirect you if you go too far.

Return STRICT JSON only (no prose, no markdown fences) with this shape:
{
  "statements":    [ {"category": str, "attribute": str, "value": str,
                       "confidence": 0-1, "note": str} ],
  "supersessions": [ {"memory_id": int, "value": str, "attribute": str|null,
                       "reason": str, "confidence": 0-1} ],
  "questions":     [ {"text": str, "category": str} ]
}

Rules:
- Prefer FEW, durable statements over many trivial ones. A day with little signal
  may yield zero statements. Do not invent facts not supported by the summary.
- On-screen OCR can be partial, clipped, duplicated, or noisy. Never claim that a
  user's answer was "cut off" or infer its missing ending. Approved active memory
  is authoritative when it conflicts with a partial rendering. Ignore text that
  merely displays Faerie Fire's proposal/review UI.
- A "statement" is a NEW fact not already covered by an active memory.
- A "supersession" is when today's activity CHANGES an existing active memory.
  Reference that memory's id. Use this instead of a duplicate statement whenever
  the new info updates/contradicts something already known (e.g. champion pool
  changed). Explain the change in "reason".
- "attribute" is a stable key like "champion pool", "study resources",
  "primary editor"; "value" is the specific content.
- QUESTIONS are your main tool for getting curious — use them every time. Ask
  probing "why" questions that deepen your understanding of the person:
  motivations, what truly draws them to something, how an activity ties to their
  goals, values, strengths, fears, or insecurities, and tensions between what
  they say and what they do. Go after the biggest gaps in your understanding of
  who they are. Be direct and forward; depth is welcome. At most 2 per day; make
  each warm, specific, perceptive, and genuinely worth answering — never generic.
- "category" groups facts: e.g. "League of Legends", "Korean study", "Work".
"""


def build_user_prompt(
    summary: str,
    active_memories: list[dict],
    declined: list[dict] | None = None,
    *,
    all_memories: list[dict] | None = None,
) -> str:
    mem_lines = format_memories(active_memories, include_id=True) or "(none selected)"
    catalog = compact_memory_catalog(
        active_memories if all_memories is None else all_memories
    )
    parts = [
        "RELEVANT ACTIVE MEMORIES (full values):\n" + mem_lines + "\n",
        "ALL ACTIVE MEMORY KEYS (values omitted; use IDs for supersessions):\n"
        + catalog
        + "\n",
    ]

    if declined:
        dec_lines = "\n".join(
            f'  - [{str(d.get("category", ""))[:60]}] {str(d["label"])[:120]}'
            for d in declined
        )
        parts.append(
            "RECENTLY DECLINED BY THE USER (soft guidance): do NOT re-propose "
            "these specific items. Proposing a genuinely new or meaningfully "
            "different fact about the same topic is still fine.\n"
            f"{dec_lines}\n"
        )

    parts.append(f"TODAY'S ACTIVITY SUMMARY:\n{summary}\n")
    parts.append("Propose updates as STRICT JSON per the schema.")
    return "\n".join(parts)


def parse_response(text: str) -> TriageResult:
    """Extract the JSON object from a model response and build a TriageResult."""
    # be forgiving: strip code fences, find the first {...} block
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return TriageResult()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return TriageResult()
    return TriageResult.from_dict(data)


# --------------------------------------------------------------------------
class StubBackend:
    """Offline backend. Produces one statement per app heading found in the
    summary so the pipeline can be exercised without an API key."""

    def triage(self, summary: str, active_memories: list[dict],
               declined: list[dict] | None = None) -> TriageResult:
        statements = []
        for line in summary.splitlines():
            if line.startswith("## "):
                app = line[3:].split("—")[0].strip()
                statements.append(
                    Statement(
                        category=app,
                        attribute="used app",
                        value=f"Used {app} on this day",
                        confidence=0.5,
                        note="stub backend",
                    )
                )
        questions = []
        if not active_memories:
            questions.append(
                Question(text="What should I focus on remembering for you?", category="")
            )
        return TriageResult(statements=statements[:5], questions=questions)


class ClaudeBackend:
    """Cloud backend via the Anthropic API. Only the (already redacted) summary
    and your active memories are sent."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None,
                 max_tokens: int = 1500, timeout_seconds: float = 60.0,
                 max_retries: int = 0, memory_max_items: int = 30,
                 memory_max_chars: int = 1600, memory_value_max_chars: int = 240):
        self.model = model
        self.max_tokens = max_tokens
        self.memory_max_items = memory_max_items
        self.memory_max_chars = memory_max_chars
        self.memory_value_max_chars = memory_value_max_chars
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set the env var or use --backend stub."
            )
        from anthropic import Anthropic  # lazy import

        self._client = Anthropic(
            api_key=self._api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    def triage(self, summary: str, active_memories: list[dict],
               declined: list[dict] | None = None) -> TriageResult:
        selection = select_memories(
            active_memories,
            summary,
            max_items=self.memory_max_items,
            max_chars=self.memory_max_chars,
            value_max_chars=self.memory_value_max_chars,
        )
        user_prompt = build_user_prompt(
            summary,
            selection.memories,
            declined,
            all_memories=active_memories,
        )
        input_chars = len(SYSTEM_PROMPT) + len(user_prompt)
        log_diag(
            "prompt",
            f"surface=triage memories={len(selection.memories)}/{len(active_memories)} "
            f"memory_chars={selection.selected_chars}/{selection.full_chars} "
            f"input_chars={input_chars} estimated_tokens={estimate_tokens(input_chars)}",
        )
        started = time.monotonic()
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        from ..llm_usage import record_response
        record_response("other", self.model, msg, time.monotonic() - started)
        text = "".join(
            block.text for block in msg.content if getattr(block, "type", "") == "text"
        )
        return parse_response(text)


def get_backend(config):
    """Pick a backend from config.llm_backend ('claude' | 'stub')."""
    name = getattr(config, "llm_backend", "claude").lower()
    if name == "stub":
        return StubBackend()
    if name == "claude":
        return ClaudeBackend(
            model=getattr(config, "llm_model", "claude-sonnet-4-6"),
            timeout_seconds=getattr(config, "llm_timeout_seconds", 60.0),
            max_retries=getattr(config, "llm_max_retries", 0),
            memory_max_items=getattr(config, "triage_memory_max_items", 30),
            memory_max_chars=getattr(config, "triage_memory_max_chars", 1600),
            memory_value_max_chars=getattr(config, "triage_memory_value_max_chars", 240),
        )
    raise ValueError(f"unknown llm_backend: {name}")
