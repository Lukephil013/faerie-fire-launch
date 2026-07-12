"""Bounded, deterministic memory context selection for LLM prompts."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass


_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\uac00-\ud7a3]+")
_STOPWORDS = {
    "about", "after", "again", "also", "and", "are", "because", "been",
    "before", "being", "but", "can", "could", "did", "does", "for", "from",
    "had", "has", "have", "how", "into", "its", "just", "like", "more",
    "not", "now", "our", "out", "should", "some", "than", "that", "the",
    "their", "them", "then", "there", "these", "they", "this", "through",
    "today", "was", "were", "what", "when", "where", "which", "while",
    "who", "why", "will", "with", "would", "you", "your",
}


@dataclass(frozen=True)
class MemorySelection:
    memories: list[dict]
    total_count: int
    full_chars: int
    selected_chars: int

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.selected_chars)


def estimate_tokens(text_or_chars: str | int) -> int:
    """Conservative display-only estimate; no tokenizer dependency required."""
    chars = text_or_chars if isinstance(text_or_chars, int) else len(text_or_chars)
    return math.ceil(max(0, chars) / 4)


def _tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    tokens = {
        token.lower()
        for token in _WORD.findall(lowered)
        if len(token) >= 3 and token.lower() not in _STOPWORDS
    }
    for run in _CJK_RUN_RE.findall(lowered):
        tokens.update(run[i:i + 2] for i in range(max(0, len(run) - 1)))
    return tokens


def _clean(text, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(1, limit - 1)].rstrip() + "…"


def prepare_memory(memory: dict, value_max_chars: int = 500) -> dict:
    """Return a prompt-safe copy without mutating the stored memory."""
    prepared = dict(memory)
    prepared["category"] = _clean(memory.get("category", ""), 100)
    prepared["attribute"] = _clean(memory.get("attribute", ""), 100)
    prepared["value"] = _clean(memory.get("value", ""), value_max_chars)
    return prepared


def format_memory(memory: dict, *, include_id: bool = False) -> str:
    prefix = f'id={memory.get("id")} ' if include_id and memory.get("id") is not None else ""
    return f'- {prefix}[{memory.get("category", "")}] {memory.get("attribute", "")}: {memory.get("value", "")}'


def format_memories(memories: list[dict], *, include_id: bool = False) -> str:
    return "\n".join(format_memory(memory, include_id=include_id) for memory in memories)


def _relevance(memory: dict, context_tokens: set[str], context_lower: str) -> int:
    category = str(memory.get("category", ""))
    attribute = str(memory.get("attribute", ""))
    value = str(memory.get("value", ""))
    score = 0
    score += 6 * len(_tokens(category) & context_tokens)
    score += 8 * len(_tokens(attribute) & context_tokens)
    score += 2 * len(_tokens(value) & context_tokens)
    if category and category.lower() in context_lower:
        score += 10
    if attribute and attribute.lower() in context_lower:
        score += 12
    return score


def select_memories(
    memories: list[dict],
    context: str,
    *,
    max_items: int = 20,
    max_chars: int = 6000,
    value_max_chars: int = 500,
) -> MemorySelection:
    """Rank memories by relevance, then fit prompt copies into hard budgets."""
    full_chars = len(format_memories(memories))
    if max_items <= 0 or max_chars <= 0 or not memories:
        return MemorySelection([], len(memories), full_chars, 0)

    context_lower = (context or "").lower()
    context_tokens = _tokens(context)
    ranked = []
    for index, memory in enumerate(memories):
        score = _relevance(memory, context_tokens, context_lower)
        valid_from = str(memory.get("valid_from", ""))
        memory_id = int(memory.get("id") or 0)
        ranked.append((score, valid_from, memory_id, -index, memory))
    ranked.sort(reverse=True)

    selected: list[dict] = []
    used = 0
    for _, _, _, _, memory in ranked:
        if len(selected) >= max_items:
            break
        prepared = prepare_memory(memory, value_max_chars=value_max_chars)
        line = format_memory(prepared)
        separator = 1 if selected else 0
        remaining = max_chars - used - separator
        if remaining <= 0:
            break
        if len(line) > remaining:
            fixed = len(format_memory({**prepared, "value": ""}))
            value_room = remaining - fixed
            if value_room < 12:
                continue
            prepared["value"] = _clean(prepared.get("value", ""), value_room)
            line = format_memory(prepared)
        selected.append(prepared)
        used += separator + len(line)

    return MemorySelection(selected, len(memories), full_chars, used)


def compact_memory_catalog(memories: list[dict]) -> str:
    """List every active memory key and ID while omitting bulky values."""
    lines = []
    for memory in memories:
        memory_id = memory.get("id")
        category = _clean(memory.get("category", ""), 60)
        attribute = _clean(memory.get("attribute", ""), 60)
        lines.append(f"- id={memory_id} [{category}] {attribute}")
    return "\n".join(lines) or "(none yet)"
