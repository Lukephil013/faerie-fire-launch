# Faerie Fire

Current documentation is indexed in [`docs/INDEX.md`](docs/INDEX.md). The original
design document and dated dev logs are historical context, not runtime truth.

*A local-first "second brain."* Faerie Fire is a background service that records
what you do on your PC, plus a daily triage pass that distills it into a curated,
temporal memory you approve fact-by-fact, with a screen-aware assistant and
voice companion built on top of that memory.

> Internal note: the Python package is still named `livingpc` and the project
> folder is still `Living Computer` — those are unchanged on purpose; only the
> product name is Faerie Fire.

Two parts:

1. **Capture** (`run.py`) — logs your active-window timeline and smart-sampled
   screenshots + OCR into a SQLite event log.
2. **Triage** (`run_triage.py`) — once a day, summarizes that activity, redacts
   secrets, asks an LLM to propose memory statements, and lets you approve /
   edit / reject each one into the memory graph (`memory.db`).

## What it captures

- **Active window timeline** — which app/window is in front and for how long,
  grouped into focus sessions. The lightweight backbone.
- **Screenshots + OCR** — captured on a smart cadence (see below), text
  extracted on-device with RapidOCR. Screenshots go to a rolling buffer
  (`blobs/`) and are meant to be purged after triage; the OCR **text** stays.

(Browser-history and clipboard collectors are specified in the design doc and
are the next collectors to add. Keystroke logging is intentionally **out** of
v1.)

## The sampler (why it doesn't drown you in frames)

Every tick (~2s) it checks four rules in order and only captures if one fires:

1. **AFK** (no input for 60s) → skip
2. **Window changed** → capture (event-driven, never missed)
3. **Screen changed enough** (perceptual-hash distance > threshold) → capture
4. **Heartbeat** (max_interval elapsed while active) → capture

Tune `default_threshold` and `max_interval` in config. Thresholds can be set
per-app (games dense, documents sparse).

## Install & run (Windows)

```bash
pip install -r requirements.txt          # app + AI + browser-assistant packages
python -m playwright install chromium    # one-time dedicated browser runtime
cp config.example.toml config.toml       # optional; edit knobs + blocklist
python run.py                            # starts capturing; Ctrl-C to stop
python run.py --no-ocr                   # store screenshots, skip OCR
python capture_status.py                 # how much it has captured
```

The Command Center browser assistant uses its own visible Chromium profile.
Faerie asks once per exact website, previews every field, fills only after a
second approval, and leaves Save/Submit to you. Passwords, MFA, payments,
identity checks, and Upwork browser automation are excluded.

In Command Center, type `/browser` to see the guided format, or create a task
directly with `/browser https://permitted.example/profile/edit | Title: Data
Analyst; availability: part time`. You can attach a text-readable résumé or
document instead of putting the information after `|`. Always use the real
edit-form URL; `example.com` is only a documentation placeholder.

The active-window + idle backend uses the Windows API (ctypes + psutil). On
non-Windows it falls back to a stub backend so the code still imports and the
tests run, but it won't capture meaningful window data there.

## Daily triage — building the second brain

After the capture loop has recorded a day, run triage to distill it into memory:

```bash
setx ANTHROPIC_API_KEY "sk-ant-..."   # once; reopen the terminal afterward
python run_triage.py                  # triage today with Claude
python run_triage.py --date 2026-06-24
python run_triage.py --backend stub   # offline dry run, no API key needed
python run_triage.py --show-summary   # also print the (redacted) text sent to the model
python gui.py                         # browse what the brain believes (Memory tab)
```

For each proposal you get `[a]pprove / [e]dit / [r]eject / [s]kip`. Supersessions
(updates to an existing fact) are shown as *was → now* so you can see the change.
Approved items land in `data/memory.db`; superseded facts are kept (closed out, not
deleted) so the trajectory survives.

It's worth a `--backend stub --show-summary` dry run first — it costs nothing and
shows you exactly what would be sent to the cloud.

## Double-click launchers (no terminal needed)

The `.bat` launchers live in `bats\` and run the Python for you. The main entry
point is **bats\Capture Control.bat**, which covers normal capture operations and
opens the review GUI.

- **Setup Background Capture.bat** — run once: registers always-on capture to
  auto-start at login and starts it now, with a **system-tray icon** (`tray.py`).
  Left-click the tray icon to open the companion; right-click to pause/resume.
  Controls: **Start/Stop Background Capture.bat**, or **Capture Control.bat** for
  status, logs, a safe reset, and an emergency force-stop.
- **Companion.bat** — the animated wizard-raccoon companion (talk to it; it knows you).
- **Memory GUI.bat** — the app: an *Inferences* tab for the Yes/No review of
  hypotheses about you (only claims past the confidence gate), and a *Memory* tab
  to browse what the brain believes. Or run `python gui.py`.
- **Backfill Inferences.bat** — seed inference evidence from already-captured history.
- **Import Journals.bat** — chronological backfill of exported journals
  (`data\notion\`) into memory, facts dated by their entries. `--dry-run` first.
- **Backup Memory.bat** — create the legacy local `memory.db` checkpoint in
  `data\backups\`. It also runs with nightly hygiene, but it is not a portable
  disaster-recovery backup.
- **Portable Backup.bat** — create a verified, encrypted whole-profile
  `.ffbackup` after portable backups have been configured in Settings.
- **Restore Backup.bat** — validate and restore a `.ffbackup` as a whole-profile
  replacement. Close the running app first when using this launcher.
- **Consolidate Memory.bat** — memory hygiene: merge duplicate facts (older
  copies closed, never deleted) and prune stale rejections/evidence; `--dry-run`
  previews. Also runs automatically every night.
- **View Activity.bat** — recent browser-history + clipboard events.

(A true single-file `.exe` is possible via PyInstaller, but it must be built on
Windows and rebuilt on every change. The `.bat` launchers give the same
double-click experience with no build step.)

## Automatic nightly pass

The background daemon (`tray.py`) runs a nightly pass at `inference_nightly_hour`
(default 21:00, local): it forms inferences, distils the day into **confident
facts** (auto-committed; low-confidence facts are dropped, not queued),
consolidates memory (dedupe + pruning), and creates the legacy rotating local
`memory.db` checkpoint. This nightly checkpoint is useful for small local
mistakes, but it is not sufficient to move the profile to another Windows
user or PC. No Windows task or manual approval is needed for this pass — just
keep Faerie Fire running in the evening. Run triage on demand with
`python run_triage.py --generate`.

## Portable encrypted backup & recovery

Configure portable backups under **Settings & Tools → Backup & Restore**. Pick
an absolute primary destination, optionally add a secondary external or
cloud-synced mirror, and save the recovery passphrase in a password manager.
There is no passphrase reset or backdoor. Once configured, Faerie Fire creates
verified encrypted `faerie-fire-*.ffbackup` generations at 20:00 by default,
uses a per-user Windows task that starts when next available, catches up on app
startup when overdue, and retries transient failures hourly while open. Use
**Back up now**, **Portable Backup.bat**, or
`python tools/backup_instance.py create` for an immediate generation.

A portable backup uses SQLite's online backup API for both databases and
contains the original automatic database key and salt inside the authenticated
encrypted payload, plus portraits, projects and history, journals and dumps,
personas, custom skills, and portable preferences. Credentials, browser state,
diagnostics, logs, caches, and machine-specific paths are excluded. Screenshots
are excluded by default while their activity/OCR records are preserved.

Restore is available before API-key/Soul setup in onboarding and under
Settings. It validates and stages the archive before replacing the whole
profile, re-protects the original database key for the destination Windows
user, and requires a verified rollback backup before replacing a non-empty
profile. Explicit Forget advances a repository privacy epoch and purges managed
generations; an offline destination remains visibly purge-pending and blocked.
Manually copied archives and cloud-provider version history must be removed
separately. See [Portable Backup and Recovery](docs/BACKUP_RECOVERY.md) for the
complete contents, exclusions, restore flow, and privacy boundary.

## At-rest encryption (optional)

Set a passphrase and the sensitive fields (OCR text + memory values) are
encrypted on disk; app names and timestamps stay readable so grouping works.

```bash
pip install cryptography
setx LIVINGPC_DB_KEY "a-passphrase-you-remember"   # reopen the terminal after
python encrypt_db.py        # one-time: encrypt data you already collected
```

From then on, capture and triage encrypt automatically; the view/triage tools
decrypt transparently. **Keep `secret.salt` (created next to the DBs) and your
passphrase safe — without both, encrypted data cannot be recovered.** Without a
key set, nothing is encrypted and everything works as before.

## Avoiding repeated proposals

- **Generate replaces, not stacks**: each "Generate today's proposals" clears the
  previous batch first, so re-running doesn't pile up duplicate cards.
- **Rejections are remembered (softly)**: when you Reject a proposal it's recorded
  and fed to the model next time as *"don't re-propose these specific items"* — but
  it's soft guidance, capped to recent entries (last ~25, 14 days), stored
  encrypted, and never blocks a genuinely new or differently-scoped fact. Wipe it
  anytime with **Forget rejections** in the GUI. **Clear pending** empties the
  current batch without recording rejections.
- Re-running triage on the *same day* will still surface similar facts — the input
  hasn't changed. The intended rhythm is once per day (the nightly scheduler).

## Code backups (git) & dev log

- **Git Setup.bat** (run once) initializes a local git repo and walks you through
  pushing to a private GitHub repo. **Git Push.bat** backs up your changes daily
  with a dated commit. `.gitignore` keeps all personal data and secrets out of git
  (databases, `blobs/`, `secret.salt`, logs, `config.toml`).
- `devlog/` holds a dated change log (one file per day, e.g. `2026-06-25.md`),
  committed alongside code so there's a running history of what changed.
- `FEATURES.md` is the living capabilities reference — every feature, what it does,
  and key details. Skim it to jog your memory. Updated on every commit.

## Privacy notes

- Everything stays local except the triage step: only the **redacted daily
  summary text** is sent to Claude — never screenshots, never the raw log.
- The redaction pass scrubs secret-SHAPED strings (emails, card/phone numbers,
  API keys). It does **not** understand sensitive *topics* — a private journal's
  page titles would still go out. For those, add the app to `blocklist`.
- `blocklist` apps are never captured (the loop pauses when they're in front).
- Screenshots are meant to be short-lived: `EventLog.purge_blobs()` deletes the
  images while keeping the OCR text.
- At-rest encryption is available for the sensitive fields (see above). Note it
  covers OCR text and memory values, not app names / window titles / timestamps;
  full whole-DB encryption (SQLCipher) is still a future option.

## Tests

Pure logic (sampler decision core, perceptual hash, storage, session grouping,
blob purge) is testable anywhere:

```bash
python tests/test_sampler.py
python tests/test_storage.py
python tests/test_memory.py
python tests/test_triage.py
# or, if you have pytest:  pytest -q
```

## Layout

```
tray.py                recommended capture owner + system tray
capture_control.py     capture status/control/diagnostics UI (pywebview)
gui.py                 Inferences / Memory UI (pywebview)
companion.py           pywebview companion entry point
assistant.py           hotkey screen assistant (pywebview popup)
run.py                 capture service CLI
run_triage.py          proposal generation/review CLI
bats/                  double-click Windows launchers
data/                  private databases + screenshot buffer (ignored)
diagnostics/           private logs and bundles (ignored)
tools/project_context.py  bounded agent map + token report
livingpc/
  config.py            typed config + TOML loader
  storage.py           SQLite event log + sessions + purge
  memory.py            temporal memory graph + supersession
  inference.py         inference store (evidence + hypotheses + gate)
  inference_loop.py    observe -> evidence -> synthesise -> graduate
  memory_context.py    deterministic bounded LLM memory retrieval
  ui/                  HTML front-ends for the pywebview apps (shared look)
  sysinfo.py           foreground-window + idle backends (Windows/stub)
  sampler.py           pure decide() + perceptual-hash helpers
  capture/
    window.py          WindowTracker -> focus sessions
    screen.py          ScreenCapturer -> screenshots + OCR
  triage/
    aggregate.py       day-of-events -> per-app summary
    redact.py          scrub secrets before the LLM
    llm.py             pluggable backend (Claude / stub) + prompt
    types.py           Statement / Supersession / Question / TriageResult
    pipeline.py        aggregate -> redact -> recall -> LLM
  service.py           the capture loop
tests/                 unit/integration tests
```

## Developer orientation

Coding agents should start with `AGENTS.md`, then request only the relevant map:

```powershell
python tools/project_context.py companion
python tools/project_context.py triage --tokens
python tools/project_context.py all --verify
```

See `FEATURES.md` for implemented behavior. See `living_computer_design.md` only
for original product intent and historical architecture.
