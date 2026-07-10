@echo off
REM Import journals (data\notion\) into the memory graph, oldest month first,
REM with facts dated by their entries. Resumes at the watermark on re-runs.
REM Useful flags: --dry-run (preview), --month YYYY-MM, --reset, --backend stub.
cd /d "%~dp0.."
python tools\import_journal.py %*
echo.
pause
