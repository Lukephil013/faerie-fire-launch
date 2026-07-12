"""Tests for deterministic, bounded LLM memory context selection."""
from livingpc.memory_context import (
    compact_memory_catalog,
    format_memories,
    select_memories,
)


def _memory(memory_id, category, attribute, value, valid_from):
    return {
        "id": memory_id,
        "category": category,
        "attribute": attribute,
        "value": value,
        "valid_from": valid_from,
    }


def test_relevance_beats_recency_and_is_deterministic():
    memories = [
        _memory(1, "League of Legends", "champion pool", "Caitlyn", "2026-01-01"),
        _memory(2, "Cooking", "favorite meal", "ramen", "2026-06-01"),
    ]
    first = select_memories(memories, "What should I build on Caitlyn?", max_items=1)
    second = select_memories(memories, "What should I build on Caitlyn?", max_items=1)
    assert first.memories == second.memories
    assert first.memories[0]["id"] == 1


def test_empty_context_falls_back_to_newest():
    memories = [
        _memory(1, "A", "old", "one", "2026-01-01"),
        _memory(2, "B", "new", "two", "2026-06-01"),
    ]
    selected = select_memories(memories, "", max_items=1)
    assert selected.memories[0]["id"] == 2


def test_korean_relevance_beats_recency():
    memories = [
        _memory(1, "공부", "한국어 문법", "조사 연습이 필요하다", "2026-01-01"),
        _memory(2, "요리", "최근 식사", "라면", "2026-06-01"),
    ]
    selected = select_memories(memories, "한국어 문법을 어떻게 공부할까?", max_items=1)
    assert selected.memories[0]["id"] == 1


def test_item_char_and_value_limits_do_not_mutate_source():
    long_value = "x" * 1000
    memories = [
        _memory(i, "Topic", f"attribute {i}", long_value, f"2026-06-{i:02d}")
        for i in range(1, 6)
    ]
    selected = select_memories(
        memories, "Topic", max_items=3, max_chars=240, value_max_chars=80
    )
    assert len(selected.memories) <= 3
    assert len(format_memories(selected.memories)) <= 240
    assert all(len(memory["value"]) <= 80 for memory in selected.memories)
    assert memories[0]["value"] == long_value


def test_fewer_than_limit_and_catalog_keeps_every_id():
    memories = [
        _memory(7, "Work", "project", "Faerie Fire", "2026-06-01"),
        _memory(9, "Study", "language", "Korean", "2026-06-02"),
    ]
    selected = select_memories(memories, "project", max_items=20, max_chars=6000)
    catalog = compact_memory_catalog(memories)
    assert len(selected.memories) == 2
    assert "id=7" in catalog and "id=9" in catalog
    assert "Faerie Fire" not in catalog and "Korean" not in catalog
