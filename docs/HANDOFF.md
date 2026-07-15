# Current Agent Handoff

Generated automatically before commit. It contains Git metadata only, never
memory, OCR, clipboard, conversation, URL, screenshot, or window-title content.

## Start Here

- Branch: `main`
- Parent HEAD: `a0b5332`
- Generated UTC: `2026-07-15T23:30:17+00:00`
- Changed areas: agent-context, companion, filing, review, storage
- Read the latest commit message, then run the narrow context command below.
- Do not scan README, FEATURES, design documents, or devlogs unless the task requires history.

## Changed Files In This Commit

- `FEATURES.md`
- `README.md`
- `bats/Install Dependencies.bat`
- `companion.py`
- `gui.py`
- `livingpc/browser_assistant.py`
- `livingpc/companion/brain.py`
- `livingpc/companion/companion.html`
- `livingpc/companion/history.py`
- `livingpc/config.py`
- `livingpc/context_attachment.py`
- `livingpc/curiosity.py`
- `livingpc/goal_ai.py`
- `livingpc/ui/command_center_preview.html`
- `livingpc/ui/memory.html`
- `requirements-core.txt`
- `requirements.txt`
- `skills/upwork-profile-draft/SKILL.md`
- `tests/test_browser_assistant.py`
- `tests/test_command_center_preview.py`
- ...and 7 more; use `git show --stat -1`.

## Context Commands

- `python tools/project_context.py companion`
- `python tools/project_context.py filing`
- `python tools/project_context.py review`
- `python tools/project_context.py storage`

## Verification

- `python tools/project_context.py all --verify`
- `python -m pytest -q`
- Test status is not inferred by the hook; verify before changing or shipping code.

## Recent History Before This Commit

- a0b5332 Update 2026-07-14
- a4621ab Update 2026-07-14
- 8e012c9 Update 2026-07-13
