# Inference Engine — Plan

Status: proposal for review (not yet built). Turns Faerie Fire from "a logbook of
facts you approve" into an **inference engine**: it continuously forms bold
psychological hypotheses about you from your behavior, and refines them with
rapid yes/no statements you answer whenever you open the UI.

## Two kinds of memory

- **Observations (facts)** — what you did. **Auto-committed at ≥75% confidence**;
  triage stops asking you about facts. Low ceremony, background.
- **Inferences (hypotheses)** — *why*, what it means, who you are. **Never
  auto-committed — always validated by yes/no.** This is the product.

## The inference loop (runs every ~20–30 min)

Inputs each run:
- Recent activity since the last run (windows, OCR, browser, clipboard) **plus
  dwell** — how long you lingered on each thing.
- Your model so far: confirmed facts + confirmed inferences + the association graph.
- Per theme: any **previously rejected** hypotheses (as negative constraints).

Output: a small set of new **candidate inferences**, each with a statement, a
theme, a confidence, the **evidence** that triggered it, and links to related
memories. Frequent runs use a cheap/fast model (Haiku); a deeper nightly pass
uses a stronger one (Sonnet).

## Dwell / attention

Derive time-spent per app/content from the event log (gaps between window/screen
changes). Long dwell = strong engagement ("spent 12 min on a thread about X").
Dwell is a primary inference input — it's how the system notices what actually
grabs you, not just what you touched.

## The refinement loop (the core idea)

You answer each candidate **Yes / No / Kind of / Skip**, and can **rewrite it** in
a text box:
- **Yes** → *confirmed*; confidence rises. Confirmed repeatedly → a **core belief**
  (weighted heavily in retrieval, rarely re-asked).
- **No** → *rejected*, and its theme is flagged. **Next loop the engine is told:
  "you guessed X (and earlier Y) about theme T; the user said no — form a genuinely
  different hypothesis consistent with that rejection."** A "no" produces a smarter
  next guess, not silence.
- **Kind of** → *partially true*; kept but flagged — the next round produces a more
  precise version of it.
- **Skip** → stays a candidate; re-surfaced later, lower priority.
- **Refine (text box)** → you rewrite the statement in your own words. Your wording
  becomes the inference (confirmed) and is the strongest teaching signal — the loop
  treats it as ground truth and builds from it.

A theme with several rejections gets **parked** (attempt cap) so it doesn't nag.
Confirmed inferences connect into the association graph (facts ↔ inferences ↔
inferences) — that's "connect it to other insights."

## Validation UX

A dedicated card surface in the app. Whenever you open it, a stack of candidate
statements is waiting. Each card has **Yes / No / Kind of / Skip** plus a **text
box to rewrite the statement** in your own words. Unlimited, fast. Each answer
updates the model immediately; the next loop reflects it.

## Proactive reflection

Once a theme has enough confirmed inferences, the companion **reflects them back**
in conversation ("I've been noticing you seem drawn to …"), each with an inline
**text box so you can refine the statement** on the spot. Your refinements feed
straight back into the model.

## Boldness + guardrails

Bold & psychological: it theorizes about motivations, needs, patterns, and
personality. Guardrails: constructive, non-pathologizing framing; no clinical or
diagnostic claims; you can retire or purge any single inference or a whole theme;
a wrong guess is cheap — a "no" just spawns a better one.

## Cost

~48 loop runs/day on Haiku (fractions of a cent each) + a nightly Sonnet pass.
Low-single-digit dollars/day at heavy use, capped by your spend limit.

## Schema additions (fits current `livingpc/memory.py`)

- New `inference` table: `id, theme, statement, confidence, status
  (candidate|confirmed|rejected|retired), evidence(JSON), refines_id (the rejected
  inference it replaces), source_refs(JSON), times_confirmed, created_at,
  validated_at`.
- Reuse `memory_edge` to link inferences ↔ memories (extend node kinds).
- Facts: triage auto-commits ≥75% into `memory` (skips `pending` for those).
- New `livingpc/inference.py` (loop + prompt, pluggable model) + a validation
  surface + a cadence hook (tray/schedule) for the 20–30 min runs + nightly pass.

## Coexistence with what exists

- Triage keeps running but auto-commits facts; its "questions" role is replaced by
  the inference loop.
- `memory_context` already does relevance retrieval — confirmed inferences flow
  through it into the companion/assistant.
- `memory_edge` association graph already exists — inferences plug straight in.

## Build phases

- **A.** ✅ **Done** — inference store (`livingpc/inference.py`): schema +
  confirm/reject/kind-of/skip/refine lifecycle + confidence dynamics + core-belief
  + theme parking + re-hypothesis lineage. Tests: `tests/test_inference.py` (8/8).
- **B.** ✅ **Done** — the inference loop (`livingpc/inference_loop.py`): dwell
  derivation from foreground sessions, context assembly (facts + confirmed
  inferences + per-theme rejections + parked themes), a pluggable model
  (StubInferenceModel offline / ClaudeInferenceModel — Haiku loop, Sonnet
  nightly), candidate parsing, and a watermarked `run_inference()` that dedups
  against open candidates and skips parked/rejected themes. Tests:
  `tests/test_inference_loop.py` (6/6).
- **C.** ✅ **Done** — the review card UI. `livingpc/inference_review.py`
  (`InferenceReview`: `stack`/`confirmed`/`stats`, a single `answer(action,id,text)`
  dispatcher, and `run_now`) + an **Inferences** tab in `gui.py`: a candidate card
  stack with Yes / Kind of / No / Skip, a per-card rewrite box ("Save my wording"),
  a "Run inference now" button (threaded), and a live "What I now believe about
  you" panel (★ = core belief). Tests: `tests/test_inference_review.py` (5/5).
- **D.** ✅ **Done** — auto-commit facts at ≥75%. `pipeline.apply_result` commits
  confident statements + supersessions straight to memory; low-confidence facts and
  all questions stay pending. Threshold `config.auto_commit_confidence` (0.75).
  `run_triage.generate` uses it. Tests: `tests/test_triage_autocommit.py` (3/3).
- **E.** ✅ **Done** — cadence + nightly + reflection. `inference_scheduler.py`
  (pure `due()` decision + `InferenceScheduler` thread) fires the loop every
  ~25 min and one deeper Sonnet pass nightly; started by the tray daemon.
  Proactive reflection: `InferenceStore.next_reflection`/`mark_reflected`,
  `Companion.maybe_reflection` (paced), companion API `get_reflection`/
  `refine_reflection`, and a reflection card with a refine box in `companion.html`.
  Tests: `tests/test_inference_scheduler.py` (6/6).

## Decisions (locked)

- Auto-commit: **facts only** at ≥75%; inferences are always validated.
- Cadence: inference loop **every ~20–30 min** (cheap model) + nightly deep pass.
- Boldness: **bold & psychological**, with the guardrails above.
- Validation lives in a **dedicated card UI**, shown whenever you open it, unlimited.
- Answer buttons: **Yes / No / Kind of / Skip** + a **text box to rewrite** any card.
- Proactivity: the companion **reflects confirmed insights back**, each with an
  inline refine box.

## Rework (evidence accumulation + confidence gate)

Supersedes the "surface every candidate" behaviour. The engine now:

- files behaviour as hidden, theme-tagged **evidence** (never shown as a question);
- **synthesises** one claim per theme with a **hybrid confidence** = model estimate
  boosted by independent-evidence volume, and cannot graduate without at least
  `inference_min_evidence` pieces;
- only **surfaces** a claim for Yes/No/Kind-of/Skip once it reaches the gate
  (`inference_surface_confidence`, default 0.80); sub-gate themes appear only as a
  passive "forming" progress bar;
- on **No**, keeps the evidence and re-forms a genuinely different >=80% claim.

Key code: `evidence` table + `forming()`/`upsert_claim()`/`to_review(min_confidence)`
in `inference.py`; `hybrid_confidence`/`synthesize_theme`/observe+synthesise in
`inference_loop.py`; `forming()`/gated `stack()` in `inference_review.py`; the
"Still forming" panel in `gui.py`. Tests: 29/29.
