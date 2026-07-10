@echo off
REM Seed inference evidence from your already-captured history, and clear the
REM old pre-rework inference rows. Stop background capture first so the capture
REM DB isn't locked. Default: last 30 days. Use --days 0 for all history.
cd /d "%~dp0.."
echo Backfilling inference evidence from capture history...
python tools\backfill_inferences.py --reset %*
echo.
pause
