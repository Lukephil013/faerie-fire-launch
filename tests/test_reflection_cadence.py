"""Phase 6: one shared, consent-aware reflection rhythm."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from livingpc.reflection_cadence import ReflectionCadenceStore, in_quiet_hours


def at(day=1, hour=12):
    return datetime(2026, 1, day, hour, tzinfo=timezone.utc)


def store():
    tmp = tempfile.TemporaryDirectory()
    return tmp, ReflectionCadenceStore(os.path.join(tmp.name, "memory.db"))


def test_quiet_hours_span_midnight_and_daytime_window():
    assert in_quiet_hours(at(hour=22), 21, 8)
    assert in_quiet_hours(at(hour=7), 21, 8)
    assert not in_quiet_hours(at(hour=12), 21, 8)
    assert in_quiet_hours(at(hour=12), 10, 14)


def test_global_weekly_cap_applies_across_every_reflection_kind():
    tmp, cadence = store()
    try:
        cadence.offer("goal_update", "goal:1", "quiet_goal", now=at())
        assert cadence.claim_next(now=at(), min_days=7, quiet_start_hour=21,
                                  quiet_end_hour=8)["kind"] == "goal_update"
        cadence.offer("inference_review", "claim:2", "contradiction", priority=99,
                      now=at(2))
        assert cadence.claim_next(now=at(2), min_days=7, quiet_start_hour=21,
                                  quiet_end_hour=8) is None
        assert cadence.claim_next(now=at(8), min_days=7, quiet_start_hour=21,
                                  quiet_end_hour=8)["kind"] == "inference_review"
    finally:
        cadence.close(); tmp.cleanup()


def test_priority_backlog_and_deduplication_prevent_anxiety_queue():
    tmp, cadence = store()
    try:
        first = cadence.offer("goal_update", "goal:1", "quiet", now=at(), backlog_limit=2)
        again = cadence.offer("goal_update", "goal:1", "new_evidence", priority=90,
                              now=at(), backlog_limit=2)
        second = cadence.offer("investigation_checkin", "inv:2", "answer_due",
                               now=at(), backlog_limit=2)
        blocked = cadence.offer("inference_review", "claim:3", "new_evidence",
                                now=at(), backlog_limit=2)
        assert first["event_id"] == again["event_id"]
        assert second["accepted"] and blocked["reason"] == "backlog"
        assert cadence.claim_next(now=at(), min_days=0, quiet_start_hour=21,
                                  quiet_end_hour=8)["subject_key"] == "goal:1"
    finally:
        cadence.close(); tmp.cleanup()


def test_snooze_escalates_and_ignore_suppresses_repeated_subject():
    tmp, cadence = store()
    try:
        event = cadence.offer("goal_update", "goal:1", "quiet", now=at())
        cadence.claim_next(now=at(), min_days=0, quiet_start_hour=21, quiet_end_hour=8)
        one = cadence.feedback(event["event_id"], "snooze", now=at(), snooze_base_days=3)
        assert datetime.fromisoformat(one["eligible_at"]) == at() + timedelta(days=3)
        cadence.claim_next(now=at(4), min_days=0, quiet_start_hour=21, quiet_end_hour=8)
        two = cadence.feedback(event["event_id"], "snooze", now=at(4), snooze_base_days=3)
        assert datetime.fromisoformat(two["eligible_at"]) == at(10)
        cadence.claim_next(now=at(10), min_days=0, quiet_start_hour=21, quiet_end_hour=8)
        ignored = cadence.feedback(event["event_id"], "ignore", now=at(10),
                                   ignore_suppress_days=30, usefulness=1, burden=5)
        assert datetime.fromisoformat(ignored["suppress_until"]) == at(10) + timedelta(days=30)
        assert cadence.offer("goal_update", "goal:1", "quiet", now=at(20))["reason"] == "suppressed"
    finally:
        cadence.close(); tmp.cleanup()


def test_never_is_durable_and_feedback_keeps_only_local_metadata():
    tmp, cadence = store()
    try:
        event = cadence.offer("investigation_checkin", "sensitive-topic-hash", "permission",
                              now=at())
        cadence.claim_next(now=at(), min_days=0, quiet_start_hour=21, quiet_end_hour=8)
        cadence.feedback(event["event_id"], "never", now=at(), usefulness=1, burden=5)
        assert cadence.offer("investigation_checkin", "sensitive-topic-hash", "new_evidence",
                             now=at(31))["reason"] == "never"
        raw = str(cadence.snapshot())
        assert "prompt body" not in raw and "private answer" not in raw
    finally:
        cadence.close(); tmp.cleanup()
