# Faerie Fire Bootstrap

1. Read `docs/HANDOFF.md` first.
2. Run its narrow `tools/project_context.py` command.
3. Read target files only; avoid broad scans/history unless needed.

The tracked pre-commit hook regenerates `docs/HANDOFF.md`; `docs/INDEX.md`
defines documentation authority.

## Runtime Map

```text
tray.py -> livingpc/service.py -> capture -> data/living_computer.db + data/blobs/
gui.py/run_triage.py -> aggregate/redact/retrieve -> Claude/stub -> approval
inference_loop.py -> evidence -> claims past 80% -> gui.py Inferences review
nightly -> triage/consolidate/backup(data/backups)
companion.py / assistant.py -> recent screen + relevant memories -> Claude
```

- `tray.py` owns capture and runs one service thread.
- `capture.lock`, `tray.lock`, and `.capture_stop` are project-root anchored.
- Private data: `data/`, `diagnostics/`; launchers: `bats/`.
- `livingpc/config.py` owns typed defaults and project/data path resolution.
- `livingpc/memory_context.py` owns deterministic prompt-memory retrieval.

## Invariants

- Never expose private payloads in generic logs, context reports, or bundles.
- Blocklisted foreground apps are neither captured nor sent to an LLM.
- Raw triage stays local; cloud-bound summaries are redacted.
- Triage excludes Faerie Fire windows to prevent self-observation loops.
- Proposals remain pending until explicit approval. Rejections are not memories.
- Confidence, association strength, and activation are separate.
- Proposed associations never influence recall until explicitly approved.
- The triage watermark advances only after a successful model response.
- New generation replaces the prior pending batch.
- Prompt selection never edits, truncates, or deletes stored memories.
- Only explicit Forget deletes memory; consolidation never does.
- Prompt logs contain counts and estimates only, never prompt text or values.
- Claim scheduled cadence before work; failures wait for the next cycle.
- Keep process matching restricted to actual Python script arguments.

## Fast Commands

```powershell
python tools/project_context.py <area>
python tools/project_context.py all --verify
python -m pytest -q
python capture_status.py
```

Areas: capture, triage, companion, filing, review, storage, diagnostics. Reserve `all` for structural work.

## Working Preference

- Minimize usage: narrow inspection, combined edits, concise updates, focused tests.
- Before risky tracked-file restore/checkout/reset/broad rewrite/recovery, create a safety checkpoint first: prefer a small commit on a `codex/` branch when coherent, else `git stash push -u -m "safety before <operation>"` or save a patch. Never restore a large UI file from `HEAD` over uncommitted work without this checkpoint and an explicit reason.

## Handoff Discipline

- Pre-commit runs and stages `tools/update_handoff.py --staged`.
- Never put private payloads or secrets in the generated handoff.
- Commit messages carry human intent; the handoff carries git metadata plus
  context and verification commands.
- For architecture/path/invariant changes, update the context manifest and verify all.
