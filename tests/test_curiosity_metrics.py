import os
import json
import sqlite3
import tempfile
from dataclasses import asdict
from datetime import date, timedelta

from livingpc.curiosity_metrics import (
    DAILY_XP_CAP, MetricStore, proposed_profile, render_dashboard,
    level_for_xp, xp_into_level,
)


def test_level_curve_is_easy_until_three_digits_then_harder():
    # Easy: 50 XP per level below level 100.
    assert level_for_xp(0) == 1
    assert level_for_xp(49) == 1
    assert level_for_xp(50) == 2
    assert level_for_xp(570) == 12          # today's real total lands at level 12
    assert xp_into_level(570) == 20
    # Level 100 begins at 99 * 50 = 4950 XP.
    assert level_for_xp(4949) == 99
    assert level_for_xp(4950) == 100
    # Slightly harder past 100: 120 XP per level.
    assert level_for_xp(4950 + 119) == 100
    assert level_for_xp(4950 + 120) == 101


def test_level_up_toast_only_fires_on_milestones_and_never_replays():
    with tempfile.TemporaryDirectory() as tmp:
        store = MetricStore(os.path.join(tmp, "memory.db"))
        fired = []
        store._on_level_up = lambda level: fired.append(level)  # capture toasts
        try:
            # Climb from level 1 well past level 5 in one day (milestone events
            # are 75 XP each; the daily cap is 300 -> level 7).
            for i in range(6):
                store.record_event(0, "milestone", f"m:{i}", occurred_on="2026-07-01")
            # Crossed level 5 exactly once; level 10 not reached -> one toast.
            assert fired == [5]
            # A DB restore/re-run cannot replay it: reopening seeds the
            # high-water mark and awarding more on a fresh day still only fires
            # the NEXT unseen milestone (10), never 5 again.
            store.close()
            store2 = MetricStore(os.path.join(tmp, "memory.db"))
            fired2 = []
            store2._on_level_up = lambda level: fired2.append(level)
            for i in range(10):
                store2.record_event(0, "milestone", f"n:{i}", occurred_on="2026-07-02")
            assert 5 not in fired2          # never replays a passed milestone
            assert fired2 and min(fired2) >= 10
            store2.close()
        finally:
            pass


def curiosity(label="exercise", directive="help me exercise", curiosity_id=1):
    return {"id": curiosity_id, "label": label, "directive": directive}


class TestMetricProfiles:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.store = MetricStore(self.db)

    def teardown_method(self):
        self.store.close()
        self.tmp.cleanup()

    def test_proposes_non_diagnostic_mental_health_profile(self):
        profile = proposed_profile(curiosity("mental health", "support my wellbeing"))
        assert profile.domain == "mental_health"
        assert profile.status == "draft"
        assert {item.slug for item in profile.state_metrics} == {
            "energy", "mood", "focus", "stress"}
        assert "mood" not in {item.slug for item in profile.dimensions}

    def test_exercise_profile_is_draft_until_approved(self):
        draft = self.store.ensure_profile(curiosity())
        assert draft.domain == "exercise"
        assert draft.status == "draft"
        approved = self.store.approve_profile(1)
        assert approved.status == "approved"
        assert abs(sum(item.weight for item in approved.dimensions) - 1.0) < 1e-9

    def test_profile_edit_normalizes_weights(self):
        draft = self.store.ensure_profile(curiosity())
        dimensions = [asdict(item) for item in draft.dimensions[:2]]
        dimensions[0]["weight"] = 3
        dimensions[1]["weight"] = 1
        approved = self.store.approve_profile(1, dimensions=dimensions)
        assert approved.dimensions[0].weight == 0.75
        assert approved.dimensions[1].weight == 0.25

    def test_general_offline_fallback_is_available_for_an_explicit_draft(self):
        profile = proposed_profile(curiosity("piano", "learn piano"))
        assert profile.domain == "general"
        assert self.store.ensure_profile(curiosity("piano", "learn piano")).domain == "general"

    def test_negative_state_prompts_use_positive_ten_is_better_wording(self):
        mental = proposed_profile(curiosity("mental health", "support my wellbeing"))
        exercise = proposed_profile(curiosity())
        stress = next(item for item in mental.state_metrics if item.slug == "stress")
        soreness = next(item for item in exercise.state_metrics if item.slug == "soreness")
        assert "manageable" in stress.checkin_prompt.lower()
        assert "movement-ready" in soreness.checkin_prompt.lower()

    def test_old_profile_schema_migrates_without_losing_row(self):
        legacy = os.path.join(self.tmp.name, "legacy.db")
        conn = sqlite3.connect(legacy)
        conn.execute("""CREATE TABLE curiosity_metric_profile (
            curiosity_id INTEGER PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL,
            domain TEXT NOT NULL, dimensions_json TEXT NOT NULL, state_json TEXT NOT NULL,
            created_at TEXT NOT NULL, approved_at TEXT)""")
        profile = proposed_profile(curiosity())
        conn.execute("INSERT INTO curiosity_metric_profile VALUES (?,?,?,?,?,?,?,?)", (
            1, 1, "draft", "exercise",
            json.dumps([asdict(item) for item in profile.dimensions]),
            json.dumps([asdict(item) for item in profile.state_metrics]),
            "2026-07-01T00:00:00+00:00", None))
        conn.commit(); conn.close()
        migrated = MetricStore(legacy)
        try:
            loaded = migrated.get_profile(1)
            assert loaded.domain == "exercise"
            assert loaded.publication_status == "private"
        finally:
            migrated.close()


class TestMetricScoring:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "memory.db")
        self.store = MetricStore(self.db)
        self.profile = self.store.ensure_profile(curiosity())
        self.profile = self.store.approve_profile(1)

    def teardown_method(self):
        self.store.close()
        self.tmp.cleanup()

    def test_checkin_initializes_mastery_and_xp(self):
        created = self.store.record_checkin(
            1, {"energy": 7.5, "soreness": 2.5, "recovery": 5},
            {item.slug: 10 for item in self.profile.dimensions},
            "private note", checkin_date="2026-07-01")
        assert created
        snapshot = self.store.build_snapshot(1, "2026-07-01")
        assert snapshot.overall_mastery == 100
        assert snapshot.total_xp == 12
        assert snapshot.level == 1
        assert snapshot.xp_into_level == 12
        assert snapshot.state["energy"] == 75
        assert "private note" not in snapshot.summary

    def test_weaker_evidence_moves_mastery_down_gently(self):
        growth = {item.slug: 10 for item in self.profile.dimensions}
        self.store.record_checkin(1, {}, growth, checkin_date="2026-07-01")
        first = self.store.build_snapshot(1, "2026-07-01")
        self.store.record_checkin(
            1, {}, {item.slug: 0 for item in self.profile.dimensions},
            checkin_date="2026-07-02")
        second = self.store.build_snapshot(1, "2026-07-02")
        assert first.overall_mastery == 100
        assert second.overall_mastery == 80
        assert second.total_xp > first.total_xp

    def test_missing_day_is_not_a_zero_or_penalty(self):
        growth = {item.slug: 4 for item in self.profile.dimensions}
        self.store.record_checkin(1, {}, growth, checkin_date="2026-07-01")
        first = self.store.build_snapshot(1, "2026-07-01")
        missing = self.store.build_snapshot(1, "2026-07-02")
        assert missing.overall_mastery == first.overall_mastery
        assert missing.xp_delta == 0

    def test_confidence_ages_while_mastery_stays_stable(self):
        growth = {item.slug: 4 for item in self.profile.dimensions}
        self.store.record_checkin(1, {}, growth, checkin_date="2026-07-01")
        first = self.store.build_snapshot(1, "2026-07-01")
        aging = self.store.build_snapshot(1, "2026-07-15")
        old = self.store.build_snapshot(1, "2026-07-30")
        assert first.overall_confidence > 0
        assert 0 < aging.overall_confidence < first.overall_confidence
        assert old.overall_confidence == 0
        assert old.overall_mastery == first.overall_mastery

    def test_event_deduplication_and_daily_xp_cap(self):
        assert self.store.record_event(
            1, "milestone", "milestone:a", occurred_on="2026-07-01")
        assert not self.store.record_event(
            1, "milestone", "milestone:a", occurred_on="2026-07-01")
        self.store.record_event(1, "milestone", "milestone:b", occurred_on="2026-07-01")
        self.store.record_event(1, "milestone", "milestone:c", occurred_on="2026-07-01")
        # Add enough to exceed generous new cap and prove capping applies
        self.store.record_event(1, "milestone", "milestone:d", occurred_on="2026-07-01")
        self.store.record_event(1, "milestone", "milestone:e", occurred_on="2026-07-01")
        snapshot = self.store.build_snapshot(1, "2026-07-01")
        assert snapshot.xp_delta == DAILY_XP_CAP
        assert snapshot.total_xp == DAILY_XP_CAP
        # Easy curve: 50 XP/level below level 100, so 300 XP == level 7.
        assert snapshot.level == 7
        assert snapshot.xp_into_level == 0

    def test_global_xp_has_one_cap_and_survives_other_investigations(self):
        self.store.record_event(1, "milestone", "one", occurred_on="2026-07-01")
        self.store.record_event(2, "milestone", "two", occurred_on="2026-07-01")
        self.store.record_event(2, "milestone", "three", occurred_on="2026-07-01")
        # Extra to exceed the (now generous) single daily global cap
        self.store.record_event(1, "milestone", "four", occurred_on="2026-07-01")
        self.store.record_event(2, "milestone", "five", occurred_on="2026-07-01")
        self.store.record_event(2, "milestone", "six", occurred_on="2026-07-01")
        assert self.store.global_xp("2026-07-01") == DAILY_XP_CAP

    def test_replacing_checkin_does_not_duplicate_xp(self):
        growth = {item.slug: 3 for item in self.profile.dimensions}
        assert self.store.record_checkin(1, {}, growth, checkin_date="2026-07-01")
        assert not self.store.record_checkin(1, {}, growth, checkin_date="2026-07-01")
        snapshot = self.store.build_snapshot(1, "2026-07-01")
        assert snapshot.total_xp == 12

    def test_trend_compares_two_seven_day_windows(self):
        start = date(2026, 7, 1)
        for offset in range(14):
            rating = 2 if offset < 7 else 5
            day = (start + timedelta(days=offset)).isoformat()
            self.store.record_checkin(
                1, {}, {item.slug: rating for item in self.profile.dimensions},
                checkin_date=day)
            snapshot = self.store.build_snapshot(1, day)
        assert snapshot.trend_7d is not None
        assert snapshot.trend_7d > 0

    def test_missing_day_snapshots_are_excluded_from_trend_windows(self):
        for day, rating in (("2026-07-01", 2), ("2026-07-03", 2),
                            ("2026-07-08", 8), ("2026-07-14", 8)):
            self.store.record_checkin(
                1, {}, {item.slug: rating for item in self.profile.dimensions},
                checkin_date=day)
            snapshot = self.store.build_snapshot(1, day)
        for day in ("2026-07-09", "2026-07-10", "2026-07-11"):
            self.store.build_snapshot(1, day)
        snapshot = self.store.build_snapshot(1, "2026-07-14")
        assert snapshot.trend_7d is not None
        assert snapshot.trend_7d > 0

    def test_publication_requires_seven_distinct_checkin_days(self):
        assert self.store.get_profile(1).publication_status == "private"
        for offset in range(7):
            day = (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
            self.store.record_checkin(1, {}, {"consistency": 5}, checkin_date=day)
            self.store.build_snapshot(1, day)
        assert self.store.get_profile(1).publication_status == "ready"
        published = self.store.approve_publication(1)
        assert published.publication_status == "published"
        assert published.publication_approved_at
        assert self.store.revoke_publication(1).publication_status == "ready"

    def test_chart_is_deterministic_and_contains_no_note_text(self):
        growth = {item.slug: 4 for item in self.profile.dimensions}
        secret = "raw-private-checkin-note-123"
        self.store.record_checkin(1, {"energy": 3}, growth, secret,
                                  checkin_date="2026-07-01")
        snapshot = self.store.build_snapshot(1, "2026-07-01")
        path_a, digest_a = render_dashboard(
            self.profile, snapshot, self.store.history(1), self.tmp.name)
        path_b, digest_b = render_dashboard(
            self.profile, snapshot, self.store.history(1), self.tmp.name)
        assert path_a == path_b
        assert digest_a == digest_b
        with open(path_a, "rb") as stream:
            assert secret.encode() not in stream.read()
