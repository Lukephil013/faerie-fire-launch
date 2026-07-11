import os
import json
import sqlite3
import tempfile
from dataclasses import asdict
from datetime import date, timedelta

from livingpc.curiosity_metrics import (
    DAILY_XP_CAP, MetricStore, proposed_profile, render_dashboard,
)


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

    def test_unsupported_curiosity_gets_no_automatic_profile(self):
        assert proposed_profile(curiosity("piano", "learn piano")) is None
        assert self.store.ensure_profile(curiosity("piano", "learn piano")) is None

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
        assert snapshot.total_xp == 5
        assert snapshot.level == 1
        assert snapshot.xp_into_level == 5
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
        snapshot = self.store.build_snapshot(1, "2026-07-01")
        assert snapshot.xp_delta == DAILY_XP_CAP
        assert snapshot.total_xp == DAILY_XP_CAP
        assert snapshot.level == 2
        assert snapshot.xp_into_level == 0

    def test_replacing_checkin_does_not_duplicate_xp(self):
        growth = {item.slug: 3 for item in self.profile.dimensions}
        assert self.store.record_checkin(1, {}, growth, checkin_date="2026-07-01")
        assert not self.store.record_checkin(1, {}, growth, checkin_date="2026-07-01")
        snapshot = self.store.build_snapshot(1, "2026-07-01")
        assert snapshot.total_xp == 5

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
