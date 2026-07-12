# Current Agent Handoff

Generated automatically before commit. It contains Git metadata only, never
memory, OCR, clipboard, conversation, URL, screenshot, or window-title content.

## Start Here

- Branch: `main`
- Parent HEAD: `0d653e2`
- Generated UTC: `2026-07-12T19:59:50+00:00`
- Changed areas: agent-context, companion, diagnostics, filing, review, storage, triage
- Read the latest commit message, then run the narrow context command below.
- Do not scan README, FEATURES, design documents, or devlogs unless the task requires history.

## Changed Files In This Commit

- `FEATURES.md`
- `agent_window.py`
- `docs/GOALS_CURIOSITIES.md`
- `docs/INDEX.md`
- `docs/UPWARD_SPIRAL_IMPLEMENTATION.md`
- `docs/UPWARD_SPIRAL_PLAN.md`
- `docs/faerie-fire-upward-spiral-implementation.pdf`
- `gui.py`
- `livingpc/companion/brain.py`
- `livingpc/config.py`
- `livingpc/context_attachment.py`
- `livingpc/curiosity.py`
- `livingpc/curiosity_metrics.py`
- `livingpc/goal_ai.py`
- `livingpc/goals.py`
- `livingpc/inference.py`
- `livingpc/inference_scheduler.py`
- `livingpc/journal_filter.py`
- `livingpc/memory.py`
- `livingpc/memory_context.py`
- ...and 41 more; use `git show --stat -1`.

## Context Commands

- `python tools/project_context.py companion`
- `python tools/project_context.py diagnostics`
- `python tools/project_context.py filing`
- `python tools/project_context.py review`
- `python tools/project_context.py storage`
- `python tools/project_context.py triage`

## Verification

- `python tools/project_context.py all --verify`
- `python -m pytest -q`
- Test status is not inferred by the hook; verify before changing or shipping code.

## Recent History Before This Commit

- 0d653e2 Update 2026-07-11
- 1eb8c2c Faerie Fire - unified bilingual (EN/KO) launch build
