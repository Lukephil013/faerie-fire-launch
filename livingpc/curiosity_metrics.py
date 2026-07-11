"""Evidence-based mastery metrics for curiosities.

Raw check-in notes stay local.  The deterministic scorer consumes explicit
ratings and verified effort events; passive capture never awards XP or changes
mastery.  Notion receives only snapshots and sanitized summaries.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import crypto
from .db import connect as db_connect


CHECKIN_CONFIDENCE = 0.8
XP_BY_EVENT = {
    "checkin": 5,
    "assessment": 10,
    "practice": 20,
    "milestone": 50,
}
DAILY_XP_CAP = 100
CALIBRATION_DAYS = 7


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


@dataclass(frozen=True)
class MetricDimension:
    slug: str
    label: str
    description: str
    weight: float
    checkin_prompt: str


@dataclass(frozen=True)
class MetricProfile:
    curiosity_id: int
    version: int
    status: str
    domain: str
    dimensions: tuple[MetricDimension, ...]
    state_metrics: tuple[MetricDimension, ...]
    created_at: str
    approved_at: str | None = None
    publication_status: str = "private"
    publication_approved_at: str | None = None
    last_published_at: str | None = None


@dataclass(frozen=True)
class MetricEvent:
    id: int
    curiosity_id: int
    dimension_slug: str | None
    event_type: str
    observed_score: float | None
    xp: int
    confidence: float
    source_key: str
    occurred_on: str


@dataclass(frozen=True)
class DailySnapshot:
    curiosity_id: int
    snapshot_date: str
    profile_version: int
    metrics: dict[str, dict[str, float | None]]
    state: dict[str, float]
    overall_mastery: float | None
    overall_confidence: float
    xp_delta: int
    total_xp: int
    level: int
    xp_into_level: int
    trend_7d: float | None
    evidence_count: int
    calibration_days: int
    summary: str
    chart_digest: str | None = None
    notion_chart_upload_id: str | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS curiosity_metric_profile (
    curiosity_id INTEGER PRIMARY KEY,
    version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'draft',
    domain TEXT NOT NULL,
    dimensions_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    publication_status TEXT NOT NULL DEFAULT 'private',
    publication_approved_at TEXT,
    last_published_at TEXT,
    CHECK (status IN ('draft', 'approved'))
);

CREATE TABLE IF NOT EXISTS curiosity_metric_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    curiosity_id INTEGER NOT NULL,
    dimension_slug TEXT,
    event_type TEXT NOT NULL,
    observed_score REAL,
    xp INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0,
    source_key TEXT NOT NULL UNIQUE,
    occurred_on TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_event_curiosity_day
ON curiosity_metric_event(curiosity_id, occurred_on);

CREATE TABLE IF NOT EXISTS curiosity_metric_checkin (
    curiosity_id INTEGER NOT NULL,
    checkin_date TEXT NOT NULL,
    state_json TEXT NOT NULL,
    growth_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (curiosity_id, checkin_date)
);

CREATE TABLE IF NOT EXISTS curiosity_daily_snapshot (
    curiosity_id INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    overall_mastery REAL,
    overall_confidence REAL NOT NULL,
    xp_delta INTEGER NOT NULL,
    total_xp INTEGER NOT NULL,
    level INTEGER NOT NULL,
    xp_into_level INTEGER NOT NULL,
    trend_7d REAL,
    evidence_count INTEGER NOT NULL,
    calibration_days INTEGER NOT NULL,
    summary TEXT NOT NULL,
    chart_digest TEXT,
    notion_chart_upload_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (curiosity_id, snapshot_date)
);
"""


def _dimension(slug: str, label: str, description: str, prompt: str,
               weight: float = 0.2) -> MetricDimension:
    return MetricDimension(slug, label, description, weight, prompt)


MENTAL_HEALTH_PROFILE = (
    "mental_health",
    (
        _dimension("regulation", "Emotional regulation", "Using skills to respond rather than react.",
                   "How effectively did you regulate difficult emotions today?"),
        _dimension("recovery", "Recovery skills", "Returning toward baseline after stress.",
                   "How well did you recover after stress or overwhelm today?"),
        _dimension("awareness", "Self-awareness", "Noticing needs, patterns, and internal signals.",
                   "How clearly did you notice and understand your needs today?"),
        _dimension("connection", "Connection", "Seeking or accepting healthy support.",
                   "How supported or meaningfully connected did you feel today?"),
        _dimension("routine", "Routine consistency", "Following supportive routines without perfectionism.",
                   "How consistently did you use supportive routines today?"),
    ),
    (
        _dimension("energy", "Energy", "Current energy state.", "How was your energy today?", 0),
        _dimension("mood", "Mood", "Current mood state, not a success score.", "How was your overall mood today?", 0),
        _dimension("focus", "Focus", "Current ability to focus.", "How available was your focus today?", 0),
        _dimension("stress", "Stress manageability", "How manageable the current stress load feels.", "How manageable did your stress feel today?", 0),
    ),
)

EXERCISE_PROFILE = (
    "exercise",
    (
        _dimension("consistency", "Consistency", "Showing up for planned movement.",
                   "How consistently did you follow your movement plan today?"),
        _dimension("capacity", "Work capacity", "Sustaining appropriate physical effort.",
                   "How capable did your body feel during physical effort today?"),
        _dimension("technique", "Technique", "Moving with control and appropriate form.",
                   "How confident were you in your movement quality today?"),
        _dimension("mobility", "Mobility", "Moving comfortably through useful ranges.",
                   "How available and comfortable was your mobility today?"),
        _dimension("knowledge", "Exercise knowledge", "Understanding training choices and signals.",
                   "How well did you understand what you were doing and why today?"),
    ),
    (
        _dimension("energy", "Energy", "Current physical energy.", "How was your physical energy today?", 0),
        _dimension("soreness", "Movement comfort", "Current comfort and readiness despite soreness.", "How comfortable and movement-ready did your body feel today?", 0),
        _dimension("recovery", "Recovery", "Current sense of physical recovery.", "How recovered did you feel today?", 0),
    ),
)

GENERIC_PROFILE = (
    "general",
    (
        _dimension("consistency", "Consistency", "Repeated verified engagement.", "How consistently did you engage today?"),
        _dimension("knowledge", "Knowledge", "Understanding important concepts.", "How well did you understand the material today?"),
        _dimension("application", "Application", "Using knowledge in practice.", "How effectively did you apply what you know today?"),
        _dimension("reflection", "Reflection", "Learning from outcomes and feedback.", "How clearly did you learn from today's experience?"),
        _dimension("independence", "Independence", "Performing with less support.", "How independently could you work today?"),
    ),
    (_dimension("energy", "Energy", "Current energy state.", "How was your energy today?", 0),),
)


_DRAFT_SYSTEM = (
    "You design a small mastery-tracking rubric for ONE personal investigation. "
    "From the investigation's title and framing, draft measures that are "
    "specific to ITS actual subject — never generic fitness/study filler. "
    "Output STRICT JSON only, no prose, matching exactly:\n"
    '{"dimensions":[{"slug":"...","label":"...","description":"...",'
    '"checkin_prompt":"..."}],"state_metrics":[{"slug":"...","label":"...",'
    '"description":"...","checkin_prompt":"..."}]}\n'
    "Rules: 3-5 dimensions (growth skills the person can get better at), "
    "1-3 state_metrics (today-only readings like energy or comfort — never "
    "scored). slugs: short lowercase ascii with underscores, unique. "
    "checkin_prompt: one plain evening question about TODAY, answerable in a "
    "sentence. Keep labels under 4 words. Reflective and non-diagnostic; no "
    "medical or clinical claims."
)


def _draft_profile_with_model(curiosity: dict) -> MetricProfile | None:
    """One model call to draft measures from the investigation's own framing.
    Returns None on any failure so the keyword templates below still apply."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        from anthropic import Anthropic
        from .lang import is_ko
        client = Anthropic(api_key=key, timeout=25.0, max_retries=0)
        model = os.environ.get("FAERIE_METRIC_MODEL") or "claude-sonnet-4-6"
        system = _DRAFT_SYSTEM + (
            " Write every label, description, and checkin_prompt in natural "
            "Korean (keep slugs ascii)." if is_ko() else "")
        prompt = ("INVESTIGATION TITLE: " + str(curiosity.get("label") or "") +
                  "\nFRAMING:\n" + str(curiosity.get("directive") or ""))
        msg = client.messages.create(model=model, max_tokens=900, system=system,
                                     messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        try:
            from .llm_usage import record_response
            record_response("metric_profile", model, msg, 0.0)
        except Exception:
            pass
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        seen: set[str] = set()

        def build(items, weight):
            out = []
            for item in items or []:
                slug = str(item.get("slug") or "").strip().lower().replace(" ", "_")[:40]
                label = str(item.get("label") or "").strip()[:60]
                prompt_text = str(item.get("checkin_prompt") or "").strip()[:200]
                if not slug or not label or not prompt_text or slug in seen:
                    continue
                seen.add(slug)
                out.append(_dimension(slug, label,
                                      str(item.get("description") or "").strip()[:200],
                                      prompt_text, weight))
            return out

        dims = build(data.get("dimensions"), 0.2)[:5]
        states = build(data.get("state_metrics"), 0)[:3]
        if len(dims) < 3 or not states:
            return None
        return MetricProfile(int(curiosity["id"]), 1, "draft", "custom",
                             tuple(dims), tuple(states), _now())
    except Exception:
        return None


def proposed_profile(curiosity: dict) -> MetricProfile | None:
    # Preferred: one GoalAI call drafts measures from the investigation's own
    # framing (domain "custom"), so a food-crash investigation gets food-crash
    # measures instead of a canned template that happened to keyword-match.
    drafted = _draft_profile_with_model(curiosity)
    if drafted is not None:
        return drafted
    # Fallback: the original keyword-matched starter templates.
    text = f"{curiosity.get('label', '')} {curiosity.get('directive', '')}".lower()
    if any(word in text for word in ("mental", "mood", "anxiety", "wellbeing", "stress")):
        domain, dimensions, states = MENTAL_HEALTH_PROFILE
    elif any(word in text for word in ("exercise", "fitness", "workout", "gym", "strength")):
        domain, dimensions, states = EXERCISE_PROFILE
    else:
        return None
    return MetricProfile(int(curiosity["id"]), 1, "draft", domain,
                         tuple(dimensions), tuple(states), _now())


class MetricStore:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = db_connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.conn.execute(
            "PRAGMA table_info(curiosity_metric_profile)")}
        additions = {
            "publication_status": "TEXT NOT NULL DEFAULT 'private'",
            "publication_approved_at": "TEXT",
            "last_published_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE curiosity_metric_profile ADD COLUMN {name} {declaration}")

    def close(self) -> None:
        self.conn.close()

    def ensure_profile(self, curiosity: dict) -> MetricProfile | None:
        existing = self.get_profile(int(curiosity["id"]))
        if existing:
            if existing.domain == "general" and existing.status == "draft":
                return None
            return existing
        profile = proposed_profile(curiosity)
        if profile is None:
            return None
        self.conn.execute(
            "INSERT INTO curiosity_metric_profile "
            "(curiosity_id,version,status,domain,dimensions_json,state_json,created_at,"
            "approved_at,publication_status,publication_approved_at,last_published_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (profile.curiosity_id, profile.version, profile.status, profile.domain,
             _json([asdict(d) for d in profile.dimensions]),
             _json([asdict(d) for d in profile.state_metrics]),
             profile.created_at, profile.approved_at, profile.publication_status,
             profile.publication_approved_at, profile.last_published_at),
        )
        self.conn.commit()
        return profile

    def get_profile(self, curiosity_id: int) -> MetricProfile | None:
        row = self.conn.execute(
            "SELECT * FROM curiosity_metric_profile WHERE curiosity_id=?", (curiosity_id,)
        ).fetchone()
        if not row:
            return None
        return MetricProfile(
            row["curiosity_id"], row["version"], row["status"], row["domain"],
            tuple(MetricDimension(**item) for item in json.loads(row["dimensions_json"])),
            tuple(MetricDimension(**item) for item in json.loads(row["state_json"])),
            row["created_at"], row["approved_at"], row["publication_status"],
            row["publication_approved_at"], row["last_published_at"],
        )

    def approve_publication(self, curiosity_id: int) -> MetricProfile:
        profile = self.get_profile(curiosity_id)
        if not profile or profile.status != "approved":
            raise ValueError("approved metric profile required")
        snapshot = self.latest_snapshot(curiosity_id)
        if (self.checkin_days(curiosity_id) < CALIBRATION_DAYS or not snapshot or
                snapshot.calibration_days < CALIBRATION_DAYS):
            raise ValueError("seven distinct check-in days are required before publishing")
        timestamp = _now()
        self.conn.execute(
            "UPDATE curiosity_metric_profile SET publication_status='published',"
            "publication_approved_at=? WHERE curiosity_id=?", (timestamp, curiosity_id))
        self.conn.commit()
        return self.get_profile(curiosity_id)  # type: ignore[return-value]

    def revoke_publication(self, curiosity_id: int) -> MetricProfile:
        status = "ready" if self.checkin_days(curiosity_id) >= CALIBRATION_DAYS else "private"
        self.conn.execute(
            "UPDATE curiosity_metric_profile SET publication_status=?,"
            "publication_approved_at=NULL WHERE curiosity_id=?", (status, curiosity_id))
        self.conn.commit()
        profile = self.get_profile(curiosity_id)
        if not profile:
            raise ValueError("metric profile not found")
        return profile

    def mark_published_success(self, curiosity_id: int) -> None:
        self.conn.execute(
            "UPDATE curiosity_metric_profile SET last_published_at=? WHERE curiosity_id=?",
            (_now(), curiosity_id))
        self.conn.commit()

    def checkin_days(self, curiosity_id: int) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM curiosity_metric_checkin WHERE curiosity_id=?",
            (curiosity_id,)).fetchone()[0])

    def approve_profile(self, curiosity_id: int, *, dimensions=None,
                        state_metrics=None) -> MetricProfile:
        profile = self.get_profile(curiosity_id)
        if not profile:
            raise ValueError(f"metric profile for curiosity {curiosity_id} not found")
        dims = _validated_dimensions(dimensions or [asdict(d) for d in profile.dimensions],
                                     require_weight=True)
        states = _validated_dimensions(state_metrics or [asdict(d) for d in profile.state_metrics],
                                       require_weight=False)
        approved_at = _now()
        self.conn.execute(
            "UPDATE curiosity_metric_profile SET version=version+1,status='approved',"
            "dimensions_json=?,state_json=?,approved_at=? WHERE curiosity_id=?",
            (_json([asdict(d) for d in dims]), _json([asdict(d) for d in states]),
             approved_at, curiosity_id),
        )
        self.conn.commit()
        return self.get_profile(curiosity_id)  # type: ignore[return-value]

    def record_event(self, curiosity_id: int, event_type: str, source_key: str, *,
                     dimension_slug: str | None = None,
                     observed_score: float | None = None,
                     xp: int | None = None, confidence: float = 0.6,
                     occurred_on: str | None = None) -> bool:
        if event_type not in XP_BY_EVENT:
            raise ValueError(f"unsupported metric event type: {event_type}")
        if occurred_on is None:
            occurred_on = datetime.now().astimezone().date().isoformat()
        score = None if observed_score is None else _clamp(observed_score)
        try:
            self.conn.execute(
                "INSERT INTO curiosity_metric_event "
                "(curiosity_id,dimension_slug,event_type,observed_score,xp,confidence,"
                "source_key,occurred_on,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (curiosity_id, dimension_slug, event_type, score,
                 max(0, int(XP_BY_EVENT[event_type] if xp is None else xp)),
                 max(0.0, min(1.0, float(confidence))), source_key, occurred_on, _now()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def record_checkin(self, curiosity_id: int, state: dict[str, float],
                       growth: dict[str, float], note: str = "", *,
                       checkin_date: str | None = None) -> bool:
        profile = self.get_profile(curiosity_id)
        if not profile or profile.status != "approved":
            raise ValueError("approve the metric profile before checking in")
        day = checkin_date or datetime.now().astimezone().date().isoformat()
        state_slugs = {item.slug for item in profile.state_metrics}
        growth_slugs = {item.slug for item in profile.dimensions}
        clean_state = {k: _rating_to_score(v) for k, v in state.items() if k in state_slugs}
        clean_growth = {k: _rating_to_score(v) for k, v in growth.items() if k in growth_slugs}
        existing = self.conn.execute(
            "SELECT 1 FROM curiosity_metric_checkin WHERE curiosity_id=? AND checkin_date=?",
            (curiosity_id, day),
        ).fetchone()
        self.conn.execute(
            "INSERT OR REPLACE INTO curiosity_metric_checkin "
            "(curiosity_id,checkin_date,state_json,growth_json,note,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (curiosity_id, day, _json(clean_state), _json(clean_growth),
             crypto.enc(str(note or "")), _now()),
        )
        self.conn.commit()
        if not existing:
            self.record_event(curiosity_id, "checkin", f"checkin:{curiosity_id}:{day}:xp",
                              xp=XP_BY_EVENT["checkin"], confidence=CHECKIN_CONFIDENCE,
                              occurred_on=day)
        for slug, score in clean_growth.items():
            source = f"checkin:{curiosity_id}:{day}:{slug}"
            if existing:
                self.conn.execute(
                    "UPDATE curiosity_metric_event SET observed_score=?,confidence=? "
                    "WHERE source_key=?", (score, CHECKIN_CONFIDENCE, source))
                self.conn.commit()
            else:
                self.record_event(curiosity_id, "checkin", source,
                                  dimension_slug=slug, observed_score=score, xp=0,
                                  confidence=CHECKIN_CONFIDENCE, occurred_on=day)
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND "
            "name IN ('goal_agent_state','goal_curiosity_link','goal_node')")}
        if len(tables) == 3:
            now = _now()
            state_columns = {r[1] for r in self.conn.execute(
                "PRAGMA table_info(goal_agent_state)").fetchall()}
            linked = self.conn.execute(
                "SELECT goal_id FROM goal_curiosity_link WHERE curiosity_id=?",
                (int(curiosity_id),)).fetchall()
            for row in linked:
                current = int(row[0])
                while current:
                    if {"dirty_reason", "deferred"}.issubset(state_columns):
                        self.conn.execute(
                            "UPDATE goal_agent_state SET dirty=1,dirty_reason=?,deferred=0,"
                            "updated_at=? WHERE node_id=?",
                            ("attached curiosity check-in", now, current))
                    else:
                        self.conn.execute(
                            "UPDATE goal_agent_state SET dirty=1,updated_at=? WHERE node_id=?",
                            (now, current))
                    parent = self.conn.execute(
                        "SELECT parent_id FROM goal_node WHERE id=?", (current,)).fetchone()
                    current = int(parent[0]) if parent and parent[0] else 0
            self.conn.commit()
        return not bool(existing)

    def build_snapshot(self, curiosity_id: int, snapshot_date: str) -> DailySnapshot:
        profile = self.get_profile(curiosity_id)
        if not profile or profile.status != "approved":
            raise ValueError("approved metric profile required")
        prior = self.conn.execute(
            "SELECT * FROM curiosity_daily_snapshot WHERE curiosity_id=? AND snapshot_date<? "
            "ORDER BY snapshot_date DESC LIMIT 1", (curiosity_id, snapshot_date),
        ).fetchone()
        prior_metrics = json.loads(prior["metrics_json"]) if prior else {}
        events = self.conn.execute(
            "SELECT * FROM curiosity_metric_event WHERE curiosity_id=? AND occurred_on=? "
            "ORDER BY id", (curiosity_id, snapshot_date),
        ).fetchall()
        metrics: dict[str, dict[str, float | None]] = {}
        evidence_count = 0
        start_28 = (date.fromisoformat(snapshot_date) - timedelta(days=27)).isoformat()
        for dimension in profile.dimensions:
            previous = prior_metrics.get(dimension.slug, {}).get("mastery")
            current = None if previous is None else float(previous)
            observed = [row for row in events
                        if row["dimension_slug"] == dimension.slug
                        and row["observed_score"] is not None]
            for row in observed:
                evidence_count += 1
                score = float(row["observed_score"])
                if current is None:
                    current = score
                else:
                    current += 0.25 * float(row["confidence"]) * (score - current)
            evidence = self.conn.execute(
                "SELECT confidence,occurred_on FROM curiosity_metric_event WHERE curiosity_id=? "
                "AND dimension_slug=? AND occurred_on BETWEEN ? AND ? "
                "AND observed_score IS NOT NULL",
                (curiosity_id, dimension.slug, start_28, snapshot_date),
            ).fetchall()
            snapshot_day = date.fromisoformat(snapshot_date)
            effective = sum(
                float(row["confidence"]) * max(
                    0.0, 1.0 - (snapshot_day - date.fromisoformat(row["occurred_on"])).days / 28.0)
                for row in evidence)
            confidence = 1.0 - math.exp(-effective / 4.0)
            metrics[dimension.slug] = {
                "mastery": None if current is None else round(_clamp(current), 2),
                "confidence": round(confidence, 4),
            }
        checkin = self.conn.execute(
            "SELECT state_json FROM curiosity_metric_checkin WHERE curiosity_id=? "
            "AND checkin_date=?", (curiosity_id, snapshot_date),
        ).fetchone()
        state = json.loads(checkin["state_json"]) if checkin else {}
        weighted = [(float(metrics[d.slug]["mastery"]), d.weight)
                    for d in profile.dimensions if metrics[d.slug]["mastery"] is not None]
        overall = (sum(score * weight for score, weight in weighted) /
                   sum(weight for _, weight in weighted)) if weighted else None
        populated_conf = [float(metrics[d.slug]["confidence"]) for d in profile.dimensions
                          if metrics[d.slug]["mastery"] is not None]
        overall_conf = sum(populated_conf) / len(populated_conf) if populated_conf else 0.0
        xp_delta = min(DAILY_XP_CAP, sum(int(row["xp"]) for row in events))
        total_xp = self._total_xp(curiosity_id, snapshot_date)
        level = total_xp // 100 + 1
        calibration_days = int(self.conn.execute(
            "SELECT COUNT(*) FROM curiosity_metric_checkin WHERE curiosity_id=? "
            "AND checkin_date<=?", (curiosity_id, snapshot_date),
        ).fetchone()[0])
        if calibration_days >= CALIBRATION_DAYS and profile.publication_status == "private":
            self.conn.execute(
                "UPDATE curiosity_metric_profile SET publication_status='ready' "
                "WHERE curiosity_id=?", (curiosity_id,))
            self.conn.commit()
        trend = self._trend(curiosity_id, snapshot_date, overall,
                            has_evidence=evidence_count > 0)
        summary = _snapshot_summary(overall, overall_conf, trend, calibration_days)
        snapshot = DailySnapshot(
            curiosity_id, snapshot_date, profile.version, metrics, state,
            None if overall is None else round(overall, 2), round(overall_conf, 4),
            xp_delta, total_xp, level, total_xp % 100,
            None if trend is None else round(trend, 2), evidence_count,
            calibration_days, summary,
        )
        self._save_snapshot(snapshot)
        return snapshot

    def latest_snapshot(self, curiosity_id: int) -> DailySnapshot | None:
        row = self.conn.execute(
            "SELECT * FROM curiosity_daily_snapshot WHERE curiosity_id=? "
            "ORDER BY snapshot_date DESC LIMIT 1", (curiosity_id,),
        ).fetchone()
        return _snapshot_from_row(row) if row else None

    def history(self, curiosity_id: int, limit: int = 30) -> list[DailySnapshot]:
        rows = self.conn.execute(
            "SELECT * FROM curiosity_daily_snapshot WHERE curiosity_id=? "
            "ORDER BY snapshot_date DESC LIMIT ?", (curiosity_id, int(limit)),
        ).fetchall()
        return [_snapshot_from_row(row) for row in reversed(rows)]

    def set_chart_upload(self, curiosity_id: int, snapshot_date: str,
                         digest: str, upload_id: str) -> None:
        self.conn.execute(
            "UPDATE curiosity_daily_snapshot SET chart_digest=?,notion_chart_upload_id=? "
            "WHERE curiosity_id=? AND snapshot_date=?",
            (digest, upload_id, curiosity_id, snapshot_date),
        )
        self.conn.commit()

    def _save_snapshot(self, snapshot: DailySnapshot) -> None:
        previous = self.conn.execute(
            "SELECT chart_digest,notion_chart_upload_id FROM curiosity_daily_snapshot "
            "WHERE curiosity_id=? AND snapshot_date=?",
            (snapshot.curiosity_id, snapshot.snapshot_date),
        ).fetchone()
        self.conn.execute(
            "INSERT OR REPLACE INTO curiosity_daily_snapshot VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snapshot.curiosity_id, snapshot.snapshot_date, snapshot.profile_version,
             _json(snapshot.metrics), _json(snapshot.state), snapshot.overall_mastery,
             snapshot.overall_confidence, snapshot.xp_delta, snapshot.total_xp,
             snapshot.level, snapshot.xp_into_level, snapshot.trend_7d,
             snapshot.evidence_count, snapshot.calibration_days, snapshot.summary,
             previous["chart_digest"] if previous else snapshot.chart_digest,
             previous["notion_chart_upload_id"] if previous else snapshot.notion_chart_upload_id,
             _now()),
        )
        self.conn.commit()

    def _total_xp(self, curiosity_id: int, through_date: str) -> int:
        rows = self.conn.execute(
            "SELECT occurred_on,SUM(xp) total FROM curiosity_metric_event "
            "WHERE curiosity_id=? AND occurred_on<=? GROUP BY occurred_on",
            (curiosity_id, through_date),
        ).fetchall()
        return sum(min(DAILY_XP_CAP, int(row["total"] or 0)) for row in rows)

    def _trend(self, curiosity_id: int, snapshot_date: str,
               current: float | None, *, has_evidence: bool) -> float | None:
        target = date.fromisoformat(snapshot_date)
        start = (target - timedelta(days=13)).isoformat()
        rows = self.conn.execute(
            "SELECT snapshot_date,overall_mastery FROM curiosity_daily_snapshot "
            "WHERE curiosity_id=? AND snapshot_date>=? AND snapshot_date<? "
            "AND overall_mastery IS NOT NULL AND evidence_count>0 ORDER BY snapshot_date",
            (curiosity_id, start, snapshot_date),
        ).fetchall()
        values = [(row["snapshot_date"], float(row["overall_mastery"])) for row in rows]
        if has_evidence and current is not None:
            values.append((snapshot_date, float(current)))
        current_start = target - timedelta(days=6)
        current_week = [value for day, value in values
                        if date.fromisoformat(day) >= current_start]
        prior_week = [value for day, value in values
                      if date.fromisoformat(day) < current_start]
        if not current_week or not prior_week:
            return None
        return sum(current_week) / len(current_week) - sum(prior_week) / len(prior_week)


def _validated_dimensions(raw: list[dict], *, require_weight: bool) -> tuple[MetricDimension, ...]:
    if not raw or len(raw) > 5:
        raise ValueError("metric profiles need between one and five dimensions")
    dimensions = []
    seen = set()
    for item in raw:
        if isinstance(item, MetricDimension):
            item = asdict(item)
        slug = str(item.get("slug", "")).strip().lower().replace(" ", "_")
        if not slug or slug in seen:
            raise ValueError("metric dimension slugs must be unique")
        seen.add(slug)
        weight = float(item.get("weight", 0.0 if not require_weight else 1.0))
        dimensions.append(MetricDimension(
            slug, str(item.get("label", slug)).strip()[:80],
            str(item.get("description", "")).strip()[:300],
            max(0.0, weight), str(item.get("checkin_prompt", "")).strip()[:300],
        ))
    if require_weight:
        total = sum(item.weight for item in dimensions)
        if total <= 0:
            raise ValueError("metric dimension weights must total more than zero")
        dimensions = [MetricDimension(d.slug, d.label, d.description, d.weight / total,
                                      d.checkin_prompt) for d in dimensions]
    return tuple(dimensions)


def _rating_to_score(value: float) -> float:
    rating = max(0.0, min(10.0, float(value)))
    return round(rating * 10.0, 2)


def _snapshot_summary(overall: float | None, confidence: float,
                      trend: float | None, calibration_days: int) -> str:
    if overall is None:
        return "No mastery estimate yet; complete a check-in to establish a baseline."
    phase = (f"Calibration day {calibration_days} of 7. " if calibration_days < 7 else "")
    trend_text = "Trend needs more history." if trend is None else (
        f"Seven-day trend is {trend:+.1f} points.")
    return (f"{phase}Overall mastery is {overall:.0f}% at {confidence:.0%} confidence. "
            f"{trend_text}")


def _snapshot_from_row(row) -> DailySnapshot:
    return DailySnapshot(
        row["curiosity_id"], row["snapshot_date"], row["profile_version"],
        json.loads(row["metrics_json"]), json.loads(row["state_json"]),
        row["overall_mastery"], row["overall_confidence"], row["xp_delta"],
        row["total_xp"], row["level"], row["xp_into_level"], row["trend_7d"],
        row["evidence_count"], row["calibration_days"], row["summary"],
        row["chart_digest"], row["notion_chart_upload_id"],
    )


def snapshot_digest(profile: MetricProfile, snapshot: DailySnapshot,
                    history: list[DailySnapshot]) -> str:
    payload = {
        "profile": [asdict(item) for item in profile.dimensions],
        "state_profile": [asdict(item) for item in profile.state_metrics],
        "snapshot": asdict(snapshot),
        "history": [asdict(item) for item in history[-30:]],
    }
    payload["snapshot"].pop("notion_chart_upload_id", None)
    payload["snapshot"].pop("chart_digest", None)
    for item in payload["history"]:
        item.pop("notion_chart_upload_id", None)
        item.pop("chart_digest", None)
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:16]


def render_dashboard(profile: MetricProfile, snapshot: DailySnapshot,
                     history: list[DailySnapshot], output_dir: str) -> tuple[str, str]:
    """Render one privacy-safe radar + trend PNG using Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    digest = snapshot_digest(profile, snapshot, history)
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"curiosity-{snapshot.curiosity_id}-{snapshot.snapshot_date}-{digest}.png"
    if path.exists():
        return str(path), digest
    width, height = 1400, 760
    image = Image.new("RGB", (width, height), "#101713")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    draw.text((42, 28), f"LEVEL {snapshot.level}   XP {snapshot.total_xp}",
              fill="#a8e6b0", font=font)
    draw.text((42, 52), snapshot.summary, fill="#c8d2c9", font=font)
    _draw_radar(draw, profile, snapshot, (360, 360), 245, font)
    _draw_trend(draw, history[-30:], (720, 115, 1320, 555), font)
    y = 610
    for metric in profile.state_metrics:
        if metric.slug not in snapshot.state:
            continue
        value = float(snapshot.state[metric.slug])
        draw.text((720, y), metric.label, fill="#c8d2c9", font=font)
        draw.rectangle((850, y, 1250, y + 14), fill="#28352c")
        draw.rectangle((850, y, 850 + int(4 * value), y + 14), fill="#5fd36f")
        draw.text((1260, y), f"{value:.0f}%", fill="#a8e6b0", font=font)
        y += 32
    image.save(path, format="PNG", optimize=True)
    return str(path), digest


def _draw_radar(draw, profile: MetricProfile, snapshot: DailySnapshot,
                center: tuple[int, int], radius: int, font) -> None:
    dimensions = list(profile.dimensions)
    if not dimensions:
        return
    cx, cy = center
    count = len(dimensions)
    for ring in range(1, 6):
        points = []
        r = radius * ring / 5
        for index in range(count):
            angle = -math.pi / 2 + 2 * math.pi * index / count
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(points, outline="#34463a")
    values = []
    for index, dimension in enumerate(dimensions):
        angle = -math.pi / 2 + 2 * math.pi * index / count
        value = snapshot.metrics.get(dimension.slug, {}).get("mastery")
        fraction = (float(value) / 100.0) if value is not None else 0.0
        values.append((cx + radius * fraction * math.cos(angle),
                       cy + radius * fraction * math.sin(angle)))
        lx = cx + (radius + 42) * math.cos(angle)
        ly = cy + (radius + 30) * math.sin(angle)
        draw.text((lx - 35, ly - 6), dimension.label[:18], fill="#c8d2c9", font=font)
    draw.polygon(values, fill="#285a32", outline="#75e36f", width=3)


def _draw_trend(draw, history: list[DailySnapshot], box: tuple[int, int, int, int], font) -> None:
    left, top, right, bottom = box
    draw.text((left, top - 28), "30-day mastery trend", fill="#a8e6b0", font=font)
    draw.rectangle(box, outline="#34463a", width=1)
    valid = [(index, item.overall_mastery) for index, item in enumerate(history)
             if item.overall_mastery is not None]
    if len(valid) < 2:
        draw.text((left + 20, top + 20), "More daily snapshots are needed.",
                  fill="#819087", font=font)
        return
    span = max(1, len(history) - 1)
    points = []
    for index, value in valid:
        x = left + (right - left) * index / span
        y = bottom - (bottom - top) * float(value) / 100.0
        points.append((x, y))
    draw.line(points, fill="#75e36f", width=4, joint="curve")
