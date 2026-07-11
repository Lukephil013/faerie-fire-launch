"""The No / Kind-of feedback dialogue — teach the engine why it was wrong.

A bare "No" only tells the engine which wording to avoid. This module turns No
and Kind-of into a short dialogue: the model asks a few deeply clarifying
follow-up questions about the claim, the user answers in free text (links like
op.gg profiles welcome — they're kept as references), and the model distills
the answer into a LESSON: an authoritative correction stored per theme
(`feedback_note` in memory.db) and injected into every future synthesis for
that theme. So the next round's claim isn't just "different" — it's informed.

Flow (driven by the GUI):
    questions = feedback_questions(inf, id, action, model)   # show to user
    result    = submit_feedback(inf, id, action, user_text, questions, model)
    -> stores the lesson, applies the No/Kind-of, returns the lesson to show.

Models mirror the rest of the app: ClaudeFeedbackModel (Sonnet — this is a
rare, deep call) and StubFeedbackModel (offline, for tests).
"""
from __future__ import annotations

import json
import os
import re

from .diagnostics import log_diag
from .inference import InferenceStore

_URL_RE = re.compile(r"https?://\S+")

QUESTIONS_SYSTEM = """\
You are the feedback stage of a personal "second brain". The engine made a
claim about the user and the user answered "{action}". Your job: ask at most 3
SHORT follow-up questions that would most sharply clarify what the engine got
wrong (for "no") or what's missing (for "kind of"). Ask about the specific
claim, not generalities. Invite concrete corrections — the user may share
links, stats, or examples. Never be defensive; the user is the authority.

Return STRICT JSON only: {{"questions": [str, ...]}}
"""

ANALYZE_SYSTEM = """\
You are the feedback-analysis stage of a personal "second brain". The engine
made a claim, the user answered "{action}", was asked follow-up questions, and
wrote a reply. Distill their reply into a LESSON: 1-3 plain sentences stating
what the engine should now believe (or stop assuming) about this theme. The
user is the authority — take their framing at face value, don't argue or
psychoanalyze the correction itself. If they shared links, list them as
references (you cannot browse them; treat any stats/details they typed as the
signal). If the reply implies a sharper version of the claim, include it.

Return STRICT JSON only:
{{"lesson": str, "references": [str, ...], "revised_claim": str|null}}
"""


def _extract_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.DOTALL)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


class StubFeedbackModel:
    """Offline/deterministic — lets the whole dialogue run and be tested."""

    def questions(self, claim: dict, action: str) -> list[str]:
        return [
            f"What specifically is wrong about: \"{claim.get('statement', '')}\"?",
            "What should the engine understand about this theme instead?",
        ]

    def analyze(self, claim: dict, action: str, questions: list[str],
                user_text: str) -> dict:
        refs = _URL_RE.findall(user_text or "")
        summary = " ".join((user_text or "").split())[:200]
        return {"lesson": f"User correction ({action}): {summary}" if summary else "",
                "references": refs, "revised_claim": None}


class ClaudeFeedbackModel:
    """Anthropic-backed; uses the nightly (deeper) model — this is a rare call."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None,
                 max_tokens: int = 700, timeout_seconds: float = 60.0):
        self.model = model
        self.max_tokens = max_tokens
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Set the env var or use inference_backend=stub.")
        from anthropic import Anthropic
        self._client = Anthropic(api_key=key, timeout=timeout_seconds)

    def _call(self, system: str, user: str) -> str:
        msg = self._client.messages.create(
            model=self.model, max_tokens=self.max_tokens, system=system,
            messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    def questions(self, claim: dict, action: str) -> list[str]:
        prompt = (f"THEME: {claim.get('theme')}\nCLAIM: {claim.get('statement')}\n"
                  f"EVIDENCE BEHIND IT: {json.dumps(claim.get('evidence') or {})}\n"
                  f"The user answered: {action}. Ask your follow-up questions.")
        log_diag("prompt", f"surface=feedback-questions model={self.model} "
                 f"theme={claim.get('theme')}")
        data = _extract_json(self._call(
            QUESTIONS_SYSTEM.format(action=action), prompt))
        questions = [str(q).strip() for q in (data.get("questions") or []) if str(q).strip()]
        return questions[:3] or ["What did the engine get wrong here?"]

    def analyze(self, claim: dict, action: str, questions: list[str],
                user_text: str) -> dict:
        prompt = (f"THEME: {claim.get('theme')}\nCLAIM: {claim.get('statement')}\n"
                  f"USER ANSWERED: {action}\n"
                  f"QUESTIONS ASKED:\n" + "\n".join(f"- {q}" for q in questions)
                  + f"\n\nUSER'S REPLY:\n{user_text}")
        log_diag("prompt", f"surface=feedback-analyze model={self.model} "
                 f"theme={claim.get('theme')} reply_chars={len(user_text or '')}")
        data = _extract_json(self._call(
            ANALYZE_SYSTEM.format(action=action), prompt))
        refs = list(dict.fromkeys(
            [str(u) for u in (data.get("references") or [])]
            + _URL_RE.findall(user_text or "")))
        lesson = str(data.get("lesson") or "").strip()
        revised = data.get("revised_claim")
        return {"lesson": lesson, "references": refs,
                "revised_claim": str(revised).strip() if revised else None}


def get_feedback_model(config):
    backend = getattr(config, "inference_backend", "claude").lower()
    if backend == "stub":
        return StubFeedbackModel()
    return ClaudeFeedbackModel(
        model=getattr(config, "inference_nightly_model", "claude-sonnet-4-6"),
        timeout_seconds=getattr(config, "llm_timeout_seconds", 60.0))


# --- the flow the GUI drives -------------------------------------------------
def feedback_questions(inf: InferenceStore, inference_id: int, action: str,
                       model) -> list[str]:
    claim = inf.get(inference_id)
    if claim is None:
        raise ValueError(f"inference {inference_id} not found")
    return model.questions(inf._dict(claim), action)


def submit_feedback(inf: InferenceStore, inference_id: int, action: str,
                    user_text: str, questions: list[str], model) -> dict:
    """Analyze the user's reply, store the lesson, then apply the No/Kind-of.
    Empty reply -> no lesson, just the plain action (old behaviour)."""
    if action not in ("no", "kind_of"):
        raise ValueError(f"feedback only applies to no/kind_of, got {action!r}")
    row = inf.get(inference_id)
    if row is None:
        raise ValueError(f"inference {inference_id} not found")
    claim = inf._dict(row)

    lesson, refs = "", []
    user_text = (user_text or "").strip()
    if user_text:
        analysis = model.analyze(claim, action, questions or [], user_text)
        lesson = analysis.get("lesson") or ""
        refs = analysis.get("references") or []
        if lesson:
            inf.add_feedback_note(inference_id, claim["theme"], action,
                                  questions=questions, user_text=user_text,
                                  lesson=lesson, refs=refs)
    if action == "no":
        inf.reject(inference_id)
    else:
        inf.kind_of(inference_id)
    log_diag("inference", f"feedback action={action} theme={claim['theme']} "
             f"lesson={'yes' if lesson else 'no'} refs={len(refs)}")
    return {"lesson": lesson, "references": refs, "theme": claim["theme"]}
