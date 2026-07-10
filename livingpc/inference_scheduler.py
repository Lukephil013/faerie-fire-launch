"""Cost-bounded daily AI cycle.

Runs inference, curiosities, changed GoalAI paths, and housekeeping once per
local day. Designed to live in the background daemon (tray):
start `InferenceScheduler(cfg).run(stop_event)` on its own thread.

The scheduling DECISION is a pure function (`due`) so it's testable without
threads or clocks; the thread runner is a thin loop around it. Each run opens its
own DB connections (via `run_inference`), so nothing is shared across threads.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from .diagnostics import log_diag
from .storage import EventLog

NIGHTLY_DATE_KEY = "inference_nightly_date"
LAST_LOOP_KEY = "inference_last_success"
LAST_CURIOSITY_KEY = "curiosity_last_success"
LAST_GOAL_AI_KEY = "goal_ai_last_success"
LAST_LOOP_ATTEMPT_KEY = "inference_last_attempt"
LAST_CURIOSITY_ATTEMPT_KEY = "curiosity_last_attempt"
LAST_GOAL_AI_ATTEMPT_KEY = "goal_ai_last_attempt"
CHECKIN_DATE_KEY = "curiosity_checkin_prompt_date"


def due(now: datetime, last_loop: datetime | None, last_nightly_date: str | None,
        *, interval_seconds: float, nightly_hour: int) -> str | None:
    """Return ``nightly`` exactly once per local day at/after the daily hour.

    The legacy arguments remain for callers/config compatibility; elapsed time
    alone never triggers inference anymore.
    """
    today = now.date().isoformat()
    if now.hour >= nightly_hour and last_nightly_date != today:
        return "nightly"
    return None


def curiosity_due(now: datetime, last_curiosity: datetime | None, *,
                  interval_seconds: float) -> bool:
    """Curiosity runs on its own, much longer cadence — independent of the
    inference loop/nightly split, since it's a background pass over whatever
    curiosities are currently active, not tied to fresh evidence."""
    if last_curiosity is None:
        return True
    return (now - last_curiosity).total_seconds() >= interval_seconds


def goal_ai_due(now: datetime, last_run: datetime | None, *,
                interval_seconds: float) -> bool:
    if last_run is None:
        return True
    return (now - last_run).total_seconds() >= interval_seconds


def checkin_prompt_due(now: datetime, last_prompt_date: str | None, *,
                       hour: int) -> bool:
    """Return True once per local day at or after the configured hour."""
    return now.hour >= hour and last_prompt_date != now.date().isoformat()


class InferenceScheduler:
    def __init__(self, cfg, *, log=None):
        self.cfg = cfg
        self.log = log or (lambda *a: None)
        self.interval_seconds = float(
            getattr(cfg, "inference_interval_minutes", 25.0)) * 60.0
        self.nightly_hour = int(getattr(cfg, "inference_nightly_hour", 3))
        self.poll_seconds = float(getattr(cfg, "inference_poll_seconds", 30.0))
        self._last_loop: datetime | None = None
        self.curiosity_interval_seconds = float(
            getattr(cfg, "curiosity_interval_minutes", 720.0)) * 60.0
        self._last_curiosity: datetime | None = None
        self.goal_ai_interval_seconds = float(
            getattr(cfg, "goal_ai_interval_minutes", 240.0)) * 60.0
        self._last_goal_ai: datetime | None = None
        self.checkin_hour = int(getattr(cfg, "curiosity_checkin_hour", 21))

    # persistence for the once-a-day guard (survives restarts) --------------
    def _get_nightly_date(self) -> str | None:
        try:
            ev = EventLog(self.cfg.db_path)
            try:
                return ev.get_meta(NIGHTLY_DATE_KEY)
            finally:
                ev.close()
        except Exception:
            return None

    def _set_nightly_date(self, date_str: str) -> None:
        try:
            ev = EventLog(self.cfg.db_path)
            try:
                ev.set_meta(NIGHTLY_DATE_KEY, date_str)
            finally:
                ev.close()
        except Exception:
            pass

    def _get_success_time(self, key: str) -> datetime | None:
        try:
            ev = EventLog(self.cfg.db_path)
            try:
                value = ev.get_meta(key)
            finally:
                ev.close()
            return datetime.fromisoformat(value) if value else None
        except (TypeError, ValueError, OSError):
            return None

    def _set_success_time(self, key: str, value: datetime) -> None:
        try:
            ev = EventLog(self.cfg.db_path)
            try:
                ev.set_meta(key, value.isoformat())
            finally:
                ev.close()
        except Exception:
            pass

    def _get_checkin_date(self) -> str | None:
        try:
            ev = EventLog(self.cfg.db_path)
            try:
                return ev.get_meta(CHECKIN_DATE_KEY)
            finally:
                ev.close()
        except Exception:
            return None

    def _set_checkin_date(self, date_str: str) -> None:
        try:
            ev = EventLog(self.cfg.db_path)
            try:
                ev.set_meta(CHECKIN_DATE_KEY, date_str)
            finally:
                ev.close()
        except Exception:
            pass

    def _run_once(self, kind: str) -> bool:
        from .inference_loop import run_inference, get_model
        succeeded = True
        try:
            result = run_inference(
                self.cfg,
                observer_model=get_model(self.cfg, nightly=False),
                synthesis_model=get_model(self.cfg, nightly=True),
            )
            log_diag("inference", f"scheduled {kind} run created={result.created}")
            self.log(f"[inference] {kind} run: {result.created} new hypothesis(es)")
            if result.created and getattr(self.cfg, "notify_on_graduation", True):
                from .notify import notify
                noun = "hypothesis" if result.created == 1 else "hypotheses"
                notify(f"{result.created} new {noun} about you",
                       "A claim crossed the confidence gate — review it in the "
                       "Memory GUI when you have a minute.", cfg=self.cfg)
        except Exception as error:
            succeeded = False
            log_diag("inference", f"scheduled {kind} run failed "
                     f"error={type(error).__name__}: {error}")
            self.log(f"[inference] {kind} run failed: {type(error).__name__}")
        return succeeded

    def _run_housekeeping(self) -> bool:
        succeeded = True
        for operation in (
            self._run_triage_nightly,
            self._snapshot_curiosity_metrics,
            self._resync_published_metric_dashboards,
            self._purge_event_blobs,
            self._consolidate_memory,
            self._backup_memory,
            self._notify_reviews,
        ):
            succeeded = operation() and succeeded
        return succeeded

    def _purge_event_blobs(self) -> bool:
        """Nightly screenshot hygiene.

        Event rows and text payloads stay; only old screenshot blob files are
        unlinked and their blob_ref columns nulled. This keeps capture useful
        without letting the blob folder grow forever.
        """
        retention_days = int(getattr(self.cfg, "blob_retention_days", 3) or 0)
        if retention_days <= 0:
            log_diag("inference", "blob purge skipped retention_days=0")
            return True
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        store = EventLog(self.cfg.db_path)
        try:
            purged = store.purge_blobs(before_ts=cutoff.isoformat())
        finally:
            store.close()
        log_diag("inference", f"blob purge retention_days={retention_days} purged={purged}")
        if purged:
            self.log(f"[housekeeping] purged {purged} old screenshot blob(s)")
        return True

    def _run_daily_cycle(self, now: datetime) -> bool:
        """Run independent claimed stages in quality-preserving dependency order."""
        inference_ok = self._run_once("daily")
        if inference_ok:
            self._set_success_time(LAST_LOOP_KEY, now)
        curiosity_ok = self._run_curiosity_once()
        if curiosity_ok:
            self._set_success_time(LAST_CURIOSITY_KEY, now)
        goal_ok = True
        if getattr(self.cfg, "goal_ai_enabled", True):
            goal_ok = self._run_goal_ai_once()
            if goal_ok:
                self._set_success_time(LAST_GOAL_AI_KEY, now)
        housekeeping_ok = self._run_housekeeping()
        return inference_ok and curiosity_ok and goal_ok and housekeeping_ok

    def _snapshot_curiosity_metrics(self, now: datetime | None = None) -> bool:
        """Finalize the previous local day. Repeated runs replace one snapshot."""
        if not getattr(self.cfg, "curiosity_metrics_enabled", True):
            return True
        try:
            from .curiosity import CuriosityStore
            from .curiosity_metrics import MetricStore
            local_now = now or datetime.now().astimezone()
            day = (local_now.date() - timedelta(days=1)).isoformat()
            curiosities = CuriosityStore(self.cfg.memory_db_path)
            metrics = MetricStore(self.cfg.memory_db_path)
            finalized = 0
            try:
                for row in curiosities.list_curiosities(status="active"):
                    profile = metrics.ensure_profile(row)
                    if profile and profile.status == "approved":
                        metrics.build_snapshot(row["id"], day)
                        finalized += 1
            finally:
                metrics.close()
                curiosities.close()
            log_diag("curiosity", f"metric snapshots finalized={finalized}")
            return True
        except Exception as error:
            log_diag("curiosity", "metric snapshot failed "
                     f"error={type(error).__name__}")
            self.log(f"[curiosity] metric snapshot failed: {type(error).__name__}")
            return False

    def _prompt_for_curiosity_checkin(self) -> bool:
        """Send a generic reminder; curiosity labels and values stay private."""
        try:
            from .notify import notify
            notify("Evening curiosity check-in",
                   "A short check-in is ready in Faerie Fire.", cfg=self.cfg)
            return True
        except Exception as error:
            log_diag("curiosity", "check-in reminder failed "
                     f"error={type(error).__name__}")
            return False

    def _resync_published_metric_dashboards(self) -> bool:
        """Immediately mirror finalized snapshots for explicitly published profiles."""
        try:
            from .curiosity import CuriosityStore, get_curiosity_model
            from .curiosity_metrics import MetricStore
            from .inference import InferenceStore
            from .memory import MemoryStore
            from .notion_sync import sync_curiosity_to_notion
            mem = MemoryStore(self.cfg.memory_db_path)
            inf = InferenceStore(self.cfg.memory_db_path)
            store = CuriosityStore(self.cfg.memory_db_path)
            metrics = MetricStore(self.cfg.memory_db_path)
            succeeded = True
            try:
                model = get_curiosity_model(self.cfg, usage_category="curiosity")
                for row in store.list_curiosities(status="active"):
                    profile = metrics.get_profile(row["id"])
                    if not profile or profile.publication_status != "published":
                        continue
                    result = sync_curiosity_to_notion(
                        self.cfg, mem, inf, store, row["id"], model)
                    if not result.get("ok"):
                        succeeded = False
            finally:
                metrics.close(); mem.close(); inf.close(); store.close()
            return succeeded
        except Exception as error:
            log_diag("notion", "nightly metric resync failed "
                     f"error={type(error).__name__}: {error}")
            return False

    def _run_triage_nightly(self) -> bool:
        """Distil the day's activity into confident facts (auto-committed). Runs
        in the daemon so no separate Windows task is needed. Best-effort."""
        if not getattr(self.cfg, "triage_nightly_enabled", True):
            return True
        try:
            from .storage import EventLog
            from .memory import MemoryStore, today
            from .triage.llm import get_backend
            from .triage.pipeline import run_triage, apply_result
            ev = EventLog(self.cfg.db_path)
            mem = MemoryStore(self.cfg.memory_db_path)
            try:
                ctx = run_triage(ev, mem, get_backend(self.cfg), today(), incremental=True)
                counts = apply_result(
                    mem, ctx.result, today(),
                    auto_commit_confidence=getattr(self.cfg, "auto_commit_confidence", 0.75),
                    watermark=ctx.window_end, window_start=ctx.window_start)
                log_diag("inference", f"nightly triage auto={counts['auto_committed']} "
                         f"dropped={counts['dropped']}")
                self.log(f"[triage] auto-committed {counts['auto_committed']} fact(s)")
            finally:
                ev.close()
                mem.close()
            return True
        except Exception as error:
            log_diag("inference", f"nightly triage failed "
                     f"error={type(error).__name__}: {error}")
            self.log(f"[triage] failed: {type(error).__name__}")
            return False

    def _consolidate_memory(self) -> bool:
        """Nightly hygiene: merge duplicate facts, prune stale rejections and
        evidence. Runs before the backup so snapshots capture the tidy state.
        Best-effort — never fatal to the scheduler."""
        if not getattr(self.cfg, "consolidate_enabled", True):
            return True
        try:
            from .consolidate import consolidate
            from .memory import MemoryStore
            mem = MemoryStore(self.cfg.memory_db_path)
            try:
                result = consolidate(
                    mem,
                    similarity=getattr(self.cfg, "consolidate_value_similarity", 0.85),
                    rejection_retention_days=getattr(
                        self.cfg, "consolidate_rejection_retention_days", 90),
                    evidence_retention_days=getattr(
                        self.cfg, "consolidate_evidence_retention_days", 180))
            finally:
                mem.close()
            log_diag("inference", f"consolidated merged={result['merged']} "
                     f"rejections={result['pruned_rejections']} "
                     f"evidence={result['pruned_evidence']}")
            if result["merged"] or result["pruned_rejections"] or result["pruned_evidence"]:
                self.log(f"[consolidate] merged {result['merged']} duplicate(s), "
                         f"pruned {result['pruned_rejections']} rejection(s) + "
                         f"{result['pruned_evidence']} evidence row(s)")
            return True
        except Exception as error:
            log_diag("inference", f"consolidation failed "
                     f"error={type(error).__name__}: {error}")
            self.log(f"[consolidate] failed: {type(error).__name__}")
            return False

    def _backup_memory(self) -> bool:
        """Nightly snapshot of memory.db into the rotating backup set.
        Best-effort — never fatal to the scheduler."""
        if not getattr(self.cfg, "backup_enabled", True):
            return True
        try:
            from .backup import backup_memory
            result = backup_memory(
                self.cfg.memory_db_path,
                getattr(self.cfg, "backup_dir", "") or None,
                keep=getattr(self.cfg, "backup_keep", 14))
            log_diag("inference", f"backup written kept={result['kept']} "
                     f"pruned={result['pruned']}")
            self.log(f"[backup] memory.db snapshot ({result['kept']} kept)")
            try:  # project docs ride along in the same rotating set
                from .backup import default_backup_dir
                from .filing import projects_dir_for, snapshot_projects
                snap = snapshot_projects(
                    projects_dir_for(self.cfg),
                    getattr(self.cfg, "backup_dir", "")
                    or default_backup_dir(self.cfg.memory_db_path),
                    keep=getattr(self.cfg, "backup_keep", 14))
                if snap["path"]:
                    log_diag("inference", f"projects snapshot kept={snap['kept']} "
                             f"pruned={snap['pruned']}")
            except Exception as error:
                log_diag("inference", f"projects snapshot failed "
                         f"error={type(error).__name__}")
            return True
        except Exception as error:
            log_diag("inference", f"backup failed "
                     f"error={type(error).__name__}: {error}")
            self.log(f"[backup] failed: {type(error).__name__}")
            return False

    def _notify_reviews(self) -> bool:
        """Once per nightly pass: a desktop toast if inferences are waiting for
        a Yes/No. Silent when the stack is empty. Best-effort."""
        try:
            from .inference import InferenceStore
            from .notify import notify, review_reminder
            inf = InferenceStore(self.cfg.memory_db_path)
            try:
                gate = getattr(self.cfg, "inference_surface_confidence", 0.80)
                count = len(inf.to_review(min_confidence=gate))
            finally:
                inf.close()
            if count:
                notify(*review_reminder(count), cfg=self.cfg)
                self.log(f"[notify] {count} inference(s) awaiting review")
            log_diag("inference", f"nightly review reminder count={count}")
            return True
        except Exception as error:
            log_diag("inference", f"review reminder failed "
                     f"error={type(error).__name__}: {error}")
            return False

    def _run_curiosity_once(self) -> bool:
        """Periodic curiosity pass: one round of items for every active
        curiosity (the greatest one getting a bigger budget), gated the same
        way any generate_items call is. Best-effort — never fatal.

        Also resyncs every active curiosity's Notion page on this same
        cadence (independent of whether this round produced new items), so a
        sync that failed or was skipped in real time (stale config, a
        transient network error, a suggestion response) gets caught up
        automatically rather than requiring the user to trigger something
        that happens to call the sync helper again."""
        try:
            from .curiosity import CuriosityStore, get_curiosity_model, run_all_active
            from .inference import InferenceStore
            from .memory import MemoryStore
            mem = MemoryStore(self.cfg.memory_db_path)
            inf = InferenceStore(self.cfg.memory_db_path)
            store = CuriosityStore(self.cfg.memory_db_path)
            try:
                model = get_curiosity_model(self.cfg, usage_category="curiosity")
                created = run_all_active(
                    mem, inf, store, model,
                    greatest_limit=int(getattr(self.cfg, "curiosity_scan_limit_greatest", 5)),
                    background_limit=int(getattr(self.cfg, "curiosity_scan_limit_background", 2)),
                    question_min_confidence=float(
                        getattr(self.cfg, "curiosity_question_min_confidence", 0.70)),
                    suggestion_min_confidence=float(
                        getattr(self.cfg, "curiosity_suggestion_min_confidence", 0.80)),
                    max_open=int(getattr(self.cfg, "curiosity_max_open_per_curiosity", 6)))
                sync_ok = self._resync_active_curiosities_to_notion(
                    mem, inf, store, model)
            finally:
                mem.close()
                inf.close()
                store.close()
            log_diag("curiosity", f"scheduled pass created={created}")
            if created:
                self.log(f"[curiosity] {created} new item(s) across active curiosities")
            return sync_ok
        except Exception as error:
            log_diag("curiosity", f"scheduled pass failed "
                     f"error={type(error).__name__}: {error}")
            self.log(f"[curiosity] pass failed: {type(error).__name__}")
            return False

    def _run_goal_ai_once(self) -> bool:
        """Bounded bottom-up GoalAI sweep; failure never stops capture."""
        try:
            from .goal_ai import run_goal_sweep
            result = run_goal_sweep(self.cfg)
            log_diag("goal-ai", f"scheduled sweep reviewed={result['reviewed']} "
                     f"failed={result['failures']} proposals={result['proposals_created']} "
                     f"blocked={result['became_blocked']}")
            self.log(f"[goal-ai] reviewed {result['reviewed']} node(s), "
                     f"{result['proposals_created']} proposal(s)")
            if (getattr(self.cfg, "goal_ai_notifications", True) and
                    (result["proposals_created"] or result["became_blocked"])):
                from .notify import notify
                notify(
                    "GoalAI has an update",
                    f"{result['became_blocked']} newly blocked · "
                    f"{result['proposals_created']} new proposal(s). Open Goals to review.",
                    cfg=self.cfg)
            return result["failures"] == 0
        except Exception as error:
            log_diag("goal-ai", f"scheduled sweep failed error={type(error).__name__}")
            self.log(f"[goal-ai] sweep failed: {type(error).__name__}")
            return False

    def _resync_active_curiosities_to_notion(self, mem, inf, store, model) -> bool:
        """Best-effort: mirror every active curiosity to Notion. Each call is
        independently wrapped so one bad page doesn't block the rest."""
        if not getattr(self.cfg, "notion_sync_enabled", False):
            return True
        try:
            from .notion_sync import sync_curiosity_to_notion
        except Exception as error:
            log_diag("notion", f"scheduled resync unavailable "
                     f"error={type(error).__name__}: {error}")
            return False
        succeeded = True
        for row in store.list_curiosities(status="active"):
            try:
                result = sync_curiosity_to_notion(
                    self.cfg, mem, inf, store, row["id"], model)
                if result is not None and not result.get("ok"):
                    succeeded = False
            except Exception as error:
                succeeded = False
                log_diag("notion", f"scheduled resync failed curiosity_id={row['id']}: "
                         f"{type(error).__name__}: {error}")
        return succeeded

    def _fire_due_reminders(self) -> None:
        """Toast any due /remind reminders (30s poll granularity). Best-effort
        — a reminder hiccup must never take down the scheduler."""
        if not getattr(self.cfg, "reminders_enabled", True):
            return
        try:
            from .reminders import fire_due
            fired = fire_due(self.cfg)
            if fired:
                log_diag("inference", f"reminders fired={fired}")
        except Exception as error:
            log_diag("inference", f"reminder firing failed "
                     f"error={type(error).__name__}")

    def run(self, stop_event: threading.Event) -> None:
        """Block until stop_event is set, firing runs as they come due."""
        last_nightly = self._get_nightly_date()
        self._last_loop = (self._get_success_time(LAST_LOOP_ATTEMPT_KEY)
                           or self._get_success_time(LAST_LOOP_KEY))
        self._last_curiosity = (self._get_success_time(LAST_CURIOSITY_ATTEMPT_KEY)
                                or self._get_success_time(LAST_CURIOSITY_KEY))
        self._last_goal_ai = (self._get_success_time(LAST_GOAL_AI_ATTEMPT_KEY)
                              or self._get_success_time(LAST_GOAL_AI_KEY))
        last_checkin = self._get_checkin_date()
        self.log(f"[inference] daily AI cycle started (@ {self.nightly_hour:02d}:00 local)")
        while not stop_event.is_set():
            now = datetime.now().astimezone()   # local time, so nightly_hour is LOCAL
            self._fire_due_reminders()
            if (getattr(self.cfg, "curiosity_metrics_enabled", True) and
                    checkin_prompt_due(now, last_checkin, hour=self.checkin_hour)):
                if self._prompt_for_curiosity_checkin():
                    last_checkin = now.date().isoformat()
                    self._set_checkin_date(last_checkin)
            action = due(now, self._last_loop, last_nightly,
                         interval_seconds=self.interval_seconds,
                         nightly_hour=self.nightly_hour)
            if action == "nightly":
                # Claim cadence BEFORE model/network work. Optional failures must
                # never turn the 30-second poll into an API retry storm.
                last_nightly = now.date().isoformat()
                self._set_nightly_date(last_nightly)
                self._last_loop = now
                self._last_curiosity = now
                self._last_goal_ai = now
                self._set_success_time(LAST_LOOP_ATTEMPT_KEY, now)
                self._set_success_time(LAST_CURIOSITY_ATTEMPT_KEY, now)
                self._set_success_time(LAST_GOAL_AI_ATTEMPT_KEY, now)
                self._run_daily_cycle(now)
            stop_event.wait(self.poll_seconds)
        self.log("[inference] scheduler stopped")
