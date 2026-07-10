# Database Lock Fix Plan

Recurring `OperationalError: database is locked` across the GUI, agent
windows, and GoalAI actions. Root cause: several processes (GUI, native agent
windows, tray service with inference/curiosity/nightly cycles) share
`memory.db` and `living_computer.db` through long-lived connections while
every database runs in SQLite's default delete-journal mode, where one writer
blocks all readers and one open reader blocks the writer. Per-field
encryption (`crypto.enc`) is unrelated — it runs in Python before SQL and
holds no locks.

## Phase 1 — WAL everywhere (the actual fix)

- New `livingpc/db.py` exposing `connect(path, *, timeout=30.0)`:
  `sqlite3.connect(path, timeout=timeout)`, then
  `PRAGMA busy_timeout=30000`, `PRAGMA journal_mode=WAL`,
  `PRAGMA synchronous=NORMAL`. Idempotent; WAL persists in the file.
- Replace every `sqlite3.connect` call site with the helper:
  `storage.py` (EventLog), `memory.py`, `goals.py`, `goal_ai.py`,
  `inference.py`, `curiosity.py`, `curiosity_metrics.py`, `clarify.py`,
  `companion/history.py`, `diagnostics.py`, `llm_usage.py`,
  `capture/extras.py` (temp copy — keep as-is), `collect_diagnostics.py`.
- Effect: readers and the writer no longer block each other. Only
  writer-vs-writer contention remains, absorbed by `busy_timeout`.

## Phase 2 — hygiene

- `backup.py`: connect via helper; run `PRAGMA wal_checkpoint(TRUNCATE)`
  before the copy and use the `Connection.backup()` API so `-wal` content is
  always captured.
- Confirm no store holds a write transaction across an LLM/network call
  (`chat_with_goal_agent` already commits each write before the model call —
  verified; audit inference_loop and curiosity the same way).
- `db_rescue.py` stays as a diagnostic, expected to become rarely needed.

## Phase 3 — guardrail

- Keep the GUI-side `withDbRetry`/`autoDatabaseRescue`; with WAL these become
  a backstop instead of a crutch.
- Optional later: single writer-retry (100–250 ms backoff, 2 attempts) inside
  `GuiApi` write endpoints.

## Verification

- New test: helper returns `journal_mode='wal'`; two connections from
  separate threads/processes interleave one long read with writes without a
  lock error.
- `python -m pytest -q` full suite (structural change).
- Manual: GUI open on Growth + agent window open + trigger GoalAI step
  drafting repeatedly; nightly backup while writing.
- `python tools/project_context.py all --verify` after adding `livingpc/db.py`
  (architecture/path change → update context manifest).

## Risks / notes

- WAL creates `-wal`/`-shm` sidecar files next to each DB; backup must
  checkpoint first (Phase 2 handles it). All DBs live on the local disk, so
  WAL's no-network-share caveat does not apply.
- The switch is persistent but reversible (`PRAGMA journal_mode=DELETE`).
- One-time migration happens transparently on first connect.
