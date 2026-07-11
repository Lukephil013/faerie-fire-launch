@echo off
REM Memory hygiene pass: merge duplicate facts (newest kept; older copies are
REM closed, never deleted) and prune stale rejections/evidence. The nightly
REM pass does this automatically. Pass --dry-run to preview, --report for sizes.
cd /d "%~dp0.."
python tools\consolidate_memory.py %*
echo.
pause
