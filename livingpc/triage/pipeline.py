"""Orchestrates a triage run: aggregate -> redact -> recall -> LLM.

Pure-ish: it does the data assembly and the model call, but applying approvals
to the memory store is left to the caller (the review CLI), so the pipeline has
no interactive side effects.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..storage import EventLog, now_iso
from ..memory import MemoryStore
from .aggregate import build_day_summary, build_summary, day_bounds
from .redact import redact
from .types import TriageResult

WATERMARK_KEY = "last_triage_ts"
AUTO_COMMIT_CONFIDENCE = 0.75   # facts at/above this land in memory without review


def apply_result(memory: MemoryStore, result: TriageResult, date: str, *,
                 auto_commit_confidence: float = AUTO_COMMIT_CONFIDENCE,
                 watermark: str | None = None,
                 window_start: str | None = None) -> dict:
    """Apply a triage result to memory. Confident facts (statements +
    supersessions at/above the threshold) auto-commit straight into memory.
    Low-confidence facts and all questions are DROPPED — the inference engine,
    not fact-triage, is how the system now gets curious about you.

    Clears any legacy pending rows. Returns {"auto_committed": int,
    "dropped": int}. No LLM/network work, so it's fully testable.
    """
    auto = 0
    dropped = 0
    memory.conn.execute("BEGIN IMMEDIATE")
    try:
        memory.clear_pending(commit=False)
        refs = [{"kind": "triage", "date": date,
                 "window_start": window_start, "window_end": watermark}]

        for st in result.statements:
            if st.value and st.confidence >= auto_commit_confidence:
                memory.add(st.category, st.attribute, st.value,
                           confidence=st.confidence, source_refs=refs, commit=False)
                auto += 1
            else:
                dropped += 1

        for sup in result.supersessions:
            old = memory.get(sup.memory_id)
            if old is not None and sup.value and sup.confidence >= auto_commit_confidence:
                memory.supersede(sup.memory_id, sup.value, attribute=sup.attribute,
                                 confidence=sup.confidence, source_refs=refs,
                                 commit=False)
                auto += 1
            else:
                dropped += 1

        dropped += len(result.questions)
        if watermark is not None:
            memory.set_meta(WATERMARK_KEY, watermark, commit=False)
        memory.conn.commit()
    except Exception:
        memory.conn.rollback()
        raise

    return {"auto_committed": auto, "dropped": dropped}


@dataclass
class TriageContext:
    date: str
    window_start: str       # ISO start of the activity window summarized
    window_end: str         # ISO end (the new watermark, in incremental mode)
    raw_summary: str        # before redaction (local only; never sent)
    summary: str            # redacted; this is what went to the model
    active_memories: list   # list[dict]
    result: TriageResult


def run_triage(
    events: EventLog,
    memory: MemoryStore,
    backend,
    date: str,
    *,
    incremental: bool = True,
    redact_fn=redact,
) -> TriageContext:
    """Summarize activity, redact, recall memory, and ask the model.

    incremental=True (default): only summarize activity SINCE the last triage
    (a moving watermark), so heavy days aren't truncated by the per-app cap and
    repeated runs don't overlap. The watermark advances only after a successful
    model call. incremental=False: summarize the whole calendar `date` (used for
    re-triaging a specific past day).
    """
    if incremental:
        start = memory.get_meta(WATERMARK_KEY) or day_bounds(date)[0]
        end = now_iso()
        raw = build_summary(events, start, end,
                            f"Activity since last review (through {end[:16]})")
    else:
        start, end = day_bounds(date)
        raw = build_day_summary(events, date)

    summary = redact_fn(raw)
    active = memory.active_as_dicts()
    declined = memory.recent_rejections()   # soft, capped, recent-only
    result: TriageResult = backend.triage(summary, active, declined)

    return TriageContext(
        date=date,
        window_start=start,
        window_end=end,
        raw_summary=raw,
        summary=summary,
        active_memories=active,
        result=result,
    )
