# Goals and Investigations Walkthrough

This is a practical example of how to use the Soul/Root/Branch/Leaf tree with
Investigations and GoalAI.

The short version:

1. Put stable life domains in Goals.
2. Put unknowns/questions in Investigations by dumping your current thoughts
   into the journal box.
3. Let Faerie create the Investigation tab and follow-up questions from that
   current framing.
4. If you do not know where an Investigation belongs, leave it unattached.
5. Use **Classify / place** once it has some evidence.
6. Let GoalAI review nodes and propose changes.
7. Approve only the proposals that feel right.
8. Work from Leaves day to day.

## Example: finding exercise you actually like

Suppose the vague desire is:

> I want to get healthier, but I dislike most exercise and I do not know what
> kind I would actually stick with.

Do not make “Fitness” a broad Investigation. Fitness is too large and enduring.
It belongs in the goal tree.

### Step 1: Create the Root

Go to **Goals** and create:

- Root: `Physical Health`

Use this for the broad life area. It does not need to be perfectly planned yet.
The Root is just saying:

> This domain matters to my Soul.

### Step 2: Create a Branch for the specific direction

Under `Physical Health`, create:

- Branch: `Find sustainable exercise`

A Branch is more specific than the Root. It should describe a meaningful
direction, but it still may not be directly actionable.

Good Branch:

- `Find sustainable exercise`

Too broad for a Branch:

- `Be healthy`

Too small for a Branch:

- `Walk Tuesday at 5 PM`

That last one is a Leaf.

### Step 3: Use Review Node

Select `Find sustainable exercise` and click **Review node**.

GoalAI will audit the node and may notice things like:

- The node has no experiments yet.
- The system does not know what kinds of movement you enjoy.
- The main blocker is not discipline but fit.
- This needs investigation before building a plan.

This is what Review Node is for:

> “Given the current state of this node, what needs attention?”

### Step 4: Approve or refine a proposed Investigation

GoalAI may propose something like:

- Investigation: `Why do I dislike repetitive gym exercise, and what movement
  formats would I willingly repeat?`

If you approve that proposal:

1. It appears in the **Investigations** tab.
2. It attaches to `Physical Health › Find sustainable exercise`.
3. Its answers become context for that Branch.
4. Future GoalAI reviews can use the answers.

This is the key loop:

```text
Review Node notices uncertainty
        ↓
GoalAI proposes an Investigation
        ↓
You approve it
        ↓
Investigation asks questions / generates suggestions
        ↓
Answers become evidence
        ↓
GoalAI proposes better Branches, Leaves, or notes
```

### Step 5: Answer Investigation questions

In **Investigations**, you might see questions like:

- What kinds of exercise have felt boring or punitive?
- Have any forms of movement felt naturally rewarding?
- Do you dislike the physical sensation, the environment, the repetition, or the
  identity around exercise?
- When have you moved your body without needing discipline?

Answer these in plain language. You do not need to sound organized. The point is
to create evidence.

Example answer:

> I hate gym machines because they feel repetitive and pointless. Hiking feels
> better because there is scenery and a destination. I also like activities that
> feel skill-based, like climbing or martial arts, more than “burn calories”
> exercise.

### Step 6: Let the Investigation produce suggestions

After enough context, the Investigation might suggest:

- Try one scenic hike this week and rate how rewarding it felt.
- Try a beginner climbing session as a skill-based movement test.
- Avoid gym-machine routines for now because they appear to create resistance.
- Track “willingness to repeat” instead of calories burned.

Suggestions are not automatically tasks. They are proposals or experiments.

### Step 7: Turn useful suggestions into Leaves

If a suggestion feels useful, use **Implement** or let GoalAI propose a child
Leaf.

Possible Leaves:

- Leaf: `Take one scenic hike and rate willingness to repeat`
- Leaf: `Research beginner climbing gym options`
- Leaf: `Try one climbing session`

Leaves are where daily action lives.

### Step 8: Complete Leaves manually

When you actually do a Leaf, check it off manually.

Faerie can provide evidence, reminders, and reflections, but it should not
complete tasks for you automatically.

Completion answers:

> Did I do the action?

Mastery/evidence answers:

> What did this action reveal?

Those are different.

### Step 9: Review the Branch again

After a few Leaves or Investigation answers, review `Find sustainable exercise`
again.

GoalAI might now say:

- Hiking appears promising.
- Gym-machine routines should not be the default plan.
- Climbing is worth testing because it matches skill-based motivation.
- The next useful plan is a two-week experiment.

It may propose:

- New Branch: `Build a repeatable outdoor movement routine`
- New Leaf: `Schedule one hike for Saturday`
- New Leaf: `Try climbing once before deciding`
- Evidence note: `Skill-based movement appears more motivating than calorie-based exercise`

## Example: work dread

Start with a broad Root:

- Root: `Mental Health`

Create a specific Branch:

- Branch: `Reduce daily start-anxiety at Parsons`

Run **Review node**.

GoalAI may propose an Investigation:

- Investigation: `What threat does my brain perceive before work communication tasks?`

Answer questions as they appear.

Possible outputs:

- Evidence note: `Dread spikes around ambiguous communication, not all work.`
- Leaf: `Write a 3-sentence response draft before opening inbox.`
- Leaf: `List the feared consequence before starting communication tasks.`
- Branch refinement: `Reduce threat response around ambiguous work communication.`

The Investigation does not replace the Branch. It helps the Branch become
smarter.

## Example: social dread starts unclassified

Suppose the question is:

> Why do I dread meeting new people/social interaction?

You do not have to decide upfront whether this belongs under Mental Health,
Social Life, Energy Management, Work/Career, or the Soul.

### Step 1: Dump your current thoughts

Go to **Investigations** and use the large journal box.

Write the messy version first. For example:

> Why do I dread meeting new people/social interaction?
>
> The old idea that I am trying to prove I am superior does not feel true now.
> What feels more current is that I do not want to be trapped performing, I do
> not know what the person will expect from me afterward, and vague social
> contexts make me feel like I cannot find an exit.

Then click **Create investigation from journal**.

Because it is unattached, it shows as:

- `Soul-level investigation`

That does not mean it will stay Soul-level forever. It means:

> We are not forcing a category before we understand the mechanism.

The full dump is saved as the first answered seed item. The follow-up questions
should be based on this current framing, not on stale assumptions.

### Step 2: Answer the follow-up questions

Answer the Investigation's questions.

Example answers might reveal:

- the dread is strongest before the interaction;
- the fear is being trapped performing;
- vague duration makes it worse;
- the issue is not all people, but unclear expectation and obligation;
- once the interaction starts, it may become easier.

At this stage, do not create a social goal just because the topic sounds social.
Let the evidence point.

### Step 3: Click Classify / place

Once there is enough evidence, click **Classify / place** on the Investigation.

Faerie may propose one of several outcomes:

```text
Keep investigating
```

Use this if there is not enough evidence yet.

```text
Attach to existing node
Mental Health → Reduce social threat response
```

Use this if the right Branch already exists.

```text
Create Branch under existing Root
Mental Health
└─ Reduce social threat response
```

Use this if Mental Health exists, but the specific Branch does not.

```text
Create Root + Branch skeleton
Social Life / Connection
└─ Reduce dread around meeting new people
```

Use this if the Investigation reveals a distinct missing life domain.

```text
Keep Soul-level
```

Use this if the answer is mainly self-understanding and does not need a goal yet.

### Step 4: Approve the fitting proposal

If you approve a skeleton proposal, Faerie creates the nodes and attaches the
Investigation to the relevant node.

For example:

```text
Soul
└─ Mental Health
   └─ Reduce social threat response
      └─ Investigation: Why do I dread meeting new people/social interaction?
```

or:

```text
Soul
└─ Social Life / Connection
   └─ Reduce dread around meeting new people
      └─ Investigation: Why do I dread meeting new people/social interaction?
```

### Step 5: Let GoalAI turn understanding into action

After placement, run **Review node** on the new or attached Branch.

GoalAI may propose Leaves like:

- Define an exit condition before one social event.
- Do one low-stakes interaction with a clear time limit.
- Reflect afterward on whether the feared consequence happened.
- Separate social fear from social energy cost.

The order matters:

```text
question → evidence → classification → proposal → approved action
```

That prevents generic advice like “just socialize more.”

## When to create each thing

### Create a Root when:

- It is a major life domain.
- It will matter for months or years.
- It connects directly to the Soul.

Examples:

- Mental Health
- Physical Health
- Korean Fluency
- Work/Career
- Creative Work

### Create a Branch when:

- You know a direction but not every task.
- It is smaller than a Root but bigger than one action.
- It may contain Leaves, nested Branches, evidence, and Investigations.

Examples:

- Reduce daily start-anxiety at Parsons
- Find sustainable exercise
- Build Korean listening comprehension
- Move toward AI/LLM engineering work

### Create a Leaf when:

- It is an action you can manually complete.
- It is small enough to do or not do.
- It should count toward completion.

Examples:

- Apply to one AI support role
- Take one hike
- Review 20 Korean vocabulary cards
- Write one work-response draft

### Create or approve an Investigation when:

- There is an unresolved question.
- You need evidence before planning.
- The system keeps guessing without enough context.
- A pattern crosses multiple possible goals.

Examples:

- Why do I avoid starting work communication?
- What exercise formats would I willingly repeat?
- What makes Korean study feel alive instead of mechanical?
- How does tiredness affect my self-assessment?

## How to use Review Node

Use **Review node** when you want a GoalAI audit of one node.

Good times to use it:

- You just created a Root or Branch and want help shaping it.
- A Branch feels vague.
- You are stuck.
- You answered an Investigation and want GoalAI to update its strategy.
- There are proposals waiting and you want to refine them.

Review Node may produce:

- Questions
- Proposals
- Blockers
- Next focus
- Evidence requests
- Investigation proposals
- Child Branch/Leaf proposals

If Review Node asks a question that should persist over time, turn it into an
Investigation.

## How to use Investigations

Use **Investigations** like a research lab.

An Investigation should usually be phrased as a question:

- “Why do I…?”
- “What makes…?”
- “How does X relate to Y?”
- “What would prove…?”
- “What kind of X fits me?”

Avoid broad labels as Investigations:

- `Mental Health`
- `Fitness`
- `Career`

Those are Roots. Better Investigation versions would be:

- `Why does my mood dip after work communication?`
- `What movement formats feel rewarding instead of punitive?`
- `What roles match my AI/system-building strengths without trapping me in support work?`

## What should happen to old broad curiosities?

Broad legacy curiosities do not need to be deleted immediately.

Recommended cleanup:

1. Create or confirm the matching Root.
2. Attach the broad curiosity to that Root if it still has useful history.
3. Create sharper Investigations under specific Branches.
4. Archive the broad curiosity once the sharper Investigations replace it.

Example:

- Old curiosity: `Fitness`
- Root: `Physical Health`
- Branch: `Find sustainable exercise`
- New Investigation: `What exercise formats would I willingly repeat?`
- Archive `Fitness` once it no longer carries useful active questions.

## Daily usage pattern

Use this rhythm:

1. Open **Self** or **Goals**.
2. Look at the highest-priority Leaves.
3. Do 1–3 Leaves.
4. Check them off manually.
5. If you feel stuck, run **Review node** on the relevant Branch.
6. If the blocker is unclear, approve or create an Investigation.
7. At the end of the day, let the 8 PM cycle update changed paths.

## The most important rule

Do not force everything to become a task immediately.

Some things need to be understood before they become useful action.

But also do not let Investigations float forever. A healthy Investigation should
eventually produce one of these:

- A better Branch
- A concrete Leaf
- An evidence note
- A memory candidate
- A refined belief/inference
- A decision that no action is needed yet

Everything ultimately feeds the Soul.
