@echo off
REM Double-click to launch Faerie Fire (Command Center + Growth).
REM Opens gui.py directly — no tray icon, no optional dependencies required.
REM First run walks you through onboarding (API key + naming your Soul) inside the app.
REM If the window doesn't appear, run "python gui.py" from this project folder in a
REM terminal instead, so you can see the error.
cd /d "%~dp0.."
start "" pythonw gui.py --view command-center
