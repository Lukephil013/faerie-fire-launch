"""Phase E: cadence decision, reflection selection, and the companion hook."""
import os
import sys
import tempfile
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.pop("LIVINGPC_DB_KEY", None)

from livingpc.config import Config  # noqa: E402
from livingpc.inference import InferenceStore, CORE_BELIEF_CONFIRMATIONS  # noqa: E402
from livingpc.inference_scheduler import checkin_prompt_due, curiosity_due, due  # noqa: E402
from livingpc.companion.brain import Companion, StubChat  # noqa: E402

HOUR = 3
INTERVAL = 25 * 60


def _at(h, m=0):
    return datetime(2026, 7, 1, h, m, 0, tzinfo=timezone.utc)


def test_due_fires_nightly_once_per_day():
    # at/after the nightly hour, not yet run today -> nightly
    assert due(_at(3, 5), None, None, interval_seconds=INTERVAL, nightly_hour=HOUR) == "nightly"
    # already ran nightly today -> not nightly again (falls through to loop rules)
    assert due(_at(3, 5), _at(3, 4), "2026-07-01",
               interval_seconds=INTERVAL, nightly_hour=HOUR) is None


def test_due_does_not_poll_again_after_daily_claim():
    now = _at(12, 0)
    assert due(now, None, "2026-07-01", interval_seconds=INTERVAL,
               nightly_hour=HOUR) is None
    assert due(now, now - timedelta(days=2), "2026-07-01",
               interval_seconds=INTERVAL, nightly_hour=HOUR) is None


def test_before_daily_hour_does_nothing():
    assert due(_at(1, 0), None, None,
               interval_seconds=INTERVAL, nightly_hour=HOUR) is None


def test_curiosity_due_fires_first_time_then_waits_out_its_own_cadence():
    now = _at(12, 0)
    # much longer interval than the inference loop's — independent cadence
    long_interval = 12 * 60 * 60
    assert curiosity_due(now, None, interval_seconds=long_interval) is True
    assert curiosity_due(now, now - timedelta(hours=1),
                         interval_seconds=long_interval) is False
    assert curiosity_due(now, now - timedelta(hours=13),
                         interval_seconds=long_interval) is True


def test_checkin_prompt_due_once_per_local_day():
    now = _at(21, 15)
    assert checkin_prompt_due(now, None, hour=21)
    assert not checkin_prompt_due(now, "2026-07-01", hour=21)
    assert not checkin_prompt_due(_at(20, 59), None, hour=21)


def test_nightly_metric_snapshot_is_idempotent():
    from livingpc.curiosity import CuriosityStore
    from livingpc.curiosity_metrics import MetricStore
    from livingpc.inference_scheduler import InferenceScheduler

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        curiosities = CuriosityStore(cfg.memory_db_path)
        cid = curiosities.add_curiosity("build a steady exercise habit", "Exercise")
        row = next(c for c in curiosities.list_curiosities() if c["id"] == cid)
        metrics = MetricStore(cfg.memory_db_path)
        try:
            profile = metrics.ensure_profile(row)
            metrics.approve_profile(
                cid, dimensions=profile.dimensions, state_metrics=profile.state_metrics)
            metrics.record_checkin(
                cid, {"energy": 4}, {"consistency": 4}, checkin_date="2026-07-01")
        finally:
            metrics.close()
            curiosities.close()

        scheduler = InferenceScheduler(cfg)
        now = datetime(2026, 7, 2, 3, tzinfo=timezone.utc)
        assert scheduler._snapshot_curiosity_metrics(now)
        assert scheduler._snapshot_curiosity_metrics(now)

        metrics = MetricStore(cfg.memory_db_path)
        try:
            assert len(metrics.history(cid, limit=30)) == 1
        finally:
            metrics.close()


def test_nightly_resync_runs_immediately_after_metric_snapshot():
    from livingpc.inference_scheduler import InferenceScheduler

    class Result:
        created = 0

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        scheduler = InferenceScheduler(cfg)
        order = []
        scheduler._run_triage_nightly = lambda: order.append("triage") or True
        scheduler._snapshot_curiosity_metrics = lambda: order.append("snapshot") or True
        scheduler._resync_published_metric_dashboards = lambda: order.append("resync") or True
        scheduler._consolidate_memory = lambda: True
        scheduler._backup_memory = lambda: True
        scheduler._notify_reviews = lambda: True
        assert scheduler._run_housekeeping()
        assert order == ["triage", "snapshot", "resync"]


def test_housekeeping_purges_old_blobs_only():
    from livingpc.inference_scheduler import InferenceScheduler
    from livingpc.storage import EventLog

    with tempfile.TemporaryDirectory() as d:
        old_blob = os.path.join(d, "old.png")
        new_blob = os.path.join(d, "new.png")
        with open(old_blob, "wb") as f:
            f.write(b"old")
        with open(new_blob, "wb") as f:
            f.write(b"new")
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"),
                     blob_retention_days=3)
        events = EventLog(cfg.db_path)
        events.log_event(
            "screenshot", blob_ref=old_blob,
            ts=(datetime.now(timezone.utc) - timedelta(days=5)).isoformat())
        events.log_event(
            "screenshot", blob_ref=new_blob,
            ts=datetime.now(timezone.utc).isoformat())
        events.close()

        scheduler = InferenceScheduler(cfg)
        assert scheduler._purge_event_blobs()
        assert not os.path.exists(old_blob)
        assert os.path.exists(new_blob)


def test_zero_blob_retention_purges_existing_blobs_immediately():
    from livingpc.inference_scheduler import InferenceScheduler
    from livingpc.storage import EventLog

    with tempfile.TemporaryDirectory() as d:
        blob = os.path.join(d, "now.png")
        with open(blob, "wb") as f:
            f.write(b"now")
        cfg = Config(db_path=os.path.join(d, "e.db"),
                     memory_db_path=os.path.join(d, "m.db"), blob_retention_days=0)
        events = EventLog(cfg.db_path)
        events.log_event("screenshot", blob_ref=blob, ts=datetime.now(timezone.utc).isoformat())
        events.close()

        assert InferenceScheduler(cfg)._purge_event_blobs()
        assert not os.path.exists(blob)


def test_resync_active_curiosities_to_notion_covers_every_active_curiosity():
    """The 12h periodic pass is meant as a catch-all so a sync that failed or
    was skipped in real time still gets picked up — it should attempt every
    *active* curiosity (not paused/archived ones), regardless of whether this
    round produced new items."""
    import livingpc.notion_sync as notion_sync
    from livingpc.inference_scheduler import InferenceScheduler
    from livingpc.curiosity import CuriosityStore

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        store = CuriosityStore(cfg.memory_db_path)
        active_a = store.add_curiosity("get fit", "fitness")
        active_b = store.add_curiosity("learn piano", "piano")
        paused = store.add_curiosity("read more", "reading")
        store.set_status(paused, "paused")
        store.close()

        calls = []
        original = notion_sync.sync_curiosity_to_notion
        notion_sync.sync_curiosity_to_notion = (
            lambda config, mem, inf, st, cid, model, **kw: calls.append(cid))
        try:
            scheduler = InferenceScheduler(cfg)
            store = CuriosityStore(cfg.memory_db_path)
            try:
                assert scheduler._resync_active_curiosities_to_notion(
                    None, None, store, None) is True
            finally:
                store.close()
        finally:
            notion_sync.sync_curiosity_to_notion = original

        assert sorted(calls) == sorted([active_a, active_b])
        assert paused not in calls


def test_resync_active_curiosities_to_notion_survives_one_failing():
    """One curiosity's sync raising must not stop the others from syncing —
    best-effort, wrapped per curiosity."""
    import livingpc.notion_sync as notion_sync
    from livingpc.inference_scheduler import InferenceScheduler
    from livingpc.curiosity import CuriosityStore

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        store = CuriosityStore(cfg.memory_db_path)
        boom_id = store.add_curiosity("get fit", "fitness")
        ok_id = store.add_curiosity("learn piano", "piano")
        store.close()

        calls = []

        def _flaky(config, mem, inf, st, cid, model, **kw):
            if cid == boom_id:
                raise RuntimeError("network down")
            calls.append(cid)

        original = notion_sync.sync_curiosity_to_notion
        notion_sync.sync_curiosity_to_notion = _flaky
        try:
            scheduler = InferenceScheduler(cfg)
            store = CuriosityStore(cfg.memory_db_path)
            try:
                assert scheduler._resync_active_curiosities_to_notion(
                    None, None, store, None) is False
            finally:
                store.close()
        finally:
            notion_sync.sync_curiosity_to_notion = original

        assert calls == [ok_id]


def test_failed_inference_run_reports_failure_for_immediate_retry():
    from livingpc.inference_scheduler import InferenceScheduler

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        scheduler = InferenceScheduler(cfg)
        with patch("livingpc.inference_loop.get_model", return_value=object()), \
             patch("livingpc.inference_loop.run_inference", side_effect=RuntimeError("offline")):
            assert scheduler._run_once("loop") is False


def test_daily_cycle_is_claimed_before_background_work():
    from livingpc.inference_scheduler import (
        InferenceScheduler, LAST_CURIOSITY_ATTEMPT_KEY,
        LAST_GOAL_AI_ATTEMPT_KEY, LAST_LOOP_ATTEMPT_KEY,
    )

    class OnePoll:
        stopped = False
        def is_set(self):
            return self.stopped
        def wait(self, _seconds):
            self.stopped = True

    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        cfg.curiosity_metrics_enabled = False
        scheduler = InferenceScheduler(cfg)
        scheduler.nightly_hour = 0
        scheduler._get_nightly_date = lambda: "2026-07-05"
        calls = []
        scheduler._run_daily_cycle = lambda now: calls.append("daily") or False
        scheduler.run(OnePoll())
        assert calls == ["daily"]
        assert scheduler._last_loop is not None
        assert scheduler._last_curiosity is not None
        assert scheduler._last_goal_ai is not None
        assert scheduler._get_success_time(LAST_LOOP_ATTEMPT_KEY) is not None
        assert scheduler._get_success_time(LAST_CURIOSITY_ATTEMPT_KEY) is not None
        assert scheduler._get_success_time(LAST_GOAL_AI_ATTEMPT_KEY) is not None


def test_daily_cycle_orders_inference_curiosity_goal_ai_then_housekeeping():
    from livingpc.inference_scheduler import InferenceScheduler
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        scheduler = InferenceScheduler(cfg)
        order = []
        scheduler._run_once = lambda kind: order.append("inference") or True
        scheduler._run_curiosity_once = lambda: order.append("curiosity") or True
        scheduler._run_goal_ai_once = lambda: order.append("goal-ai") or True
        scheduler._run_housekeeping = lambda: order.append("housekeeping") or True
        assert scheduler._run_daily_cycle(_at(20))
        assert order == ["inference", "curiosity", "goal-ai", "housekeeping"]


def test_disabled_notion_sync_is_not_a_curiosity_failure():
    from livingpc.inference_scheduler import InferenceScheduler
    with tempfile.TemporaryDirectory() as d:
        cfg = Config(db_path=os.path.join(d, "e.db"), memory_db_path=os.path.join(d, "m.db"))
        cfg.notion_sync_enabled = False
        scheduler = InferenceScheduler(cfg)
        assert scheduler._resync_active_curiosities_to_notion(None, None, None, None)


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn(); print("PASS " + fn.__name__)
        except Exception:
            fails += 1; print("FAIL " + fn.__name__); traceback.print_exc()
    print("\n%d/%d passed" % (len(fns) - fails, len(fns)))
    sys.exit(1 if fails else 0)
