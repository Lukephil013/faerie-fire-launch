"""The inference loop — evidence-accumulation model (reworked Phase B).

The engine does NOT show you half-formed guesses. Each run:

  1. Reads recent activity + **dwell** since the last run.
  2. OBSERVE: turns that behaviour into small, theme-tagged **evidence** items
     and files them away silently (the `evidence` table). Nothing is shown.
  3. SYNTHESE: for each theme that got new evidence, weighs ALL accumulated
     evidence for that theme (plus what you've already rejected) into a single
     claim with a **hybrid confidence** = the model's own estimate, boosted by how
     much independent evidence backs it.
  4. GRADUATE: a claim only becomes a yes/no question once it crosses the
     confidence gate (default 0.80). Below that it just shows as a "forming"
     progress bar — never as a question.

A "No" on a graduated claim keeps the evidence and marks the wording rejected, so
the next synthesis forms a genuinely different claim for that theme — deliberately
fast, so the next pass tries something else.

A "Yes", by contrast, is NOT fast to re-litigate. A theme you use daily (say,
League of Legends) keeps generating fresh evidence forever, and evidence never
expires on its own; without a check, the very next run would rebuild a claim from
almost the same evidence pile that already earned the "Yes" and ask you a
near-identical question again — indefinitely. So a theme with an existing
CONFIRMED claim is skipped in synthesis until it has earned enough genuinely NEW
evidence since that confirmation (the same `inference_min_evidence` bar used to
graduate in the first place) to justify asking again. Evidence still accumulates
for it every run either way; only the re-ask is paused.

Pluggable model: StubInferenceModel runs the whole thing offline/testably;
ClaudeInferenceModel uses Anthropic (Haiku for the frequent loop, Sonnet nightly).
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .storage import EventLog
from .memory import MemoryStore
from .inference import InferenceStore, SURFACE_CONFIDENCE
from .triage.aggregate import build_summary, is_internal_ui
from .memory_context import format_memories, select_memories
from .diagnostics import log_diag

WATERMARK_KEY = "inference_watermark"


# --------------------------------------------------------------------------
# Dwell derivation (pure; the loop's primary signal)
# --------------------------------------------------------------------------
def derive_dwell(store: EventLog, start: str, end: str, top: int = 8) -> list[dict]:
    """Time-spent per (app, window) from foreground sessions in [start, end)."""
    rows = store.conn.execute(
        "SELECT app, window_title, start_ts, end_ts FROM sessions "
        "WHERE start_ts >= ? AND start_ts < ?",
        (start, end),
    ).fetchall()

    agg: dict[tuple, dict] = {}
    for r in rows:
        if is_internal_ui(r["app"], r["window_title"]):
            continue
        if not (r["start_ts"] and r["end_ts"]):
            continue
        try:
            secs = (datetime.fromisoformat(r["end_ts"])
                    - datetime.fromisoformat(r["start_ts"])).total_seconds()
        except ValueError:
            continue
        if secs <= 0:
            continue
        key = (r["app"] or "(unknown)", r["window_title"] or "")
        a = agg.setdefault(key, {"app": key[0], "title": key[1],
                                 "seconds": 0.0, "sessions": 0, "longest_seconds": 0.0})
        a["seconds"] += secs
        a["sessions"] += 1
        a["longest_seconds"] = max(a["longest_seconds"], secs)

    items = sorted(agg.values(), key=lambda x: x["seconds"], reverse=True)
    return items[:top] if top else items


def format_dwell(dwell: list[dict]) -> str:
    if not dwell:
        return "(no dwell signal this window)"
    lines = []
    for d in dwell:
        mins = d["seconds"] / 60.0
        longest = d["longest_seconds"] / 60.0
        title = f" — {d['title']}" if d["title"] else ""
        lines.append(
            f"- {d['app']}{title}: ~{mins:.0f} min over {d['sessions']} "
            f"session(s); longest unbroken {longest:.0f} min"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Data shapes
# --------------------------------------------------------------------------
@dataclass
class Evidence:
    theme: str
    observation: str
    weight: float = 1.0
    source_refs: list = field(default_factory=list)


@dataclass
class Claim:
    theme: str
    statement: str
    confidence: float = 0.6   # the MODEL's own estimate (pre-hybrid)


@dataclass
class InferenceContext:
    facts: list[dict] = field(default_factory=list)
    confirmed_inferences: list[dict] = field(default_factory=list)
    rejections_by_theme: dict = field(default_factory=dict)
    parked_themes: list[str] = field(default_factory=list)
    lessons_by_theme: dict = field(default_factory=dict)   # user corrections
    # Other themes' current unanswered claims (theme -> statement), forming or
    # graduated. Synthesis sees these so it can't independently reinvent the
    # same explanatory pattern under a new theme label — see SYNTH_SYSTEM.
    open_candidates_by_theme: dict = field(default_factory=dict)


@dataclass
class InferenceRunResult:
    window: tuple
    dwell: list[dict]
    evidence_added: int
    synthesized: int
    graduated: list[str]          # themes whose claim is now at/over the gate

    @property
    def created(self) -> int:
        """How many themes are now ready to ask you about (crossed the gate)."""
        return len(self.graduated)


def build_context(mem: MemoryStore, inf: InferenceStore) -> InferenceContext:
    facts = mem.active_as_dicts() if hasattr(mem, "active_as_dicts") else []
    rejections = {t: inf.rejected_for_theme(t) for t in inf.theme_rejection_counts()}
    lessons = {}
    if hasattr(inf, "themes_with_lessons"):
        lessons = {t: inf.lessons_for_theme(t) for t in inf.themes_with_lessons()}
    open_candidates = {
        c["theme"]: c["statement"] for c in inf.to_review(min_confidence=0.0)
        if c.get("statement")
    }
    return InferenceContext(
        facts=facts,
        confirmed_inferences=inf.confirmed(),
        rejections_by_theme=rejections,
        parked_themes=inf.parked_themes(),
        lessons_by_theme=lessons,
        open_candidates_by_theme=open_candidates,
    )


# --------------------------------------------------------------------------
# Hybrid confidence: model estimate + independent-evidence boost, gated by a
# minimum amount of evidence so nothing graduates on a single lucky guess.
# --------------------------------------------------------------------------
def hybrid_confidence(model_conf: float, n_evidence: int, *, gate: float,
                      min_evidence: int, per_evidence: float = 0.03,
                      max_boost: float = 0.15) -> float:
    boost = min(max_boost, per_evidence * max(0, n_evidence - 1))
    conf = min(0.99, float(model_conf) + boost)
    if n_evidence < min_evidence:
        conf = min(conf, gate - 0.01)     # can't cross the gate without enough evidence
    return round(max(0.0, conf), 4)


# --------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------
OBSERVE_SYSTEM = """\
You are the observation stage of a personal "second brain". You do NOT draw
conclusions here. From a summary of recent activity and DWELL (how long the user
lingered on things), extract a few small, concrete, behavioural OBSERVATIONS —
raw evidence — each tagged with a stable theme/domain. These accumulate silently
over time; another stage will later decide what they mean. Note what actually
engaged them (dwell), not everything they touched. Do not speculate about
personality yet; just record the behaviour.

Return STRICT JSON only:
{"evidence": [{"theme": str, "observation": str}]}
- "theme" is a stable domain key (e.g. "focus", "League of Legends", "learning").
- "observation" is one concrete behavioural note grounded in the activity/dwell.
"""

SYNTH_SYSTEM = """\
You are the synthesis stage of a personal "second brain". Given a THEME and the
accumulated behavioural EVIDENCE for it, form the single strongest, boldest,
psychologically insightful claim about WHO THE USER IS that this evidence
supports — motivations, needs, values, patterns, contradictions. Then rate your
CONFIDENCE (0-1) that the claim is true given the evidence.

Rules:
- One claim, addressed to "you", one or two sentences, specific and falsifiable.
- Weigh ALL the evidence; more corroboration => higher confidence. Thin or mixed
  evidence => be honest with a lower confidence.
- Do NOT repeat or lightly reword anything under ALREADY REJECTED — the user said
  no; form a genuinely DIFFERENT claim consistent with that rejection.
- USER CORRECTIONS are authoritative: the user explained, in their own words,
  what earlier guesses got wrong about this theme. Every new claim MUST be
  consistent with them — build on what they taught you, don't re-litigate it.
- Do NOT default to a favorite explanatory pattern and reapply it to new subject
  matter. If your best claim here would just restate the same underlying thesis
  already captured under THINGS YOU ALREADY BELIEVE, OTHER PENDING CLAIMS, or
  OTHER THEMES' REJECTED CLAIMS below — even worded differently, about a
  different topic — that is NOT a new insight. The user saying "no" to an idea
  under one theme label means "no" to that idea everywhere, not just there.
  Set is_redundant=true in that case. A synthesis pass that honestly finds
  nothing new for this theme is a valid, expected outcome, not a failure.
- Constructive, non-pathologizing; no clinical/diagnostic language.

Return STRICT JSON only:
{"statement": str, "confidence": 0-1, "is_redundant": bool}
"""


def _facts_block(context: InferenceContext, hint: str) -> str:
    selection = select_memories(context.facts, hint, max_items=24, max_chars=2000)
    return format_memories(selection.memories, include_id=False) or "(none yet)"


def build_observe_prompt(summary: str, dwell: list[dict],
                         context: InferenceContext) -> str:
    return "\n".join([
        "WHAT YOU ALREADY BELIEVE (facts):\n"
        + _facts_block(context, summary + "\n" + format_dwell(dwell)) + "\n",
        "DWELL — where attention actually went:\n" + format_dwell(dwell) + "\n",
        "RECENT ACTIVITY SUMMARY:\n" + summary + "\n",
        "Extract concrete, theme-tagged observations as STRICT JSON.",
    ])


def build_synthesize_prompt(theme: str, evidences: list[str],
                            context: InferenceContext) -> str:
    ev = "\n".join(f"  - {e}" for e in evidences) or "  (none)"
    rejected = context.rejections_by_theme.get(theme, [])
    rej = "\n".join(f"  - {s}" for s in rejected) or "  (none)"
    lessons = context.lessons_by_theme.get(theme, [])
    les = "\n".join(f"  - {s}" for s in lessons) or "  (none)"
    believe = "\n".join(f"  - id={i['id']} [{i['theme']}] {i['statement']}"
                        for i in context.confirmed_inferences[:12]) or "  (none yet)"
    pending = "\n".join(
        f"  - [{t}] {s}" for t, s in context.open_candidates_by_theme.items() if t != theme
    ) or "  (none)"
    other_rejected = "\n".join(
        f"  - [{t}] {s}" for t, statements in context.rejections_by_theme.items()
        if t != theme for s in statements
    ) or "  (none)"
    return "\n".join([
        f"THEME: {theme}\n",
        "ACCUMULATED EVIDENCE FOR THIS THEME:\n" + ev + "\n",
        "ALREADY REJECTED for this theme (go genuinely different):\n" + rej + "\n",
        "USER CORRECTIONS for this theme (authoritative — honor these):\n" + les + "\n",
        "THINGS YOU ALREADY BELIEVE (for coherence):\n" + believe + "\n",
        "OTHER PENDING CLAIMS awaiting an answer, from different themes (don't "
        "restate the same underlying idea under this theme):\n" + pending + "\n",
        "OTHER THEMES' REJECTED CLAIMS (the user already said no to these ideas "
        "elsewhere — don't reintroduce them here under a new label):\n"
        + other_rejected + "\n",
        "Return the single strongest claim + your confidence as STRICT JSON.",
    ])


def _extract_json(text: str) -> dict | None:
    cleaned = re.sub(r"^```(?:json)?", "", text.strip()).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def parse_evidence(text: str) -> list[Evidence]:
    data = _extract_json(text) or {}
    out = []
    for item in data.get("evidence", []):
        obs = str(item.get("observation", "")).strip()
        theme = str(item.get("theme", "") or "general").strip()
        if obs:
            out.append(Evidence(theme=theme, observation=obs))
    return out


def parse_claim(theme: str, text: str) -> Claim | None:
    data = _extract_json(text)
    if not data:
        return None
    if bool(data.get("is_redundant")):
        return None
    statement = str(data.get("statement", "")).strip()
    if not statement:
        return None
    try:
        conf = float(data.get("confidence", 0.6))
    except (TypeError, ValueError):
        conf = 0.6
    return Claim(theme=theme, statement=statement, confidence=max(0.0, min(1.0, conf)))


# --------------------------------------------------------------------------
# Pluggable models
# --------------------------------------------------------------------------
class StubInferenceModel:
    """Offline model. `observe` turns dwell into evidence; `synthesize` grows more
    confident as evidence accumulates and avoids rejected wordings. Lets the whole
    accumulate->graduate flow run and be tested without an API key."""

    def __init__(self, max_evidence: int = 6):
        self.max_evidence = max_evidence

    def observe(self, summary: str, dwell: list[dict],
                context: InferenceContext) -> list[Evidence]:
        out: list[Evidence] = []
        for d in dwell:
            if d["app"] in context.parked_themes:
                continue
            out.append(Evidence(
                theme=d["app"],
                observation=(f"~{round(d['seconds'] / 60)} min on {d['app']} "
                             f"across {d['sessions']} session(s)"),
                source_refs=[]))
            if len(out) >= self.max_evidence:
                return out
        if not out:
            for line in summary.splitlines():
                if line.startswith("## "):
                    app = line[3:].split("—")[0].strip()
                    if app in context.parked_themes:
                        continue
                    out.append(Evidence(theme=app, observation=f"activity in {app}"))
                    if len(out) >= self.max_evidence:
                        break
        return out

    def synthesize(self, theme: str, evidences: list[str],
                   context: InferenceContext) -> Claim:
        n = len(evidences)
        model_conf = min(0.9, 0.55 + 0.06 * n)
        rejected = {s.strip().lower() for s in context.rejections_by_theme.get(theme, [])}
        base = (f"You keep returning to {theme}; the pattern suggests it anchors "
                f"how you regulate focus and mood.")
        alt = (f"Your pull toward {theme} looks less like habit and more like a "
               f"place you go to feel competent.")
        statement = alt if base.strip().lower() in rejected else base
        return Claim(theme=theme, statement=statement, confidence=model_conf)


class ClaudeInferenceModel:
    """Anthropic-backed. Haiku for the frequent loop; Sonnet nightly."""

    def __init__(self, model: str = "claude-haiku-4-5", api_key: str | None = None,
                 max_tokens: int = 900, timeout_seconds: float = 60.0,
                 max_retries: int = 0, memory_max_items: int = 24,
                 memory_max_chars: int = 2000, usage_category: str = "inference"):
        self.model = model
        self.max_tokens = max_tokens
        self.memory_max_items = memory_max_items
        self.memory_max_chars = memory_max_chars
        self.usage_category = usage_category
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set the env var or use inference_backend=stub.")
        from anthropic import Anthropic
        self._client = Anthropic(api_key=self._api_key,
                                 timeout=timeout_seconds, max_retries=max_retries)

    def _call(self, system: str, user: str) -> str:
        started = time.monotonic()
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        from .llm_usage import record_response
        record_response(self.usage_category, self.model, msg, time.monotonic() - started)
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def observe(self, summary, dwell, context) -> list[Evidence]:
        prompt = build_observe_prompt(summary, dwell, context)
        log_diag("prompt", f"surface=inference-observe model={self.model} "
                 f"dwell={len(dwell)} input_chars={len(OBSERVE_SYSTEM) + len(prompt)}")
        return parse_evidence(self._call(OBSERVE_SYSTEM, prompt))

    def synthesize(self, theme, evidences, context) -> Claim | None:
        prompt = build_synthesize_prompt(theme, evidences, context)
        log_diag("prompt", f"surface=inference-synth model={self.model} theme={theme} "
                 f"evidence={len(evidences)}")
        return parse_claim(theme, self._call(SYNTH_SYSTEM, prompt))


def get_model(config, *, nightly: bool = False, usage_category: str = "inference"):
    name = getattr(config, "inference_backend", "claude").lower()
    if name == "stub":
        return StubInferenceModel(getattr(config, "inference_max_candidates", 6))
    if name == "claude":
        model = (getattr(config, "inference_nightly_model", "claude-sonnet-4-6")
                 if nightly else getattr(config, "inference_model", "claude-haiku-4-5"))
        return ClaudeInferenceModel(
            model=model,
            timeout_seconds=getattr(config, "llm_timeout_seconds", 60.0),
            max_retries=getattr(config, "llm_max_retries", 0),
            memory_max_items=getattr(config, "inference_memory_max_items", 24),
            memory_max_chars=getattr(config, "inference_memory_max_chars", 2000),
            usage_category=usage_category)
    raise ValueError(f"unknown inference_backend: {name}")


# --------------------------------------------------------------------------
# Re-litigation gate: an already-confirmed theme waits for fresh evidence
# before being re-asked (see module docstring). Rejected/partial themes are
# NOT gated here — those are supposed to be retried quickly with a different
# claim; only a settled "Yes" needs the cooldown.
# --------------------------------------------------------------------------
def _due_for_resynthesis(inf: InferenceStore, theme: str, min_evidence: int) -> bool:
    last_confirmed = inf.last_confirmed_at(theme)
    if not last_confirmed:
        return True
    return inf.evidence_count_since(theme, last_confirmed) >= min_evidence


# --------------------------------------------------------------------------
# Synthesis for one theme (weigh evidence -> hybrid confidence -> upsert claim)
# --------------------------------------------------------------------------
def synthesize_theme(inf: InferenceStore, model, theme: str,
                     context: InferenceContext, *, gate: float,
                     min_evidence: int) -> dict | None:
    evidences = inf.evidence_for_theme(theme)
    if not evidences:
        return None
    claim = model.synthesize(theme, evidences, context)
    if claim is None or not claim.statement.strip():
        return None
    episode_count = inf.evidence_episode_count(theme)
    final = hybrid_confidence(claim.confidence, episode_count,
                              gate=gate, min_evidence=min_evidence)
    cid = inf.upsert_claim(theme, claim.statement, final,
                           evidence={"evidence_count": len(evidences),
                                     "independent_episodes": episode_count,
                                     "model_confidence": round(claim.confidence, 4)})
    stored = inf.get(cid)
    # A concept-level match may have absorbed this synthesis into an existing
    # canonical belief. That is useful evidence, not a new review card.
    graduated = bool(stored and stored["status"] == "candidate" and final >= gate)
    return {"id": cid, "theme": theme, "confidence": final, "graduated": graduated}


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------
def run_inference(config, *, model=None, observer_model=None, synthesis_model=None,
                  now: datetime | None = None,
                  store: EventLog | None = None, mem: MemoryStore | None = None,
                  inf: InferenceStore | None = None,
                  lookback_hours: float | None = None) -> InferenceRunResult:
    """One pass: observe -> file evidence -> synthesize touched themes -> graduate.
    Opens/closes its own stores unless you pass them in."""
    now_dt = now or datetime.now(timezone.utc)
    own_store, own_mem, own_inf = store is None, mem is None, inf is None
    store = store or EventLog(config.db_path)
    mem = mem or MemoryStore(config.memory_db_path)
    inf = inf or InferenceStore(config.memory_db_path)
    try:
        watermark = store.get_meta(WATERMARK_KEY)
        if watermark:
            start = watermark
        else:
            lb = (lookback_hours if lookback_hours is not None
                  else getattr(config, "inference_lookback_hours", 1.0))
            start = (now_dt - timedelta(hours=lb)).isoformat()
        end = now_dt.isoformat()

        activity_count = int(store.conn.execute(
            "SELECT (SELECT COUNT(*) FROM events WHERE ts>=? AND ts<?) + "
            "(SELECT COUNT(*) FROM sessions WHERE start_ts>=? AND start_ts<?)",
            (start, end, start, end)).fetchone()[0])
        if activity_count == 0:
            # Advancing an empty window prevents an old gap from growing into a
            # giant prompt later, while making no model call at all.
            store.set_meta(WATERMARK_KEY, end)
            log_diag("inference", f"window={start}..{end} skipped=no-new-activity")
            return InferenceRunResult(window=(start, end), dwell=[],
                                      evidence_added=0, synthesized=0, graduated=[])

        summary = build_summary(store, start, end, "Recent activity")
        dwell = derive_dwell(store, start, end)
        context = build_context(mem, inf)
        if model is not None:
            observer_model = observer_model or model
            synthesis_model = synthesis_model or model
        observer_model = observer_model or get_model(config, nightly=False)
        synthesis_model = synthesis_model or get_model(config, nightly=True)

        gate = getattr(config, "inference_surface_confidence", SURFACE_CONFIDENCE)
        min_evidence = getattr(config, "inference_min_evidence", 3)
        max_themes = getattr(config, "inference_max_themes_per_run", 4)
        parked = set(context.parked_themes)

        # OBSERVE -> file evidence silently
        added = 0
        touched: list[str] = []
        run_id = hashlib.sha256(f"{start}|{end}".encode()).hexdigest()
        for item_index, e in enumerate(observer_model.observe(summary, dwell, context)):
            if e.theme in parked:
                continue
            source_refs = list(e.source_refs or []) + [{
                "kind": "activity-window", "start": start, "end": end,
            }]
            inserted = inf.add_evidence(
                e.theme, e.observation, weight=e.weight, source_refs=source_refs,
                run_id=run_id, item_index=item_index,
            )
            if inserted is not None:
                added += 1
            if e.theme not in touched:
                touched.append(e.theme)

        # SYNTHESISE: themes touched this run first (re-weigh with new evidence),
        # then catch up on any evidence-backed theme that doesn't have a claim yet
        # (e.g. a backfill seeded it). Capped per run for cost. A theme that's
        # already CONFIRMED is skipped here until it's earned enough new evidence
        # since that confirmation (_due_for_resynthesis) — otherwise a daily habit
        # like "touched" every run would re-litigate the same settled claim from
        # the same evidence pile and re-ask the identical question forever.
        open_claim_themes = {c["theme"] for c in inf.to_review(min_confidence=0.0)}
        synth_order = [t for t in touched
                      if _due_for_resynthesis(inf, t, min_evidence)]
        for theme in inf.themes_with_evidence():
            if len(synth_order) >= max_themes:
                break
            if theme in parked or theme in synth_order or theme in open_claim_themes:
                continue
            if not _due_for_resynthesis(inf, theme, min_evidence):
                continue
            synth_order.append(theme)

        graduated: list[str] = []
        synthesized = 0
        for theme in synth_order[:max_themes]:
            res = synthesize_theme(inf, synthesis_model, theme, context,
                                   gate=gate, min_evidence=min_evidence)
            if res:
                synthesized += 1
                if res["graduated"]:
                    graduated.append(theme)

        store.set_meta(WATERMARK_KEY, end)
        log_diag("inference", f"window={start}..{end} dwell={len(dwell)} "
                 f"evidence+={added} synthesized={synthesized} graduated={len(graduated)}")
        return InferenceRunResult(window=(start, end), dwell=dwell,
                                  evidence_added=added, synthesized=synthesized,
                                  graduated=graduated)
    finally:
        if own_inf:
            inf.close()
        if own_mem:
            mem.close()
        if own_store:
            store.close()
