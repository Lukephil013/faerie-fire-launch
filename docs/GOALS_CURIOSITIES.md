# Goals, Investigations, and GoalAI

Faerie Fire uses one Soul-centered tree as the source of truth for direction and
action. Curiosities still exist in the database, but in the interface they are
best understood as **Investigations**: persistent questions that feed the tree.

## The core objects

| Object | What it is | Best use |
|---|---|---|
| Soul | The umbrella identity/intention for the whole system | “Who am I becoming?” |
| Root | A major life domain under the Soul | Mental Health, Korean Fluency, Work/Career |
| Branch | A more specific outcome or area inside a Root | Reduce work dread, find sustainable exercise |
| Leaf | A concrete action/task | Apply to one role, try one hike, review one grammar point |
| Investigation | A persistent unresolved question | “Why do I avoid this exercise style?” |
| Proposal | A suggested change to the tree | Create a Branch, add a Leaf, start an Investigation |
| Evidence | Material used for reasoning | Answers, memories, check-ins, metrics, manual notes |

## Goals vs investigations

Use a **Goal node** when you already know the direction:

- “Improve mental health.”
- “Move toward AI/LLM engineering work.”
- “Find sustainable exercise.”
- “Master Korean grammar pattern X.”

Use an **Investigation** when the important thing is still uncertain. Start it
by dumping your current thoughts in the journal box, not by trying to write a
perfect prompt:

- “Why do I dread work communication before starting?”
- “What kind of exercise would I actually repeat?”
- “How does tiredness distort my self-assessment?”
- “What evidence would prove this plan is working?”

The goal is the living structure. The investigation is the question/current-state
dump that helps the structure become smarter. The first AI follow-ups should be
grounded in what you just wrote, with older memories treated only as background.

## Review Node vs Investigation

**Review Node** is a strategic audit of an existing node. It asks:

> Given this node’s current description, children, evidence, progress, blockers,
> attached investigations, and cached reports, what needs attention?

Review Node may generate questions, but those questions are in service of
managing that node right now.

These review questions follow the assessment that produced them. Dismissing a
question suppresses the same question in later rounds unless the user explicitly
reopens it. Approving an action proposal retires the still-open questions from
that same assessment, because beginning the action has changed the context the
questions were trying to clarify.

An **Investigation** is different because it persists over time. It owns a
specific unresolved question, generates follow-ups or suggestions, gathers
answers, and feeds those results back into the attached node.

In short:

- Review Node = “What is going on here?”
- Investigation = “Keep studying this unknown until it is useful.”
- Proposal = “Given what we learned, what should change?”

### Exploration Threads inside an Investigation

An Investigation can contain **Exploration Threads** when several distinct
lenses still answer the same umbrella question. Threads own their own pending
questions and suggestions, while their answered evidence is labeled and rolled
up into the parent Investigation's working interpretation. For example, a
Faerie Fire Investigation can keep aesthetic/identity evidence separate from a
market-validation thread without pretending those are unrelated dreams.

When a proposed Investigation overlaps existing work, the user can choose to:

- add a near-identical lead as context to an existing direction;
- create an Exploration Thread inside the related Investigation; or
- start a separate Investigation with its own evidence cycle.

Faerie automatically absorbs only very strong duplicates. Meaningfully
different overlap remains a reviewable proposal. Thread conversations and
items never become sibling-thread evidence without appearing in the bounded
parent rollup.

## Suggestions that overlap existing goals

Faerie consolidates strongly similar open suggestions so a model wording
variation does not appear as a second task. The clearer, higher-confidence
version stays open; the other version remains available in resolved history.

Before turning a suggestion into a new plan, the interface compares its meaning
with active Roots, Branches, and Leaves and shows the closest matches. It
compares both the node's current wording and the Investigation suggestion that
directly created that node. Origin provenance is not inherited by descendants:
one Root-level implementation must not make every Leaf underneath it appear to
be an equally strong match.
The displayed percentage is a text-and-concept overlap estimate, not a claim
that the intentions are identical and not merge progress. It remains visible
until the user approves an update, creates separate work, or dismisses the
suggestion.
The user can then choose one of two paths:

- **Adapt this Root/Branch/Leaf** when the suggestion is a clearer framing,
  constraint, or next version of work already underway. The title and combined
  direction are editable, and submitting them creates one pending GoalAI update
  proposal. Repeated edits replace the older pending draft instead of stacking
  duplicate proposals.
- **Create separately** when the suggestion has a materially different audience,
  success condition, time horizon, or experiment despite sharing a theme.

Nothing changes during overlap review. After creating the proposal, the user
can approve it inline or inspect it in Growth first. Only approval updates the
same existing node in place, links the Investigation as
evidence, and marks the suggestion as tried. Because the node identity does not
change, its steps, completion, coaching transcript and summaries, evidence,
outcomes, Investigation links, and children remain attached. Rejecting it
leaves the node unchanged and keeps the separate-plan route available.

Separate work passes through a second semantic placement review before the
planning agent opens. Faerie receives a bounded catalog of active Root and
Branch paths and descriptions, recommends the most specific enduring owner,
and shows the complete proposed path for confirmation. Low-confidence routing
asks one placement question instead of defaulting to the Soul. A new Root is
available only when Faerie can name and explain a durable life domain that
should remain relevant after the temporary project ends; the project is then
created beneath that domain. The backend rejects unplaced planning requests and
unapproved Soul-level plans, so the UI cannot silently turn an Investigation
proposal into a project-shaped Root.

## Identity-preserving restructuring

Existing Growth nodes can be reclassified and moved without being deleted or
recreated. **Restructure** asks Faerie to review the selected node's ancestor
path and descendant subtree together against a bounded catalog of valid
parents. It recommends the smallest coherent set of type, parent, and
presentation-role changes with confidence and a plain-language reason. Manual
single-node type/parent selection remains behind **Adjust only this node**. The
review renders every current and proposed path plus counts of the records that
remain attached. Confirming it creates one pending GoalAI proposal; the tree is
still unchanged until that proposal is explicitly approved.

Nested `subgoal` records remain structurally valid, but the UI no longer labels
every level as a generic Branch. Queryable `goal_semantic_role` metadata
distinguishes **Area**, **Project**, and **Stage** (its rationale remains
encrypted) while leaving the four stored
node types (`umbrella`, `overgoal`, `subgoal`, `task`) intact. Older trees
get a deterministic derived label immediately; an approved AI review persists
the semantic roles it confirms.

Approval changes all reviewed `node_type`, `parent_id`, sibling-position, and
Branch-role records in one database transaction while preserving every node
ID. Descendants, completion states,
steps, Leaf Coach transcripts and summaries, outcomes, evidence, mastery,
Investigation links, GoalAI history, and origin provenance therefore remain on
the same records. The migration writes a durable restructure-history row,
requeues the moved subtree plus both ancestor paths, and marks older open
proposals from the affected context stale. Any validation or write failure
rolls the complete migration back.

## Recommended execution order

The map's geometry communicates hierarchy, not chronology. Active Leaves are
therefore also given a numbered recommended execution order within each Root.
The queue uses the plan's existing priority, due date, and planner position, in that order. The
same number appears on the constellation and in the focused action list so a
Root, its staging Branch, and the concrete Leaves beneath it cannot be mistaken
for duplicate simultaneous tasks.

## Leaf responsibility boundaries

Step drafting is Root-local and boundary-aware. GoalAI receives the selected
Leaf plus a bounded list of ordered peer Leaves in the same Root solely to
prevent duplicated work. It does not receive unrelated Roots, global memory,
main-chat history, passive capture, or screen activity. Every draft declares:

- the artifact or decision it receives from the preceding Leaf;
- the one output this Leaf owns;
- concrete steps that produce only that output; and
- any responsibility overlap with nearby Leaves.

Overlap recommendations distinguish shared subject matter from shared output.
GoalAI may recommend keeping Leaves separate, narrowing one boundary, or
creating an approval-only merge proposal. A merge never runs from the draft
itself. When approved, it retains Investigation/evidence links, moves children,
Leaf Coach messages and step resolutions, and experiment outcomes to the kept
node before archiving the absorbed node.

## Leaf Coach and upward execution context

Each explicit Leaf step can open a persistent **Leaf Coach**. This is an
execution helper, not the broad companion and not a structural GoalAI review.
Its prompt is limited to the Leaf, the selected step, directly linked
Investigation material, and the Soul/Root/Branch descriptions above it. It does
not receive sibling branches, global memory, the main-chat transcript, passive
capture, or screen activity.

The coach is suggestion-first for generative work: it supplies a concrete slate
of common candidates, templates, or example responses before asking the user to
remember or invent options. Reflection is used to evaluate and redirect those
AI-generated choices. Arbitrary timers are excluded unless the user supplied a
real time constraint. When redirection makes the stored **How to do this** list
obsolete, the coach can return an editable replacement step proposal. It changes
nothing until the user explicitly applies it. When the user explicitly reports
finishing the focused step, the coach asks permission to mark it complete. On
confirmation, the stored status and visible checklist update together and the
same Leaf conversation advances to the next unfinished step.

The encrypted raw coaching transcript stays on the Leaf. Compact working
updates—status, explicit blocker or constraint, selected approach, next action,
and completed-step resolution—flow only upward through that Leaf's Branch,
Root, and Soul. Parent GoalAI reviews consume those bounded rollups and are
marked dirty when a meaningful update changes. Siblings never receive them
directly; cross-branch reuse still goes through Soul harvest approval.

## How GoalAI-proposed investigations work

When a GoalAI agent proposes `start_curiosity` and you approve it:

1. Faerie creates a real Investigation.
2. It appears in the Investigations tab.
3. It attaches to the source Soul/Root/Branch/Leaf.
4. Its future answers dirty that node and its ancestors.
5. GoalAI sees the investigation output during the next review.
6. GoalAI can then propose actions, structure changes, evidence requests, or no
   action if the investigation only clarified understanding.

This means broad old curiosities like “Mental Health” or “Fitness” are usually
better represented as Roots or Branches. Sharper questions belong as
Investigations attached to those nodes.

## How self-classifying investigations work

An Investigation does not need to begin attached to a goal. If the user only has
a question or a messy current-state dump, start there.

Example:

> Why do I dread meeting new people/social interaction?

Faerie stores the dump as the Investigation seed, generates follow-up questions,
and explores first. Once enough evidence exists, use **Classify / place**. The
classifier may propose:

- attach to an existing node;
- create a missing Branch under an existing Root;
- create a new Root + Branch skeleton;
- create a Leaf if action is already clear;
- keep the Investigation Soul-level;
- keep investigating because there is not enough evidence yet.

Only approval applies the proposal. Classification is therefore:

```text
question → exploration → proposed placement → approval → goal/action structure
```

This keeps the user from needing to know upfront whether a question is about
Mental Health, Social Life, Energy Management, Work/Career, or something else.

Investigation context is rebuilt rather than frozen at creation time. Later
Soul Calibration answers enter the always-on core profile, and bounded recent
main-chat excerpts are selected when their wording is relevant to the
Investigation. Only user-authored chat is eligible; assistant replies are never
treated as user evidence. Question generation, classification, summaries, and
working-interpretation reviews all receive this refreshed context. A relevant
chat update or newer core-profile fact also makes an older approved synthesis
ready for review even when no new Investigation answer was added.

Documents can be attached directly to the whole Investigation or to one
specific question. PDF, DOCX, Markdown, and text-like files are extracted
locally and stored as encrypted, owner-scoped context. They inform future
questions and synthesis without being copied into the user's typed answer or
silently becoming a settled memory fact. The answer box can insert an explicit
filename reference such as `[Attached document: past-journals.pdf]`.

## Good patterns

### Broad domain

- Root: Physical Health
- Branch: Find sustainable exercise
- Investigation: “Why do I dislike repetitive gym exercise, and what movement
  formats feel intrinsically rewarding?”
- Possible Leaves: try climbing once, schedule a hike, test yoga, avoid gym-style
  repetition for now.

### Work anxiety

- Root: Mental Health
- Branch: Reduce daily start-anxiety at Parsons
- Investigation: “What threat does my brain perceive before work communication?”
- Possible outcomes: evidence note, memory candidate, coping Leaf, or a refined
  Branch description.

## Rule of thumb

Design from the Soul downward. Execute from the Leaves upward.

When something is foggy, create or approve an Investigation. When something is
clear enough to act on, let it become a Branch, Leaf, evidence note, memory, or
explicitly resolved understanding.
