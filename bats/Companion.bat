@echo off
REM Legacy standalone companion is retired. Use the GUI Command Center instead.
cd /d "%~dp0.."
start "" pythonw gui.py --view command-center
