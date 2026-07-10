@echo off
REM Snapshot the second brain (memory.db) into data\backups\ (rotating set).
REM The nightly pass does this automatically; run this by hand before anything
REM risky. Pass --keep N or --dir "PATH" to override config.
cd /d "%~dp0.."
python tools\backup_memory.py %*
echo.
pause
