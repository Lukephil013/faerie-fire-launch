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
| Leaf | One bounded outcome or action-and-learning cycle | Deliver, decide, experiment, practice, or reflect |
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
change, its workspace agreement and plan, completion, encrypted transcript and
confirmed summaries, evidence, outcomes, Investigation links, and children
remain attached. Rejecting it leaves the node unchanged and keeps the
separate-plan route available.

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

## Simple creation with semantic placement

The Soul offers seven optional starter Root archetypes—work, health,
relationships, learning, creativity, home, and resources. They are suggestions,
not a fixed taxonomy: the user explicitly chooses which to create, obvious
existing equivalents are not duplicated, and **New Root** remains available for
a durable domain that does not fit the catalog. Root is the only structural type
the user creates by name.

Below a Root, Area, Project, or Stage, the interface uses **Add something**.
The user describes the desired change in ordinary language; Faerie checks the
bounded Growth catalog for an existing equivalent, selects the most specific
valid owner, and classifies the addition as an Area, Project, Stage, Leaf, or—in
the exceptional durable-domain case—a new Root. Nothing is written during this
review. The result becomes a pending `create_child` proposal and is added only
after explicit approval. Approved Area/Project/Stage roles are persisted with
the created node. This keeps creation flexible without asking users to learn the
storage hierarchy or silently introducing duplicates.

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
the semantic roles it confirms. These roles are descriptive rather than a
required ladder: a Root may own an Area, Project, or Leaf directly, and a
Project may go straight to Leaves when no Stage adds meaning.
Within that flexible ladder, a **Stage** is an optional container for a distinct
phase that needs several Leaves, while a **Leaf** is one terminal outcome or
action-and-learning cycle. Most small Projects should go directly to Leaves;
Stages are useful for long projects whose research, build, launch, or follow-up
phases each need their own rollup. Stages can own Leaves; Leaves cannot own
children and are completed through the Leaf Agent. A nested
Stage → Stage relationship is accepted only when the proposed child includes
an explicit macro-stage/substage justification; otherwise intake and restructure
validation reject it rather than creating two indistinguishable phase levels.
The manual restructure form likewise presents **Root, Area, Project, Stage,
and Leaf** rather than the internal Branch label. Its destination list follows
the selected role, and an approved proposal persists both the move and the
Area/Project/Stage role atomically.

Every non-Soul node also has an explicit **Archive this node** action. Archive
first asks bounded GoalAI to distill attached context into a reviewable
knowledge handoff. The compact approved harvest flows upward through the
ancestor path; raw records remain attached to the archived subtree, and
cross-branch reuse still requires Soul routing approval. Archive is otherwise
a reversible subtree operation: the selected node and its descendants leave
the active map together, while their prior active/paused/completed states and
all attached evidence, Leaf workspace history, outcomes, and Investigation
links remain stored. The parent exposes the archived child under **Archived
history**; opening it provides **Restore this node**, which restores the
captured subtree states.
The constellation reinforces the distinction: Areas are larger violet nodes,
Projects are medium amber nodes, and Stages are smaller rose nodes in both the
tree and solar skins. Leaves remain the smallest mint execution points.

Approval changes all reviewed `node_type`, `parent_id`, sibling-position, and
Branch-role records in one database transaction while preserving every node
ID. Descendants, completion states,
workspace agreements and plans, encrypted transcripts and confirmed summaries,
legacy step-coach history, outcomes, evidence, mastery, Investigation links,
GoalAI history, and origin provenance therefore remain on the same records. The
migration writes a durable restructure-history row, requeues the moved subtree
plus both ancestor paths, and marks older open proposals from the affected
context stale. Any validation or write failure rolls the complete migration
back.

## Recommended execution order

The map's geometry communicates hierarchy, not chronology. Active Leaves are
therefore also given a numbered recommended execution order within each Root.
The queue uses the plan's existing priority, due date, and planner position, in that order. The
same number appears on the constellation and in the focused action list so a
Root, its staging Branch, and the concrete Leaves beneath it cannot be mistaken
for duplicate simultaneous tasks.

User-adjusted node coordinates plus each map surface's pan and zoom live in the
encrypted durable UI-preference store. Browser local storage remains only a
fast cache, so closing and reopening the full application does not reset the
arrangement.

## Leaf responsibility boundaries

Each Leaf owns one bounded outcome or learning cycle. Its workspace declares a
work mode so the same interaction is not forced onto every kind of Leaf:

- **Deliver or act** produces a concrete artifact or external change.
- **Decide** compares alternatives and records an explicit choice.
- **Experiment** runs a test and captures evidence plus a result.
- **Practice** repeats a behavior and reviews what is improving.
- **Reflect or record** turns experience into a confirmed observation or lesson.

The Leaf Agent receives the selected Leaf, its ancestor intent, directly linked
Investigation material, its approved agreement and plan, and its own encrypted
conversation. During a responsibility check, GoalAI may also receive a bounded
catalog of nearby Leaves solely to detect duplicated ownership. It does not
receive sibling conversations, unrelated Roots, global memory, main-chat
history, passive capture, or screen activity.

Overlap recommendations distinguish shared subject matter from shared output.
GoalAI may recommend keeping Leaves separate, narrowing one boundary, or
creating an approval-only merge proposal. A merge never runs from conversation
alone. When approved, it retains Investigation and evidence links, workspace
history, confirmed decisions and progress, legacy step resolutions, and
experiment outcomes on the kept node before archiving the absorbed node.

## Leaf Agent and upward execution context

The focused Leaf exposes one primary action: **Open Leaf Agent**. This opens a
persistent right-side adaptive workspace for the whole Leaf, rather than a chat
bound to one preselected checklist step. A new Leaf may open before a plan
exists. The workspace moves among three reversible phases:

1. **Shaping** — understand the desired outcome, offer relevant options, and
   incorporate corrections without manufacturing a checklist.
2. **Doing** — help execute the approved approach, answer normal follow-ups,
   and propose plan changes when new information invalidates the old plan.
3. **Reflecting** — capture what happened, evidence, blockers, decisions, and
   lessons; then offer completion for explicit confirmation.

The drawer keeps a compact **Current agreement** card containing the work mode,
outcome, approach, definition of done or review signal, confirmed constraints,
and approved plan version. It is the durable shared understanding behind
references such as “those,” “both,” or “the second one.” The user can move back
to shaping whenever the agreement no longer fits.

Documents can also be attached in the Leaf Agent composer. Supported files are
extracted locally, and their text is stored encrypted under that Leaf's
workspace. The model receives only bounded excerpts from the selected Leaf;
sibling Leaf documents never cross the workspace boundary. Raw files are not
copied into Faerie, and attachment text is treated as untrusted reference
material rather than instructions or an automatic memory fact.

Leaf Agent replies follow a reply-first contract. A normal conversational
message is always the primary result and remains visible even if optional
structured metadata is unusable. A response may additionally contain:

- suggestion cards with stable IDs, allowing one, several, all, none, or a
  free-form alternative;
- any number of mixed question blocks—single choice, multi-select, or text—with
  one shared submission button so related answers arrive as one conversational
  turn;
- an editable agreement or plan proposal;
- a plan-revision proposal with stable step IDs; or
- a completion or reflection proposal.

Malformed optional cards are omitted without throwing away readable prose.
Model failure produces an inline retry for that turn. There is deliberately no
semantic keyword fallback that replaces the conversation with a generic topic
menu such as “Great—automation.” If the agent cannot safely infer the user's
meaning, it asks one grounded clarification based on the active agreement and
recent turns. “I don't understand” explains the active choice; it does not
restart the Leaf.

Suggestions are AI-heavy and concrete when the user needs ideas, while
reflection evaluates and redirects them. Arbitrary timers are excluded unless
the user supplied a real time constraint. Steps appear only after a plan is
approved. Conversation can continue freely before, during, and after a plan;
it is never reduced to choosing a menu item.

Opening, chatting, selecting suggestions, or drafting changes never mutates the
Leaf. Agreement changes, revised plans, status transitions, and completion are
separate proposal cards and require explicit approval. Approved plans use
stable step IDs, so wording revisions do not detach progress or resolutions.

Separate native GoalAI, planning, inference, and harvest windows remain
available for broader bounded work. Their title bar is an explicit drag region,
so those popouts can be repositioned without moving Leaf execution out of the
main Growth workspace.

The encrypted raw workspace transcript stays on the Leaf. Compact confirmed
updates—phase, agreement, status, explicit blocker or constraint, selected
approach, progress, result, and lesson—flow only upward through that Leaf's
ancestor path. Parent GoalAI reviews consume those bounded rollups and are
marked dirty when meaningful confirmed context changes. Siblings never receive
raw turns or private working notes directly; cross-branch reuse still goes
through Soul harvest approval.

Migration preserves identity and history. Existing Leaf IDs, evidence,
outcomes, Investigation links, completion, and encrypted conversations remain
attached. Existing saved steps become a legacy plan version with stable IDs;
their completion and resolutions are retained. Legacy step-coach tables remain
available read-only during migration and are not deleted during the Leaf
Workspace v2 cutover. Leaves with an existing active plan begin in Doing;
Leaves without an approved plan begin in Shaping.

Leaf completion has one canonical review. The Leaf Agent prefills the confirmed
result and lesson from the conversation, with optional experiment details, and
the user edits or approves that proposal. Approval atomically marks the Leaf
complete, stores one `experiment_outcome`, links it as evidence, and preserves
the workspace transcript and plan history. The completed Leaf leaves the active
map and appears in **Completed Leaves** history as a compact result-and-lesson
receipt. The interface then opens the nearest next active Leaf. If none remains,
it returns to the parent and asks GoalAI to review that area for a next-Leaf
proposal. Reopening is explicit and reversible: it restores the same Leaf ID to
the active map without deleting its prior outcome, receipt, evidence, or chat.

Direct open Leaves use an adaptive two-step backend horizon. Projects have two
independent singleton attention signals: **Highest priority** and **Currently
working**. Each signal can belong to only one active Project globally. Only a
Project carrying either signal exposes its first canonical Leaf as **NOW** and
its second as **TENTATIVE NEXT** in the map and detail views; Areas, Stages,
Roots, and unattended Projects show no execution marker. Priority and due date
never create or reorder these labels. The Command Center chat can propose
setting, moving, or clearing either Project signal when the user asks; the
change remains pending until the user approves its card, just like other Growth
mutations. Paused Leaves still occupy their backend
slot, while completed and archived history do not. Pending AI proposals reserve
capacity alongside stored Leaves. Completing NOW produces one approval-gated
replan that promotes or rewrites TENTATIVE NEXT and may add one new tentative
next step. Replans cover every direct live Leaf,
are revalidated against current versions immediately before approval, and apply
the project framing plus all Leaf operations atomically.

Completion also prepares a project-local **Leaf handoff** when another active
Leaf exists in the Project's recommended execution order. A stronger, separately
configurable GoalAI route drafts the produced output, actual working material,
constraints, unresolved question, and suggested starting point. These fields are
editable and become durable only when the user approves completion. The encrypted
handoff is stored atomically with the outcome and is visible only to its explicit
destination Leaf and the existing ancestor rollups. The source transcript never
crosses. When the destination Leaf opens, its agent acknowledges the approved
handoff and continues from the transferred material instead of asking the user to
paste or reconstruct it. With no eligible destination, completion returns to the
Project review flow rather than inventing or silently creating work.

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
