@echo off
REM Double-click to open the Faerie Fire GUI (Capture + Review + Memory + Schedule).
REM Uses pythonw so no console window lingers. If the window doesn't appear,
REM run "python gui.py" in a terminal to see the error.
cd /d "%~dp0.."
start "" pythonw gui.py
