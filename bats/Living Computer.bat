@echo off
REM Double-click launcher for the full Living Computer/Faerie Fire desktop app.
REM Starts the tray daemon if it is not already running; the tray lock prevents duplicates.
cd /d "%~dp0.."
start "" pythonw tray.py
