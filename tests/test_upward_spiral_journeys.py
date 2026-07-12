"""Synthetic multi-month journeys for the Phase 6 upward-spiral contracts."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from livingpc.curiosity import CuriosityStore
from livingpc.reflection_cadence import ReflectionCadenceStore


BASE = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def candidate(topic, *, sensitivity="normal"):
    return {"title": "Explore a pattern", "question": "What changes this pattern?",
            "rationale": "Recent evidence may make this useful.",
            "relevance": .9, "uncertainty": .8,
            "expected_usefulness": .9, "burden": "low", "sensitivity": sensitivity,
            "topic_key": topic, "evidence_refs": ["synthetic:1"], "goal_node_ids": []}


def test_sensitive_rejection_stays_rejected_across_a_simulated_year():
    with tempfile.TemporaryDirectory() as directory:
        investigations = CuriosityStore(os.path.join(directory, "memory.db"))
        try:
            item = investigations.add_candidate(candidate("fear-family", sensitivity="sensitive"))
            investigations.decide_candidate(item["id"], "never_ask")
            assert investigations.add_candidate(candidate("fear-family", sensitivity="sensitive")) is None
            assert investigations.candidate_topic_blocked("fear-family")
        finally:
            investigations.close()


def test_changed_dream_can_be_prompted_later_without_monthly_noise():
    with tempfile.TemporaryDirectory() as directory:
        cadence = ReflectionCadenceStore(os.path.join(directory, "memory.db"))
        try:
            shown = []
            for month in range(6):
                now = BASE + timedelta(days=30 * month)
                cadence.offer("goal_update", "dream:music", "quiet_goal", now=now)
                event = cadence.claim_next(now=now, min_days=7, quiet_start_hour=21,
                                           quiet_end_hour=8)
                if event:
                    shown.append(event)
                    cadence.feedback(event["id"], "acted", now=now, usefulness=4, burden=1)
            assert len(shown) == 6
            assert all(event["subject_key"] == "dream:music" for event in shown)
            assert all(event["usefulness"] is None for event in shown)  # feedback is separate from interpretation
        finally:
            cadence.close()


def test_contradiction_wins_priority_but_does_not_break_weekly_consent_cap():
    with tempfile.TemporaryDirectory() as directory:
        cadence = ReflectionCadenceStore(os.path.join(directory, "memory.db"))
        try:
            cadence.offer("investigation_checkin", "inv:old-fear", "time_fallback",
                          priority=10, now=BASE)
            cadence.offer("inference_review", "claim:mistaken-fear", "contradiction",
                          priority=100, now=BASE)
            first = cadence.claim_next(now=BASE, min_days=7, quiet_start_hour=21,
                                       quiet_end_hour=8)
            assert first["trigger_kind"] == "contradiction"
            assert cadence.claim_next(now=BASE + timedelta(days=1), min_days=7,
                                      quiet_start_hour=21, quiet_end_hour=8) is None
            second = cadence.claim_next(now=BASE + timedelta(days=7), min_days=7,
                                        quiet_start_hour=21, quiet_end_hour=8)
            assert second["trigger_kind"] == "time_fallback"
        finally:
            cadence.close()


def test_successful_exception_can_change_the_next_question_not_erase_history():
    with tempfile.TemporaryDirectory() as directory:
        investigations = CuriosityStore(os.path.join(directory, "memory.db"))
        try:
            cid = investigations.add_curiosity("Understand avoidance", "Fear and action")
            old = investigations.add_synthesis(cid, {
                "interpretation": "Avoidance may rise whenever uncertainty appears.",
                "confidence": .7, "evidence": [{"ref": "answer:1", "summary": "Often avoided"}],
                "counterevidence": [], "unknowns": ["Exceptions"], "experiments": []})
            investigations.decide_synthesis(old["id"], "approve")
            new = investigations.add_synthesis(cid, {
                "interpretation": "Supportive company may create a useful exception.",
                "confidence": .55, "evidence": [{"ref": "outcome:2", "summary": "Acted with support"}],
                "counterevidence": [{"ref": "synthesis:1", "summary": "Not universal"}],
                "unknowns": ["Which kinds of support?"], "experiments": []})
            history = investigations.synthesis_history(cid)
            assert new["version"] == 2 and len(history) == 2
            assert any(row["status"] == "approved" for row in history)
            assert any(row["status"] == "draft" for row in history)
        finally:
            investigations.close()
