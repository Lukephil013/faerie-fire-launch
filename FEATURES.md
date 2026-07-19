# Faerie Fire — Feature Reference

This is the current behavior reference. See `docs/INDEX.md` for documentation
authority.

> **Launch-distribution note (2026-07-11):** this folder is the unified
> bilingual (EN/한국어) launch build. Personal-profile entry points and bats
> (capture daemon/tray, companion window, assistant, triage/journal runners,
> diagnostics collectors) were removed from this distribution — the library
> code under `livingpc/` remains, and everything removed lives in git history
> and in the separate personal install. Sections below describing those
> features apply to the personal profile, not this folder's launchers.

A scannable map of everything the system does and the details worth remembering.
Update this when implemented behavior changes.

> **What it is:** a local-first "second brain." It watches your screen, distills
> what you do into a curated, time-aware memory you approve, then uses relevant
> memory to give you real-time guidance. Local data + a cloud LLM (Claude)
> for the thinking.

---

## 1. Capture (runs continuously)
What it does: records your activity into `data/living_computer.db`.

- **Active-window timeline** — logs which app/window is focused and for how long,
  grouped into "focus sessions." The lightweight backbone.
- **Screenshots + OCR** — grabs the screen on a smart cadence, extracts on-screen
  **text** on-device (RapidOCR), stores the text in the DB and the image in
  `data/blobs/`. The text is the durable part; images are disposable.
- **Sampler rules** (when it takes a screenshot): (1) skip if AFK >60s; (2) capture
  on window change; (3) capture if the screen changed "enough" (perceptual-hash
  distance > threshold); (4) heartbeat capture every `max_interval`. Tunable per app.
- **Browser history** — periodically scans Chrome/Edge/Brave/Firefox history
  (copies the locked DB to temp, reads new visits since a watermark) and logs
  visited page titles + URLs. Off-by-config: `browser_history_enabled`.
- **Clipboard** — logs copied text when it changes. **Skips capture while a
  blocklisted app (e.g. password manager) is in front**, and remembers the value
  so it can't leak after you switch away. Off-by-config: `clipboard_enabled`.
- **Blocklist** — apps in `config.blocklist` are never captured (loop pauses),
  and also gate clipboard capture.
- All collected text (OCR, browser, clipboard) flows into the same per-app
  summary at triage time and is encrypted at rest if a key is set.
- **Always-on background capture (NEW):** runs as a silent background app that
  **auto-starts at login** with a **system-tray icon** (`tray.py`) — green when
  capturing, grey when paused. Left-click opens the companion; right-click to
  pause/resume capture, open the review GUI, or quit. So the memory graph fills
  whether or not any window is open. A single-instance lock (`capture.lock`/
  `tray.lock`) prevents double-writing across the daemon, manual runs, and the GUI.
  Controls in `bats\`: **Setup Background Capture.bat** (once, registers login auto-start +
  starts it), **Start/Stop Background Capture.bat**, and **Capture Control.bat**
  for status ("last capture Ns ago"), logs, a safe **Reset**, and an emergency
  Force-Stop. The tray's **Pause** is the clean way to pause.
- Lock/stop files are anchored to the project folder (not the launch directory),
  so every process shares the same single-instance lock and stop signal.

## 2. Memory graph (the second brain) — `data/memory.db`
What it does: stores durable, time-aware facts about you.

- Each fact = subject + category + attribute + value + validity window.
- **Temporal supersession**: facts are never overwritten. When something changes,
  the old fact is closed out (`valid_to` set, status `superseded`) and the new one
  is linked via `supersedes_id` — so you keep the *trajectory*, not just the latest.
- View in the GUI **Memory** tab (tick "Show superseded" for history).
- **Explainable associations**: `livingpc/memory.py` proposes weighted links from
  category, attribute, value overlap, and supersession evidence. Association
  strength is separate from fact confidence. Links remain proposed until you
  approve them and rejected links are not silently proposed again.
- Approved associations are not yet used for prompt recall. Activation spreading
  will be added only after association review behavior is proven.

## 3. Triage (turns activity → memory)
What it does: once invoked, summarizes activity, asks Claude to propose memory
updates, and you approve them.

- **Mission**: the prompt's standing objective is to *study you deeply* — strengths,
  weaknesses, passions, values, fears, motivations — and ask forward, probing
  "why" questions (max 2/run). It's curiosity-first, not just logging.
- **Three outputs**: `statements` (new facts), `supersessions` (changes to an
  existing fact, by id), `questions` (curiosity / clarification).
- **Bounded memory context**: sends full values for the most relevant active
  memories plus a compact ID/category/attribute catalog of every active memory.
- **Incremental windowing**: summarizes only activity **since the last triage**
  (a watermark in `data/memory.db` `meta` table), so heavy days aren't truncated by the
  per-app summary cap and repeated runs don't overlap. `--full` forces a whole day.
- **Auto-commit only**: confident facts (>=`auto_commit_confidence`, default 0.75)
  are committed straight to memory; low-confidence facts and questions are dropped.
  The inference engine (not fact-triage) is how the system gets curious about you.
- **Redaction**: before anything is sent to Claude, secret-shaped strings (emails,
  card/phone numbers, API keys) are scrubbed. Topics aren't — use the blocklist
  for semantically private apps.
- **No self-observation loop**: Faerie Fire's own Python windows are excluded from
  triage summaries. Review cards and partially visible answer fields therefore do
  not come back as new evidence on the next generation.
- **LLM backend**: pluggable. `claude` (cloud, needs `ANTHROPIC_API_KEY`) by
  default; `stub` (offline, for testing). Set in `config.llm_backend` / `--backend`.

## 3b. Journal backfill (Notion -> memory, chronologically)
What it does: imports exported journals into the memory graph with facts dated
by their entries — the highest-signal seed data the brain can get.

- **Input**: `data/notion/` (gitignored) — markdown files with optional
  front-matter (`title`, `default_year`, `exported_at`) and entries delimited
  by date-marker lines ("06/16", "6/8", "04/05/2026"). Notion's native
  markdown export can be dropped in unchanged; more journals can be added
  anytime (the watermark makes re-runs resume, `--reset` redoes).
- **Chronological, not a dump**: monthly batches oldest-first; each proposed
  fact carries the date of the entry evidencing it, committed with that
  `valid_from`. Updates supersede (`as_of=date`), so trajectories build in
  order; near-duplicates and facts older than what's known are skipped.
  Undated standing notes run last, dated by the export date.
- **Models**: `ClaudeJournalModel` (Sonnet by default — deep read, run rarely;
  text is redacted first) and `StubJournalModel` (offline). Journal-specific
  prompt: identity/values/fears/goals over logistics, strict JSON, never
  editorializes.
- **Relevance pre-filter** (`livingpc/journal_filter.py`, zero API cost): before
  anything is sent, entries that are too short, near-duplicates (dedupe keeps
  the oldest copy), or mostly pasted advice (second-person coaching text scored
  against first-person insight markers) are dropped, URL-only lines removed,
  and huge entries trimmed head+tail. Raises signal density per batch AND cuts
  tokens — matters most on raw multi-year dumps. Stats printed per run;
  `--no-filter` bypasses.
- Run: `bats\Import Journals.bat`, or `python tools/import_journal.py`
  (`--dry-run`, `--backend stub`, `--month YYYY-MM`, `--reset`,
  `--min-confidence`, `--no-filter`). Run consolidation after a big import.
- Config: `journal_dir`, `journal_import_model`, `journal_min_confidence`,
  `journal_batch_max_chars`, `journal_filter_*`, `journal_entry_max_chars`.

## 3c. Filing engine (brain dumps -> living project documents)
- The prose counterpart to triage: you dump a paragraph or an essay (companion
  chat or CLI) and the model files it into per-project Markdown docs under
  `projects/` (gitignored personal prose).
- **Append-only by machine**: dated `###` entries under `## Log`, each with a
  `<!-- ff:entry <id> -->` marker so any filing is precisely undoable. The one
  sanctioned in-place edit is the doc's leading `>` summary blockquote. Hand
  edits anywhere are never touched. Dumps pass the triage redaction scrub
  before any LLM call; diagnostics log counts only.
- **Companion commands**: `/file <thought>` (or a long message -> gentle offer
  -> bare `/file`), `/undo <id>`, `/projects`. Filing errors degrade to an
  apologetic line, never a crash.
- **Below `filing_min_confidence` the model must clarify, not guess.** A
  multi-topic dump may split across projects; unknown targets become new docs.
- **Distill (approval-gated)**: `python tools/file_dump.py --distill <slug>`
  proposes a restructured doc, shows the diff, and applies only on explicit
  y/N — after saving a pre-distill copy to `projects/.history/`.
- **Nightly snapshot**: the daemon zips `projects/` into the rotating backup
  set beside the memory.db snapshots.
- Optional `filing_to_memory`: each dump is also saved journal-format to
  `filing_journal_dir` so `tools/import_journal.py --journal-dir` can feed the
  memory graph from the same inbox.
- Run: `bats\File Idea.bat`, or `python tools/file_dump.py "idea..."`
  (`--dry-run`, `--list`, `--undo <id>`, `--distill <slug>`, `--backend stub`).
- Backends: `claude` (default, Sonnet — filing decisions are the hard part),
  `stub` (offline), `ollama` (experimental local; `filing_ollama_*`).
- Config: `projects_dir`, `filing_backend`, `filing_model`,
  `filing_min_confidence`, `filing_auto_offer`, `filing_offer_min_chars`,
  `filing_to_memory`, `filing_journal_dir`, `filing_catalog_max_chars`.
- Design doc: `docs/filing_plan.md`. Code: `livingpc/filing.py`; area:
  `python tools/project_context.py filing`.

## 4. Anti-repetition
- **Supersession, not duplication**: triage prefers updating an existing fact
  (superseding it, keeping the trajectory) over adding a near-duplicate.
- **Inference re-hypothesis**: a rejected inference becomes a per-theme negative
  constraint, so the next synthesis forms a genuinely different claim rather than
  repeating a "no".

## 5. GUI (`gui.py` / Memory GUI.bat)
A pywebview app (`livingpc/ui/memory.html`) in the companion's design language —
navy radial gradient, glassy cards, cyan glow. Two views:
- **Inferences**: the Yes/Kind of/No/Skip review of bold hypotheses about you —
  only claims past the confidence gate — as animated cards with confidence bars
  and a collapsible "rewrite in your words" editor; a passive **Still forming**
  panel (live progress bars toward the gate, no questions), and a **What I now
  believe about you** panel (★ = core). "Run inference now" triggers a pass.
- **Teach-it feedback on No / Kind of** (`livingpc/feedback.py`): instead of a
  bare rejection, the card opens a dialogue — the model (Sonnet) asks up to 3
  sharp follow-up questions about why it's wrong / what's missing; you answer
  in free text (links like op.gg are kept as references; the app doesn't browse
  them, so paste key stats). The reply is distilled into a **lesson** stored per
  theme (`feedback_note` table) and injected into every future synthesis for
  that theme as an authoritative USER CORRECTION the next claim must honor.
  "Just record No" skips the dialogue (old behaviour).
- **Memory**: current facts grouped into collapsible category cards, with live
  **search**, a **Show history** toggle (superseded facts, dimmed +
  struck-through, with validity dates), and a fact counter.
- **Timeline**: your evolution in one view — every fact plotted at its
  `valid_from` date (newest first, grouped by month, glowing dots on a line);
  superseded facts show struck-through with a "→ revised <date>" marker, so
  supersession chains read as chapters of change.
- **Import**: a drop zone — drag .md/.txt/.docx journals onto the window and
  they're staged into `data/notion/` (.docx converted to text in-app via
  `livingpc/docx_text.py`, no dependency; legacy .doc gets a "save as .docx"
  message) (front matter auto-added with a chosen
  default-year; existing headers win; name collisions get suffixes). Shows
  staged files with entry counts + date ranges, the import watermark, a
  reset toggle (auto-suggested when staged entries predate the watermark), and
  Preview (dry run) / Import buttons that run the full chronological importer
  in-window. Nothing leaves the machine until you click import.
- Architecture: `gui.py` is now just the js_api bridge (`GuiApi`). Bridge calls
- **Owner-scoped document context**: Soul Calibration questions and
  Investigations can attach PDF, DOCX, Markdown, text, CSV, TSV, JSON, and log
  files. Text is extracted locally, encrypted in `memory.db`, and scoped to the
  exact calibration question, Investigation, or Investigation question. The raw
  file is not copied. Attachment chips can insert a filename reference into the
  answer box; removing a chip hard-deletes its extracted context. Long files are
  locally excerpted by relevance before model use, while the user's typed answer
  remains the exact text saved to memory.
- Architecture: `gui.py` is now just the js_api bridge (`GuiApi`). Bridge calls
  open their own stores per call (pywebview invokes js_api on worker threads;
  SQLite is thread-bound) and long calls block + return — Python never pushes
  into the page from a thread. Tkinter is gone.

## 5b. Real-time assistant (Phase 2) — `assistant.py` / Ask Assistant.bat
What it does: ask a question mid-activity and get an answer that sees your screen
and knows you.

- **Global hotkey** (default `Ctrl+Shift+Space`, set by `assistant_hotkey`) pops a
  small always-on-top box anywhere. Type, Enter, read. Esc hides it. The box is
  a frameless pywebview panel (`livingpc/ui/assistant.html`) matching the
  companion's look — draggable by its header, ✕ hides.
- On each ask it grabs your **current screen** (image), the **redacted on-screen
  text**, and up to 20 relevance-ranked **active memories**, and sends them to Claude Sonnet
  (multimodal). So it can reason about game state, items, what you're studying, etc.
- **Privacy guard**: if a blocklisted app is in front, the screenshot is NOT sent
  (it says so) — you still get a text + memory answer.
- Note: with vision on, the screenshot image does leave your machine for that one
  question. Cost is a few cents per ask. Runs as its own background app.
- Caveat: true-fullscreen games may hide the popup; borderless-windowed works.

## 5c. The Companion (Phase 2)
A lightly animated wizard raccoon on your desktop that knows you and talks back.

- **Character window** (`companion.py` / Companion.bat) — a frameless,
  always-on-top window with a wizard raccoon whose restrained idle, listening,
  thinking, and audio-reactive speaking motion reflects the current state. Built
  on `pywebview` (Edge WebView2). Drag it anywhere.
- **The brain** (`livingpc/companion/brain.py`) — each message is answered by
  Claude given the active persona + relevant memory + recent screen context. Knows you
  and sees what you're doing.
- **Guarded browser forms** (`livingpc/browser_assistant.py`) — an explicitly
  requested task opens a visible, separate Playwright Chromium profile. Each
  exact website requires approval; Faerie sends only ordinary form metadata to
  Claude, shows a field-by-field preview, and fills after a second approval.
  Save/Submit, credentials, MFA, payments, identity checks, uploads, and Upwork
  automation are not available. Approved websites can be revoked in Settings.
- **Personas** (`livingpc/companion/personas.py`) — switchable personalities:
  *Companion* (warm/curious), *Coach* (LoL tactical), *Gremlin* (playful roast for
  videos). Add your own via a `personas.json`. Each has a color that tints the face.
- **Voice (TTS)** — replies are spoken aloud (offline, via Windows SAPI / pyttsx3),
  and the character **pulses to the real audio** (Web Audio analyser, not a fake pulse).
  Mute toggle (🔊/🔇) in the window. Pluggable engine: `pyttsx3` default, `piper`
  optional for a nicer voice, ElevenLabs hook for later. `livingpc/companion/voice.py`.
- **Voice input (you speak to it)** — local **Whisper** (faster-whisper, GPU) hears
  you. Three ways in: the **🎤 push-to-talk** button (records a phrase, auto-stops on
  silence), a **👂 wake-word** toggle ("Hey Faerie ..."), and an optional **global
  hotkey** (Ctrl+Alt+F, needs the `keyboard` package). `livingpc/companion/ears.py`.
  Background voice results are polled by the UI (never pushed from a thread — keeps
  WebView2 stable).
- Still to come: **proactive commentary** + **auto-persona switching** (E–F).
- Config: `companion_backend`, `companion_model`, `companion_voice`,
  `companion_tts_engine`, `whisper_model`, `whisper_device`, `companion_wake_phrase`,
  `companion_ptt_hotkey`.

## 5d. Desktop notifications (`livingpc/notify.py`)
Dependency-free Windows toasts (WinRT via a hidden PowerShell call; best-effort,
never blocks or breaks a caller). Three triggers: **import / dry-run finished**
(so you can tab away from long imports — includes fact counts), **nightly
review reminder** ("N inferences ready for review", silent when the stack is
empty, fires with the 21:00 pass), and **graduation heads-up** when a new
hypothesis crosses the confidence gate mid-day. Config:
`notifications_enabled`, `notify_on_graduation`.

## 6. Nightly pass (in the daemon)
- The background daemon runs a nightly pass at `inference_nightly_hour` (default
  21:00 local): forms inferences, distils the day into **confident facts**
  (auto-committed; low-confidence facts dropped), **consolidates memory**,
  and creates the **legacy local memory.db checkpoint** — in that order, so the
  checkpoint captures the tidy state.
- This inference/hygiene pass needs no Windows Scheduled Task: keep the daemon
  running in the evening. Portable whole-profile backups have their own Windows
  task and app-startup fallback. `python run_triage.py --generate` triggers
  triage on demand.
- Only has data if capture ran that day.

## 7. Encryption (optional, at rest)
- Set `LIVINGPC_DB_KEY` (a passphrase) and the sensitive fields — OCR text and
  memory values — are encrypted on disk; app names/timestamps stay readable so
  grouping works.
- A random `secret.salt` is created next to the DBs. **Lose the key or salt and
  encrypted data is unrecoverable.** Without a key, nothing is encrypted.
- `python encrypt_db.py` migrates already-collected data after you set a key.

## 8. Maintenance
- **Screenshots auto-purge**: triage deletes images older than
  `config.blob_retention_days` (default 3); OCR text is always kept.
- **Legacy rotating memory checkpoints** (`livingpc/backup.py`): the nightly
  pass snapshots
  memory.db — the one irreplaceable file — into `data/backups/` via the SQLite
  online-backup API (safe while the daemon holds the DB open). Newest
  `backup_keep` (14) snapshots retained; `secret.salt` is copied alongside once
  and the automatic key remains DPAPI-bound to the current Windows identity.
  This is a local checkpoint, not full or cross-PC recovery. On demand:
  **bats\Backup Memory.bat** or `python tools/backup_memory.py`.
- **Portable encrypted whole-profile backups** (`livingpc/instance_backup.py`):
  online-snapshot both SQLite databases, scrub external authorization state and
  optional screenshot paths, vacuum the staged copies, collect portraits,
  projects/history, journals/dumps, personas, custom skills, and portable
  settings, then publish a verified `faerie-fire-*.ffbackup` through an atomic
  `.partial` rename. AES-256-GCM authenticates the compressed payload; Scrypt
  wraps a random repository key with the recovery passphrase while DPAPI caches
  only that random key for unattended runs. Credentials, browser profiles,
  diagnostics, logs, caches, and raw machine paths stay out. Screenshots are
  opt-in. Defaults retain 14 daily, 4 weekly, and 12 monthly generations.
- Portable backups run from a per-user Windows task (20:00 by default, start
  when next available), catch up when the GUI starts overdue, and retry hourly
  while open. Settings shows destination health and verified age; it also
  provides **Back up now** and whole-profile **Restore from backup**. Restore is
  available before API-key/Soul creation, validates into target-volume staging,
  re-protects the original automatic database key for the new Windows user,
  and requires a verified rollback before replacing a non-empty profile.
- Backup, restore, and explicit Forget share a cross-process maintenance lock.
  Forget advances the managed repositories' privacy epoch, purges primary and
  secondary generations, and creates a fresh baseline. Offline destinations
  remain purge-pending and blocked. Manually copied archives and cloud-provider
  version history are outside that revocation boundary. Full operational and
  recovery details: `docs/BACKUP_RECOVERY.md`.
- **Memory consolidation** (`livingpc/consolidate.py`) — the scale plan. The
  graph only grows (nightly auto-commits, evidence every ~25 min, rejections
  forever), so a nightly hygiene pass keeps it sharp: (1) active facts with the
  same category+attribute and same/near-identical values (token Jaccard >=
  `consolidate_value_similarity`, 0.85) are merged — newest survives untouched,
  older copies are *closed* like a supersession (never deleted, with a
  `consolidated_into` note); distinct values are never merged; (2) rejection
  rows older than 90 days are pruned (prompts only read the last 14); (3)
  inference evidence older than 180 days is pruned (0 disables). On demand:
  **bats\Consolidate Memory.bat**, `--dry-run` to preview, `--report` for a
  size snapshot.

## 9. Code backup (git) & history
- **bats\Git Setup.bat** (once) → local repo + GitHub instructions. **bats\Git Push.bat** →
  daily dated commit + push. `.gitignore` excludes all data/secrets.
- `devlog/<date>.md` — per-day change log. This `FEATURES.md` — the living
  capabilities map. Both committed each push.

---

## Commands cheat-sheet
```
bats\Capture Control.bat     # normal capture controls, status, and diagnostics
bats\Setup Background Capture.bat # one-time: always-on capture + tray, auto-start at login
bats\Start Background Capture.bat # start the background capturer
bats\Stop Background Capture.bat  # stop the background capturer
bats\Companion.bat           # the voice/character companion
bats\Memory GUI.bat          # the app: Inferences / Review / Memory / Schedule
bats\Backfill Inferences.bat      # seed inference evidence from captured history
bats\Ask Assistant.bat       # real-time assistant; hotkey Ctrl+Shift+Space
python assistant.py          #   (same, from a terminal)
python run_triage.py         # review pending; generate today's if none
python run_triage.py --generate   # non-interactive (used by scheduler)
python run_triage.py --full       # whole-day summary instead of incremental
python run_triage.py --backend stub --show-summary   # free dry run
python tools/backup_memory.py       # legacy local memory.db checkpoint
python tools/backup_instance.py status
python tools/backup_instance.py create
python tools/backup_instance.py scheduled
python tools/backup_instance.py restore <archive.ffbackup>  # securely prompts
python tools/backup_instance.py schedule install|status|remove
python tools/consolidate_memory.py  # hygiene pass; --dry-run / --report
python capture_status.py     # is it really capturing? (last-capture time)
python view_activity.py [--type browser|clipboard]   # browser + clipboard events
python check_llm.py          # verify Claude connection
python encrypt_db.py         # migrate existing data to encrypted
```

## Key config (`config.toml`)
```
tick / idle_limit / max_interval / default_threshold   # capture sampling
app_thresholds = {...}                                  # per-app sampling
blocklist = [...]                                       # never-captured apps
ocr_enabled = true
browser_history_enabled / browser_poll_seconds          # browser collector
clipboard_enabled / clipboard_poll_seconds              # clipboard collector
llm_backend = "claude" | "stub";  llm_model            # triage model
triage_memory_max_items / triage_memory_max_chars        # bounded proposal context
companion_memory_max_items / assistant_memory_max_items  # bounded live context
blob_retention_days = 3                                 # screenshot cleanup
backup_enabled / backup_dir / backup_keep                 # legacy local checkpoint
instance_backup_enabled / instance_backup_primary_dir / instance_backup_secondary_dir
instance_backup_hour / instance_backup_keep_daily / instance_backup_keep_weekly
instance_backup_keep_monthly / instance_backup_include_blobs  # portable .ffbackup
consolidate_enabled / consolidate_value_similarity        # nightly memory hygiene
consolidate_rejection_retention_days / consolidate_evidence_retention_days
db_path / memory_db_path / blob_dir                     # storage locations
```

## Environment variables
```
ANTHROPIC_API_KEY   # required for the cloud (claude) triage backend
LIVINGPC_DB_KEY     # optional; enables at-rest encryption
```

## Roadmap (not built yet)
- Local-only model option (Ollama) for triage + assistant.
- Proactive companion commentary and automatic persona switching.

## Skills — custom commands + self-extension (`skills/`, `livingpc/skills.py`)
- Drop a small `.py` into `skills/` and the companion gains a slash command.
  Two kinds: **prompt** (no code runs — a fixed system prompt over the chat
  backend) and **python** (`run(args, ctx) -> str`; ctx carries cfg, an `llm`
  callable, and the memory.db path). Broken files never crash the companion —
  they show in `/skills` with their error. `/skills reload` after editing.
- **/teach <idea>** — the model drafts a skill file, shows the FULL code in
  chat, and installs only on explicit `/teach approve` (previous version kept
  as `.bak`). Matches the house invariant: proposals pending until approval.
  Skill files run as ordinary Python with your privileges — read before
  approving. Installed skills are listed in the companion's system prompt so
  it can suggest them.
- **Built-in skills**: `/remind in 20m stretch` (also `at 5pm`, `tomorrow
  9am`, `list`, `cancel <id>` — fired as desktop toasts by the daemon's 30s
  poll, stored in memory.db), `/today [date]` (recap of a day's captured
  activity: aggregate -> redact -> one model call), `/briefing` (pending
  reminders + active goals + freshest project docs in one morning note), plus
  the on-demand `upwork-profile-draft` workflow, which prepares truthful
  field-by-field copy for the user to enter manually.
- Config: `skills_dir`, `reminders_enabled`, `browser_assistant_enabled`,
  `browser_assistant_profile_dir`.

## Recently shipped
- **Portable encrypted backup and recovery** — verified whole-profile
  `.ffbackup` generations, Windows scheduling plus startup fallback, staged
  whole-profile restore, rollback, and privacy-epoch-aware Forget handling.
- **Skills system** — user-extensible slash commands with approval-gated
  self-extension (/teach), plus /remind, /today, /briefing built-ins.
- **Filing engine** — brain dumps -> living project docs, with companion
  `/file` commands, undo, approval-gated distill, and nightly snapshots. See 3c.
- **Phase 2: real-time screen assistant** — built (hotkey popup, multimodal). See 5b.
- **Browser-history + clipboard collectors** — built. See section 1.
