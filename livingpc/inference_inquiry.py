"""Persistent conversations that resolve or deliberately investigate beliefs.

An inquiry is bounded to one inference/question. It can read relevant durable
memories, accumulated behavioural evidence, and the existing self-model, but it
cannot silently create a belief. Only an explicit resolution call does that.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .diagnostics import log_diag
from .inference import InferenceStore, concept_similarity
from .memory import MemoryStore
from .memory_context import format_memories, select_memories


SYSTEM = """\
You are Faerie's inference investigator. You are helping the user determine
whether one specific claim about them is accurate, or investigate a question
they explicitly asked about themselves.

Use the supplied memories, behavioural evidence, and existing beliefs as
evidence, not as unquestionable truth. Look for contrast, exceptions, competing
explanations, and what would falsify the current hypothesis. Ask exactly ONE
short, decision-bearing question per turn. Do not repeat a question already
answered. Maintain a concise DRAFT_CLAIM reflecting the best current wording.
Confidence is your evidential estimate, never a substitute for user approval;
do not manufacture 99% certainty. The user alone decides whether a belief is
accepted, tentative, rejected, or needs more evidence.

Return STRICT JSON only:
{"reply": str, "draft_claim": str, "confidence": 0-1}
"""


@dataclass(frozen=True)
class InquiryTurn:
    reply: str
    draft_claim: str
    confidence: float


def _json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(),
                     flags=re.DOTALL)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def parse_turn(text: str, fallback_claim: str) -> InquiryTurn:
    data = _json(text)
    reply = str(data.get("reply") or "").strip()
    draft = str(data.get("draft_claim") or fallback_claim or "").strip()
    try:
        confidence = max(0.0, min(0.99, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    if not reply:
        raise ValueError("investigator returned no reply")
    return InquiryTurn(reply, draft, confidence)


class StubInquiryModel:
    def reply(self, context: str, messages: list[dict], draft: str) -> InquiryTurn:
        if not messages:
            reply = ("What concrete situation would most strongly support or contradict "
                     "this interpretation?")
        else:
            reply = ("Does this revised wording capture the pattern, including its most "
                     "important exception?")
        return InquiryTurn(reply, draft, 0.65 if messages else 0.55)


class ClaudeInquiryModel:
    def __init__(self, model: str, *, api_key: str | None = None,
                 timeout_seconds: float = 60.0):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        from anthropic import Anthropic
        self.model = model
        self._client = Anthropic(api_key=key, timeout=timeout_seconds)

    def reply(self, context: str, messages: list[dict], draft: str) -> InquiryTurn:
        transcript = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in messages[-16:]
        ) or "(conversation has not started)"
        prompt = (f"EVIDENCE CONTEXT:\n{context}\n\nCURRENT DRAFT:\n{draft or '(none)'}"
                  f"\n\nCONVERSATION:\n{transcript}\n\nContinue with one question.")
        log_diag("prompt", f"surface=inference-inquiry model={self.model} "
                 f"messages={len(messages)} input_chars={len(SYSTEM) + len(prompt)}")
        response = self._client.messages.create(
            model=self.model, max_tokens=800, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content
            if getattr(block, "type", "") == "text"
        )
        return parse_turn(text, draft)


def get_inquiry_model(config):
    if str(getattr(config, "inference_backend", "claude")).lower() == "stub":
        return StubInquiryModel()
    return ClaudeInquiryModel(
        getattr(config, "inference_nightly_model", "claude-sonnet-4-6"),
        timeout_seconds=getattr(config, "llm_timeout_seconds", 60.0),
    )


def build_inquiry_context(inf: InferenceStore, mem: MemoryStore,
                          inquiry: dict, *, max_chars: int = 7000) -> str:
    source = inf.get(inquiry["inference_id"]) if inquiry["inference_id"] else None
    source_dict = inf._dict(source) if source is not None else None
    hint = inquiry["prompt"] + " " + (source_dict["statement"] if source_dict else "")
    memories = mem.active_as_dicts()
    selected = select_memories(memories, hint, max_items=24,
                               max_chars=max(1000, max_chars // 2))

    evidence = []
    for theme in inf.themes_with_evidence():
        for observation in inf.evidence_for_theme(theme, limit=20):
            score = concept_similarity(hint, theme + " " + observation)
            if source_dict and theme == source_dict["theme"]:
                score += 0.4
            evidence.append((score, theme, observation))
    evidence.sort(key=lambda item: item[0], reverse=True)
    evidence_lines = [f"- [{theme}] {text}" for score, theme, text in evidence[:24]
                      if score > 0 or (source_dict and theme == source_dict["theme"])]
    beliefs = inf.confirmed()
    belief_lines = [f"- id={b['id']} [{b['theme']}] {b['statement']}"
                    for b in beliefs[:20]]
    source_block = "(user-directed question)"
    if source_dict:
        source_block = (f"[{source_dict['theme']}] {source_dict['statement']}\n"
                        f"Model confidence before discussion: {source_dict['confidence']}\n"
                        f"Evidence metadata: {json.dumps(source_dict['evidence'])}")
    return "\n\n".join([
        "QUESTION OR PROPOSED CLAIM:\n" + inquiry["prompt"],
        "SOURCE INFERENCE:\n" + source_block,
        "RELEVANT SAVED MEMORIES:\n" +
        (format_memories(selected.memories) or "(none selected)"),
        "RELEVANT BEHAVIOURAL OBSERVATIONS:\n" +
        ("\n".join(evidence_lines) or "(none selected)"),
        "EXISTING CONFIRMED BELIEFS (do not duplicate):\n" +
        ("\n".join(belief_lines) or "(none)"),
    ])[:max_chars]


def start_inquiry(config, inf: InferenceStore, mem: MemoryStore, *,
                  kind: str, prompt: str, inference_id=None, model=None) -> dict:
    inquiry_id = inf.start_inquiry(kind, prompt, inference_id=inference_id)
    inquiry = inf.inquiry(inquiry_id)
    if inquiry["messages"]:
        return inquiry
    draft = ""
    if inquiry["inference_id"]:
        source = inf.get(inquiry["inference_id"])
        draft = inf._dict(source)["statement"] if source is not None else ""
    context = build_inquiry_context(
        inf, mem, inquiry,
        max_chars=getattr(config, "inference_memory_max_chars", 2000) + 5000,
    )
    turn = (model or get_inquiry_model(config)).reply(context, [], draft)
    inf.add_inquiry_message(inquiry_id, "assistant", turn.reply)
    inf.update_inquiry_draft(inquiry_id, turn.draft_claim, turn.confidence)
    return inf.inquiry(inquiry_id)


def reply_to_inquiry(config, inf: InferenceStore, mem: MemoryStore,
                     inquiry_id: int, text: str, *, model=None) -> dict:
    inquiry = inf.inquiry(inquiry_id)
    if inquiry is None or inquiry["status"] != "open":
        raise ValueError("inquiry is missing or no longer open")
    text = str(text or "").strip()
    if not text:
        raise ValueError("reply cannot be empty")
    inf.add_inquiry_message(inquiry_id, "user", text)
    inquiry = inf.inquiry(inquiry_id)
    context = build_inquiry_context(
        inf, mem, inquiry,
        max_chars=getattr(config, "inference_memory_max_chars", 2000) + 5000,
    )
    turn = (model or get_inquiry_model(config)).reply(
        context, inquiry["messages"], inquiry["draft_claim"]
    )
    inf.add_inquiry_message(inquiry_id, "assistant", turn.reply)
    inf.update_inquiry_draft(inquiry_id, turn.draft_claim, turn.confidence)
    return inf.inquiry(inquiry_id)
