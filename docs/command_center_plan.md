# Command Center — Plan

Status: Phase 0 + C0 implemented in the app; later workstreams remain planned.
Revision: v3.1, 2026-07-09. **Supersedes all earlier copies** (v1 had a
"Grove" Markdown mirror of the tree; v2 added the launch edition; v3 dropped
the file mirror — tree data lives only in the DB; v3.1 folds in codex review
round 2: Phase 0 scope cut, Self-utility relocation, tray/singleton stance,
concrete profile defaults, evidence-write boundary, proposal-store extension
rule, launch prompt honesty. If your copy has a `grove/` layout section, it
is two revisions stale.)

Three workstreams:

1. **A — Command Center**: the GUI's "Self" tab becomes the embedded
   companion chat (radar removed); the standalone companion window is
   retired and locked.
2. **B — AI tree management**: the main AI reads the Soul/Root/Branch/Leaf
   tree and manages it in chat through the *existing* GoalAI proposal store.
   No file mirror. DB is the single source of truth for everything
   tree-shaped.
3. **C — Launch edition**: a `profile` config flag gates the product down to
   Command Center + Investigations + a read-only tree viewer, for eventual
   public release from a fresh repo.

## Dropped: the Grove file mirror (decision, 2026-07-09)

v1 proposed mirroring each tree node as a Markdown file (like `projects/`).
Dropped because the tree never needed files — the AI needs read access, a
proposal pen, and a chat surface, and all three are DB-shaped. Dropping the
mirror deletes the entire sync class of edge cases v1 spent its length on
(concurrent editor changes, duplicate front-matter ids, deleted-file
resurrection, op journals for multi-file moves, plaintext exposure of
identity prose, case-fold slug collisions). `projects/` remains the only
AI-managed file surface. If tree export ever returns, it should be a
strictly read-only rendering, never an input.

What SURVIVES from the Grove design (these were never about files):

- **Authority tiers.** Node log/observation appends and AI-report updates
  are direct writes (to DB fields, undoable/regenerable); anything
  structural — create/reparent/archive, title/description/directive — is
  proposal → approval. Identity text moves only by proposal.
- **Confidence gates.** Below the gate the AI asks instead of proposing
  (filing's clarify-don't-guess; GoalAI's existing thresholds).
- **Catalog discipline.** The AI never receives "the whole tree": id +
  title + status + dirty flag, capped (`tree_catalog_max_chars`), full
  detail only for nodes under discussion (GoalAI's bounded per-node context,
  kept).

---

## Workstream A — Command Center tab

Replace the Self tab's radar (`memory.html` `.self-radar*`, `loadSelf()`,
`saveSelfPortraitPrefs` — remove, don't strand) with a chat pane bound to a
`Companion` instance in gui.py's bridge Api (lazy, like companion.py's).
Rename tab "Self" → "Command Center". The pane is a trimmed companion.html
chat column: no frameless header, no window buttons, no drag region. Brings
along wholesale: attachments (files + pasted photos), skills, /file offers,
reflections.

**Phase 0 scope, exactly** (a hydra wearing a cute little hat is not a
phase). Core: embed the chat pane; share ChatStore; send text; new/switch/
delete chats; attachments (file picker + image paste — the chips UI ports
with the pane). Free-riders included ONLY because they live in the reused
`Companion` brain, not the UI: /file offers, skill commands, filing
commands. Explicitly deferred past Phase 0 if they need more than trivial
UI: the reflection refine-box, /teach's code-block rendering polish.
Companion lock + tray retarget + profile flag ride along (small, and C0
needs the flag).

**Self-tab utilities are preserved, not nuked.** The Self tab holds more
than the radar: Soul Calibration, Database Rescue, daily energy/stats, today
tasks, priority/load guard, and Goals/Curiosities shortcuts. Disposition:

- Radar visualization + `saveSelfPortraitPrefs`: removed.
- Soul Calibration, Database Rescue: preserved — relocated to a compact
  utilities strip on the Command Center tab (or a "Tools" disclosure),
  personal profile only for Database Rescue.
- Today tasks / daily energy / priority-load guard: kept as compact side
  widgets beside the chat pane in v1 (cheapest), with chat commands
  (`/today` already exists) as the long-term direction.

**Standalone companion retired AND locked**: `companion.py` exits with a
pointer to the GUI unless `legacy_companion = true`. A lock, not a
suggestion — that is what actually deletes the two-brain race (two
processes with stale in-memory history caches).

**Tray retarget, concretely**: left-click opens the GUI *on the Command
Center tab* (gui.py gains a `--view command-center` arg / initial-tab
param); the right-click menu drops the standalone Companion entry;
`Companion.bat` gets a deprecation note. **Singleton stance for v1:**
launching a second GUI window is accepted (documented, harmless — same DB,
WAL) ; a focus-existing-window mechanism is a later nicety, not Phase 0.

**Radar data isn't orphaned**: mastery/metrics (curiosity_metrics) stay;
`/briefing` and Review Node surface them in prose; a `/radar` skill can
re-render numbers later if missed.

**Prompt caching carries over** (static/dynamic system blocks already
shipped in brain.py).

## Workstream B — AI tree management (DB-only)

The companion becomes the tree's primary interface:

- **Read**: `/tree` (catalog overview: roots, dirty nodes, pending
  proposals) and `/node <name>` (one node's full context: description,
  children, evidence summaries, open investigations, cached reports) — both
  straight from goals.py/curiosity.py stores. The Command Center system
  prompt's dynamic block gains a small tree-status section (bounded).
- **Write**: natural-language requests become proposals in the **existing
  GoalAI proposal store**, rendered in chat as a diff with approve/reject —
  exactly the /teach and triage pattern. The chat's approve calls the same
  apply function the Growth UI uses. **Extend the existing store if chat
  needs fields it lacks** (proposal source `command_center`, originating
  chat message id, target node version for stale checks, before/after tree
  preview payload) — never a parallel proposal table.
- **Nightly**: the existing GoalAI dirty-node pass continues unchanged; its
  proposals now ALSO surface in Command Center chat next morning (same
  records, second renderer).
- **Direct-write tier** (no approval) — with a sharp boundary on evidence:
  - Direct append is for **user-authored content only**: an explicit "save
    this to the node", a note the user dictates, an answer they give.
  - **AI-derived interpretations are never direct.** If the model infers an
    observation from conversation ("sounds like mornings are the blocker"),
    saving it requires at minimum a lightweight "Save as evidence?"
    confirmation in chat — inference-to-evidence is exactly where an
    unattended writer would quietly distort the tree.
  - Cached AI report refreshes stay direct (regenerable, clearly labeled as
    AI assessment).

### Edge cases — B

1. **One proposal path (invariant).** Chat proposals and GUI proposal cards
   are the same records, same apply function, same stale-version checks. A
   second apply implementation is a bug by definition.
2. **Stale approvals.** User approves in chat a proposal already
   approved/rejected in the GUI (or vice versa): apply is idempotent and
   version-checked; chat reports "already handled in the GUI" rather than
   double-applying.
3. **Ambiguous node references in chat.** "/node anxiety" matching two
   branches → the AI lists candidates and asks; never guesses on writes.
4. **Prompt size.** Tree grows unbounded; catalog capped; full node context
   only on request. (Carried from Grove design.)
5. **Chat-drafted structure quality.** A conversational "split this root
   into three branches" can produce sprawling proposals — proposals render
   as explicit before/after trees in chat, and multi-node changes are
   decomposed into individually approvable proposals (matches GoalAI's
   existing granularity).

## Workstream C — Launch edition (user-facing split)

Goal: a version others can run showing **Command Center**, **Investigations/
Curiosities**, and a **read-only tree viewer**. Inferences, Clarify, Memory,
Timeline, Import stay personal.

**Concrete config defaults** (currently none of these fields exist):

```toml
profile = "personal"        # 'personal' (default) | 'launch'
legacy_companion = false    # defaults false once Command Center ships
```

`launch` forces OFF: capture + tray capture, browser/clipboard collectors,
triage, Notion publishing, and the personal tabs (Inferences, Clarify,
Memory editing, Timeline, Import). It also adjusts the companion's prompt:
no lifecycle/screen context blocks, and the persona copy must never imply
ambient observation — a launch-profile Faerie has not "seen you today" and
must not talk as if it has. What context it DOES have (chat history, tree,
investigations, journals) is stated plainly in the system prompt so the
model doesn't confabulate a capture feed that isn't there.

**Split mechanism: `profile = "personal" | "launch"` config flag, one
codebase.** A hard fork during this churn means every change lands twice
(solo-dev killer); core-package extraction is premature. The public repo is
cut FRESH at publish time — this repo's git history contains personal
artifacts (Faerie_Fire_Dossier.pdf, overview PDFs, personal images) and a
personal Notion page id as a config.py default, so a fresh cut solves
de-personalization structurally instead of by history rewrite.

What user-facing drags in:

1. **First-run**: no memory.db, no tree, no key → onboarding (API key entry
   + validation, DPAPI storage via crypto.py, create-your-Soul moment, one
   seeded example investigation), empty states for every kept view.
2. **Cost transparency**: users bring their own key; llm_usage.py already
   tracks tokens/cost — surface it in settings.
3. **No screen capture in launch v1** (decision): chat, investigations,
   journals only; capture later as explicit opt-in. Profile flag gates
   collectors, tray capture, triage.
4. **Growth tab becomes a viewer in launch** (decision): users watch the
   tree change and grow, click nodes, see node info; every mutation and
   node-question happens in Command Center. Node inspector gains "Ask Faerie
   about this node" → jumps to chat with that node in context. Editing forms
   are personal-profile only. (Cleanly matches the one-proposal-path
   invariant: the viewer renders; chat is the only user-facing writer.)
5. **De-personalization**: personal defaults out of config.py
   (notion_parent_page_id, League app_thresholds, blocklist); tracked
   personal files moved out or into gitignored `personal/`.
6. **Data location**: launch defaults to `%APPDATA%\FaerieFire` (config.py
   path resolution is centralized — one change).
7. **Packaging** (last): PyInstaller onedir + installer; version/update
   story. Product name collision check before publish (still open).

## Invariants

- One proposal path (same records, same apply, same version checks).
- Phase 0 requires nothing from Workstream B.
- Identity text (title/description/directive/structure) moves only by
  proposal. Direct AI writes are limited to **user-authored** evidence/log
  appends and cached report refreshes; AI-derived interpretations require at
  least a "Save as evidence?" confirmation.
- Launch-profile Faerie never implies ambient observation; its prompt states
  exactly what context it has.
- `companion.py` locked once Command Center ships (`legacy_companion` opt-out).
- Launch profile never enables capture, collectors, or personal tabs.
- Chat content and tree content never enter diagnostics/context bundles
  (existing invariant, restated for the new surface).

## Phasing

- **Phase 0 + C0** — Command Center tab (rename, radar out, chat pane in,
  tray retarget, companion lock) + `profile` flag with tab gating. No tree
  changes. Quality bar is product-grade: this pane is the launch product's
  front door.
- **Phase 1** — read access: `/tree`, `/node`, tree-status block in the
  system prompt, "Ask Faerie about this node" from the (now read-only in
  launch) Growth viewer.
- **Phase 2** — write access: chat-created proposals through the GoalAI
  store, chat approval rendering, direct-tier evidence/report writes.
- **C1** — de-personalization + empty-state pass. **C2** — onboarding, key
  storage, %APPDATA%, cost panel. **C3** — packaging + fresh public repo.

## Decision log

- 2026-07-09 (Luke): standalone companion retired; Command Center is the one
  chat surface. Tiered authority from day one.
- 2026-07-09 (codex review, all points accepted): companion lock not just
  retirement; identity text DB/proposal-owned; no direct summary rewrites;
  strict phase decoupling; single proposal path as invariant; privacy
  hardening.
- 2026-07-09 (Luke): launch edition = Command Center + Investigations +
  tree viewer; no capture in v1; Growth tab read-only in launch with
  chat as the only writer.
- 2026-07-09 (Luke): **Grove file mirror dropped entirely** — tree stays
  DB-only; `projects/` remains the only AI-managed file surface.
- 2026-07-09 (codex review round 2, all points accepted): Phase 0 scope cut
  to an explicit core list; Self utilities (Soul Calibration, Database
  Rescue, today/energy/load widgets) preserved/relocated, not nuked; tray
  opens GUI on Command Center via initial-tab arg, second GUI window
  accepted in v1 (no singleton yet); concrete `profile`/`legacy_companion`
  defaults; direct evidence writes limited to user-authored content with
  AI-derived interpretations gated; extend (never parallel) the GoalAI
  proposal store; launch profile strips screen/lifecycle prompt context and
  forbids implied ambient observation. Verdict: ready to implement.
