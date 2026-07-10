# Filing Engine — Plan

Status: SHIPPED (all three phases, 2026-07-08). Kept as the design record; see
`FEATURES.md` section 3c for the implemented behavior and configuration.
Deviations from the plan: none of substance — `config.example.toml` did not
exist in the repo so config documentation lives in `FEATURES.md`; the Ollama
backend shipped in phase 1 rather than phase 3.

Original proposal follows. Adds the missing prose surface to
Faerie Fire: today the system turns *observed activity* into atomic facts; this
module turns *deliberate brain dumps* into living project documents. You open
the companion, type a paragraph or an essay about an idea, and it lands in the
right project doc — appended, dated, never destructive.

## The problem it solves

OneNote/Notion accumulate jumbled pages because *you* are the filing clerk.
Here the model is the clerk: one inbox (the companion chat), and the system
decides whether a dump updates an existing project doc, starts a new one, or
needs a clarifying question. The artifact is plain Markdown on disk — readable
by any editor and any future local model.

## Two kinds of writing (parallel to facts vs. inferences)

- **Project docs** (`projects/*.md`) — coherent prose per project/idea. The
  filing engine's product. Append-only by machine; freely editable by hand.
- **Memory facts** — the existing graph. A dump can *also* feed the journal
  import path so the memory graph learns from it, but that is secondary and
  configurable.

## Where docs live

- New top-level `projects/` directory (config `projects_dir`, resolved via
  `_project_path`).
- Added to `.gitignore` (personal content, same policy as `data/`). Safety net:
  the nightly pass snapshots `projects/` alongside the
  `memory.db` backups (`livingpc/backup.py`), same rotation.

## Document conventions

Each project doc:

```markdown
# Etsy SEO Automation
> One-paragraph summary the engine keeps current (the only block it may edit).

## Log
### 2026-07-08 — pricing idea            <!-- ff:entry 01J... -->
(appended dump, lightly cleaned)
```

- Machine writes are **append-only**: new `###`-dated entries under `## Log`,
  plus the summary blockquote (the single sanctioned in-place edit).
- Every appended entry carries an HTML comment marker with a ULID so a filing
  can be undone precisely (delete the marked block, nothing else).
- Hand edits anywhere are safe; the engine re-reads docs on every run and
  never rewrites existing prose.

## Pipeline (mirrors triage)

```text
dump text
  -> redact (reuse livingpc/triage/redact.py: scrub secret-shaped strings)
  -> catalog: for each projects/*.md — slug, title, summary block, headings
  -> filing LLM (strict JSON)
  -> apply: append entry / create doc / ask to clarify
  -> confirmation in chat (+ undo id)
  -> optional: forward raw dump into the journal-import path (memory facts)
```

### Filing LLM contract

New `livingpc/filing.py`, backend pattern copied from `livingpc/triage/llm.py`
(`SYSTEM_PROMPT` / `build_user_prompt` / forgiving `parse_response` /
`ClaudeBackend` + `StubBackend` / `get_backend(config)`).

Return STRICT JSON:

```json
{
  "filings": [ {"action": "append" | "create",
                "project": "slug-or-new-title",
                "section_title": "short label for the entry",
                "markdown": "cleaned entry text",
                "summary_update": "new summary blockquote or null",
                "confidence": 0.0 } ],
  "clarify": "question to ask instead, or null"
}
```

Rules baked into the prompt:

- Prefer appending to an existing project; create only when nothing fits.
- A multi-topic dump may split into several filings (one per project).
- Clean lightly (typos, paragraphing); never summarize away content — the
  user's words are the record.
- Below `filing_min_confidence`, return `clarify` instead of guessing.
- The stub backend files everything into `projects/inbox.md` so the whole
  pipeline runs offline in tests.

### Applier invariants

- Append-only under `## Log`; `summary_update` may replace only the leading
  blockquote. No other in-place edits, ever.
- Unknown slug from the model → treat as `create` (never write outside
  `projects_dir`; slugs are sanitized to a flat namespace).
- Every write logged to diagnostics as counts/chars only — never content
  (AGENTS.md logging invariant).
- Undo removes exactly the ULID-marked block; undo of a `create` deletes the
  doc only if the entry was its sole content.

## Companion integration

In `Companion.reply()` (`livingpc/companion/brain.py`), before the normal chat
path:

- **`/file <dump>`** — explicit command, routes to the filing pipeline.
  Reply: `Filed under **Etsy SEO Automation** → "pricing idea" (undo: /undo 01J…)`.
- **Auto-offer** — a plain message over `filing_offer_min_chars` (default
  ~600) gets a one-line offer appended to the normal companion reply:
  "Want me to file that into your projects? (/file last)". No modal, no nag;
  off via `filing_auto_offer`.
- **`/undo <id>`** and **`/projects`** (list docs + summaries) round it out.
- Filing runs on the chat thread's existing error discipline: any exception
  degrades to a normal chat reply, never a crash (matches current
  `reply()` try/except).

A `bats/File Idea.bat` launcher (prompt box → pipeline) covers non-chat use,
and a `tools/file_dump.py` CLI (`--dry-run` prints the proposed filing without
writing) matches the house pattern of dry-runnable tools.

## Config (defaults in `livingpc/config.py`, override in `config.toml`)

```toml
projects_dir = "projects"            # resolved relative to project root
filing_backend = "claude"            # 'claude' | 'stub'
filing_model = "claude-sonnet-4-6"   # filing decisions are the hard part; keep Sonnet
filing_min_confidence = 0.6          # below this: clarify, don't guess
filing_auto_offer = true
filing_offer_min_chars = 600
filing_to_memory = false             # also feed dumps through journal import
filing_catalog_max_chars = 8000      # cap on catalog sent to the model
```

## Privacy

- Dumps are user-authored and deliberately sent to the model — a *lower*
  privacy risk than triage's ambient capture — but they still pass through the
  existing redaction scrub for secret-shaped strings.
- Diagnostics/prompt logs record counts and estimated tokens only, never text.
- `projects/` is plaintext by design. Note in docs that
  at-rest DB encryption does not cover it; keep truly sensitive material in
  memory instead, or point `projects_dir` at an encrypted volume.

## Phases

1. **MVP** — `livingpc/filing.py` (catalog, prompt, parse, applier),
   `tools/file_dump.py --dry-run`, config keys, stub backend, tests.
2. **Companion surface** — `/file`, `/undo`, `/projects`, auto-offer,
   `bats/File Idea.bat`, nightly `projects/` snapshot.
3. **Distill (approval-gated)** — a periodic pass proposes a *restructured*
   version of a log-heavy doc; shown as a diff, applied only on explicit
   approval (the one sanctioned rewrite, consistent with "proposals remain
   pending until explicit approval"). Also: `filing_to_memory` wiring, and an
   `ollama` backend so filing can go local once quality allows.

## Tests (pure logic, offline)

- Catalog builder: titles/summaries/headings extracted, char cap respected.
- Parser: fenced/unfenced JSON, garbage → empty result (mirror triage tests).
- Applier: append idempotence, ULID markers, summary-only edit, sanitized
  slugs, undo exactness, never-touch-existing-prose property.
- End-to-end with stub backend into a temp `projects_dir`.

## Files touched

- New: `livingpc/filing.py`, `tools/file_dump.py`, `tests/test_filing.py`,
  `bats/File Idea.bat`, `projects/` (gitignored).
- Edited: `livingpc/config.py`, `livingpc/companion/brain.py` (command
  routing), `livingpc/backup.py` (snapshot), `.gitignore`,
  `config.example.toml`, `FEATURES.md` on ship.
