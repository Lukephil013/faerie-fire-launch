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

An **Investigation** is different because it persists over time. It owns a
specific unresolved question, generates follow-ups or suggestions, gathers
answers, and feeds those results back into the attached node.

In short:

- Review Node = “What is going on here?”
- Investigation = “Keep studying this unknown until it is useful.”
- Proposal = “Given what we learned, what should change?”

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
