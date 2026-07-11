"""Controller for the inference review surface (Phase C).

A thin, UI-agnostic layer over `InferenceStore` so the card UI (and tests) share
one vocabulary of actions. The UI renders `stack()` as cards and calls `answer()`
with the button the user pressed; `run_now()` triggers a fresh inference pass.

Actions map to the plan's buttons:
    yes      -> confirm      (confidence rises; enough times => core belief)
    no       -> reject       (becomes a negative constraint for the next loop)
    kind_of  -> kind_of      (partial; next loop sharpens it)
    skip     -> skip         (stays a candidate, deprioritized)
    refine   -> refine(text) -> retire the guess, store YOUR wording as truth
"""
from __future__ import annotations

from .inference import InferenceStore

ACTIONS = ("yes", "no", "kind_of", "skip", "refine")


class InferenceReview:
    def __init__(self, memory_db_path: str):
        self.store = InferenceStore(memory_db_path)

    # --- reads the UI renders ---------------------------------------------
    def stack(self, limit: int | None = None, *,
              gate: float | None = None) -> list[dict]:
        """The yes/no cards waiting — ONLY claims that reached the confidence
        gate. Sub-gate claims never appear here."""
        if gate is None:
            return self.store.to_review(limit)
        return self.store.to_review(limit, min_confidence=gate)

    def forming(self, *, gate: float | None = None) -> list[dict]:
        """Themes still building evidence toward the gate — passive progress only
        (theme + confidence + evidence count), never shown as questions."""
        if gate is None:
            return self.store.forming()
        return self.store.forming(max_confidence=gate)

    def confirmed(self, *, core_only: bool = False) -> list[dict]:
        """What the model now believes — for the 'about you' panel."""
        return self.store.confirmed(core_only=core_only)

    def stats(self) -> dict:
        return self.store.stats()

    # --- the one write entrypoint the UI uses -----------------------------
    def answer(self, action: str, inference_id: int, text: str | None = None):
        """Apply a card action. Returns the new inference id for 'refine',
        otherwise None. Unknown actions raise ValueError."""
        if action == "yes":
            self.store.confirm(inference_id)
        elif action == "no":
            self.store.reject(inference_id)
        elif action == "kind_of":
            self.store.kind_of(inference_id)
        elif action == "skip":
            self.store.skip(inference_id)
        elif action == "refine":
            cleaned = (text or "").strip()
            if not cleaned:
                raise ValueError("refine requires the rewritten statement text")
            return self.store.refine(inference_id, cleaned)
        else:
            raise ValueError(f"unknown action: {action!r} (expected one of {ACTIONS})")
        return None

    # --- proactive reflection ---------------------------------------------
    def next_reflection(self) -> dict | None:
        """A confirmed belief the companion can volunteer back (does not mark it)."""
        return self.store.next_reflection()

    def take_reflection(self) -> dict | None:
        """Pick a belief to reflect AND mark it reflected, so it isn't repeated
        immediately. Returns the belief dict or None."""
        belief = self.store.next_reflection()
        if belief:
            self.store.mark_reflected(belief["id"])
        return belief

    # --- trigger a fresh pass ---------------------------------------------
    def run_now(self, config, *, model=None, nightly: bool = False) -> dict:
        """Run one inference pass now (opens its own stores in the loop). Returns
        a small summary the UI can show. `model` lets tests inject a stub."""
        from .inference_loop import run_inference, get_model

        if model is not None:
            result = run_inference(config, model=model)
        else:
            # Manual regeneration uses the same cost-bounded quality split as
            # the daily cycle: Haiku observes, Sonnet makes the final claim.
            result = run_inference(
                config,
                observer_model=get_model(config, nightly=False, usage_category="manual"),
                synthesis_model=get_model(config, nightly=True, usage_category="manual"),
            )
        return {
            "created": result.created,          # themes that crossed the gate
            "graduated": result.graduated,
            "evidence_added": result.evidence_added,
            "synthesized": result.synthesized,
            "window": result.window,
            "dwell_items": len(result.dwell),
        }

    def close(self) -> None:
        self.store.close()
