# Upward Spiral Engineering Plan

This is the durable implementation ledger for Faerie Fire's upward-spiral
system. Update it whenever a phase changes status, a product decision is made,
or verification materially changes.

## Product Contract

The loop is:

```text
evidence -> working interpretation -> chosen experiment -> observed outcome
         -> revised interpretation -> better next experiment
```

- Memories record what happened or what the user explicitly said.
- Working interpretations remain versioned, scoped, uncertain, and correctable.
- Investigations resolve meaningful uncertainty.
- The Growth tree records chosen direction and action.
- Model-generated Investigations and tree changes are proposals only.
- Sensitive topics require explicit permission.
- Rejections, deferrals, and blocked topics are durable feedback.
- Strengths, successful exceptions, joy, and aspirations receive equal attention
  to blockers and fears.

## Status Legend

- `pending`: design exists, implementation has not begun.
- `in_progress`: actively being implemented.
- `feedback`: working slice is awaiting user evaluation.
- `verified`: acceptance criteria and automated checks pass.
- `tuning`: technically complete but needs longitudinal product feedback.
- `blocked`: cannot proceed without a decision or external state change.

## Phase Ledger

### Phase 0 — Contracts, migrations, and UI boundaries

Status: `verified`

Deliverables:

- Final lifecycle and authority contracts for memories, claims, Investigations,
  synthesis versions, candidates, and tree proposals.
- Additive database migrations with rollback-safe tests.
- Investigation/Growth UI boundaries protected by syntax and bridge tests.
- Model calls isolated from ordinary reads and unit tests.

Acceptance criteria:

- No layer silently assumes authority owned by another layer.
- Existing data opens without destructive migration.
- No model/network call occurs during read-only UI state loading.

User checkpoint:

> Does the distinction between fact, working interpretation, Investigation, and
> chosen goal feel understandable in the interface?

### Phase 1 — Versioned Investigation Synthesis

Status: `feedback`

Deliverables:

- Current interpretation, confidence, evidence, counterevidence, unknowns,
  experiments, changes since prior version, and reopen conditions.
- Synthesis triggers after meaningful new evidence or explicit review.
- Review UI showing previous interpretation -> current interpretation -> why.
- User correction and approval before downstream influence.

Acceptance criteria:

- One Investigation can produce multiple preserved synthesis versions.
- New evidence can raise or lower confidence.
- The model can state that the evidence is insufficient.

User checkpoint:

> Does Faerie's summary feel like a useful, revisable reflection of you rather
> than a verdict about you?

### Phase 2 — Person-model Reconciliation

Status: `feedback`

Deliverables:

- Reconcile proposed interpretations with existing claims.
- Support, contradict, narrow, retire, and mark situational/change-over-time.
- Track scope, sensitivity, evidence, counterevidence, confirmation, and age.
- Reuse the existing inference/claim system rather than create a competing truth
  database.

Acceptance criteria:

- Contradictory claims do not both silently remain current.
- Investigation corrections propagate without rewriting historical evidence.
- Identity-level claims require stronger evidence than situational claims.

User checkpoint:

> When Faerie changes its mind about you, does its explanation feel fair and
> grounded in the evidence you recognize?

### Phase 3 — Suggested Investigation Engine

Status: `feedback`

Deliverables:

- Candidate object with question, rationale, evidence references, relevance,
  uncertainty, expected usefulness, burden, and sensitivity.
- Deterministic candidate gates plus bounded model wording/ranking.
- Start, refine, defer, reject, and never-ask controls.
- Limits: at most two visible candidates and three to five active
  Investigations by default.

Acceptance criteria:

- No suggested Investigation starts automatically.
- Every suggestion explains why it appeared and what answering could change.
- Rejection and blocked-topic behavior prevents repetitive resurfacing.

User checkpoint:

> Are the suggested Investigations insightful and timely, or do they feel
> intrusive, obvious, or like homework?

### Phase 4 — Tree Relevance and Gardening

Status: `feedback`

Deliverables:

- Relevance metadata and review history for Soul/Root/Branch/Leaf nodes.
- Proposals to rewrite, split, merge, pause, archive, attach evidence, or leave
  unchanged.
- Periodic prompts explaining what newer evidence made a node questionable.
- Historical nodes remain available without cluttering the current tree.

Acceptance criteria:

- The tree can become smaller and more accurate over time.
- Nothing is rewritten, moved, paused, merged, or archived without approval.
- "This goal is no longer mine" is a successful outcome.

User checkpoint:

> Do relevance reviews help the tree feel more like your current life, or are
> they asking you to reconsider things too often?

### Phase 5 — Experiment Outcomes Close the Loop

Status: `feedback`

Deliverables:

- Leaf outcomes capture what happened, expected obstacle, surprise, helpfulness,
  and changed understanding.
- Outcomes become evidence for linked Investigations and claims.
- Synthesis and next-action proposals update after meaningful outcomes.

Acceptance criteria:

- Completing, avoiding, or abandoning an experiment can all produce learning.
- Failed advice reduces confidence in the interpretation that produced it.
- The next experiment visibly reflects prior outcomes.

User checkpoint:

> Do outcome reviews help Faerie learn from real life without making every task
> feel like a journaling assignment?

### Phase 6 — Cadence, Boundaries, and Longitudinal Evaluation

Status: `feedback`

Deliverables:

- Event-driven relevance checks with conservative time-based fallback.
- Quiet hours, snooze escalation, ignored-prompt suppression, and backlog gates.
- Synthetic multi-month journeys for changed dreams, mistaken fear hypotheses,
  successful exceptions, sensitive rejection, and contradictory evidence.
- Local-only metadata for prompt usefulness and burden tuning.

Acceptance criteria:

- Default unsolicited reflection is no more than once per week.
- New contradictions may prompt sooner; inactivity alone does not create noise.
- Longitudinal tests preserve consent, history, and the ability to change.

User checkpoint:

> Over normal use, does Faerie feel attentive at the right moments, and where
> should it speak more or less often?

## Current Decisions

- The phases are construction order, not separate products.
- Implementation may continue through phases without waiting for approval when
  acceptance criteria are clear and existing authority boundaries are preserved.
- User feedback is requested at working-slice checkpoints, not arbitrary dates.
- Default unsolicited reflection cap: one per week.
- Default visible Investigation candidates: two.
- Default active Investigation range: three to five.
- Active goals with no meaningful movement receive a gentle relevance check
  after roughly 30 days by default; descendant activity resets that clock.
- All interpretation and tree mutations remain proposal-based.

## Current Verification Notes

- Phase 0 through Phase 6 focused tests: 266 passing. Coverage includes
  additive migration, synthesis version preservation, confidence reversal,
  insufficient-evidence behavior, two-stage approval, support without
  duplication, contradiction/narrowing lineage, durable rejection, stronger
  identity gates, model-free reads, and inline UI syntax.
  Suggested-Investigation coverage additionally verifies deterministic value
  ranking, a two-card display bound, active-capacity gating, source-reference
  validation, no automatic starts, durable reject/never-ask behavior, deferral
  suppression, refinement, and explicit sensitive-topic permission.
  Tree-gardening coverage verifies encrypted relevance history, event-driven
  due signals, evidence-reference validation, stale-proposal rejection,
  two-stage review/approval, rewrite, split, merge, pause, archive, evidence
  attachment, leave-unchanged, archived-history access, and native UI bridges.
  Outcome coverage verifies completed, attempted, avoided, and abandoned
  learning; encrypted structured capture; factual-memory and claim-evidence
  propagation; linked-Investigation readiness; reduced-confidence drafts after
  unhelpful advice; approval-gated next-Leaf proposals; and visible outcome-led
  next adjustments.
  Cadence coverage verifies one global weekly limit across Investigation,
  inference, and GoalAI prompts; overnight quiet hours; priority without cap
  bypass; a three-item backlog ceiling; subject deduplication; 3/6/12/24-day
  snooze escalation; repeat-ignore suppression; durable never-prompt choices;
  and metadata-only usefulness/burden feedback. Synthetic multi-month journeys
  cover changed dreams, mistaken fear hypotheses, successful exceptions,
  sensitive-topic rejection, and contradictory evidence.
- Full test collection is currently blocked by a missing `capture_control.py`.
- The suite excluding the bridge test also reports pre-existing missing
  `assistant.py` and missing Notion image assets in the context manifest.
- Unrelated skill-system changes are present in the worktree and must remain
  untouched by this program.

## Decision Log

- 2026-07-11: Adopted the upward-spiral model: reflection -> interpretation ->
  experiment -> outcome -> revised interpretation.
- 2026-07-11: User designated Codex as main engineer and requested persistent
  phase tracking plus periodic product-feedback questions.
- 2026-07-11: Investigation syntheses are encrypted, versioned drafts. A small
  deterministic evidence threshold makes review due without a hidden model
  call; approval is explicit and downstream changes remain separate proposals.
- 2026-07-11: Phase 0 verified and the Phase 1 working slice entered user
  feedback. Native-window visual evaluation remains part of that checkpoint.
- 2026-07-11: Phase 2 entered user feedback. Approved syntheses can now produce
  a separate encrypted proposal layer for new/support/contradict/narrow/retire/
  situational/change-over-time updates. A second explicit approval mutates the
  existing inference model; old wording remains historical and weak identity
  claims are rejected by code.
- 2026-07-11: Phase 3 entered user feedback. Suggested Investigations are
  encrypted candidates ranked by relevance, uncertainty, usefulness, and
  burden. At most two are shown; five active Investigations is the ceiling;
  start/refine/defer/reject/never-ask are explicit durable decisions.
- 2026-07-11: Phase 4 entered user feedback. Goal relevance is now versioned
  metadata with event-driven review readiness. Gardening proposals can rewrite,
  split, merge, pause, archive, attach evidence, or preserve a node unchanged;
  none mutate the tree until approved, and archived nodes remain available as
  collapsed history.
- 2026-07-11: User requested occasional checks on quiet goals as well as
  evidence-triggered reviews. Adopted a configurable 30-day default that counts
  meaningful descendant activity and excludes paused/archived goals; Phase 6's
  global cadence cap will prevent multiple quiet goals from becoming nagging.
- 2026-07-11: Phase 5 entered user feedback. Leaf outcomes now record what
  happened, expected obstacles, surprises, helpfulness, changed understanding,
  and the next adjustment. Outcomes become encrypted memories and evidence for
  linked Investigations and claims. Unhelpful advice creates a lower-confidence
  synthesis draft, while the next adjustment becomes an approval-gated Leaf
  proposal rather than an automatic tree mutation.
- 2026-07-12: Phase 6 entered user feedback. All unsolicited Investigation,
  inference, and GoalAI notifications now share one local cadence. The default
  is at most weekly, never during 21:00-08:00 quiet hours, with a three-item
  backlog, escalating snoozes, and durable ignore/never preferences. Explicit
  `/remind` reminders are intentionally outside this gate because the user
  requested them directly.
