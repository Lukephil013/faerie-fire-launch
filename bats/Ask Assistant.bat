@echo off
REM Double-click to start the real-time assistant. It runs in the background;
REM press your hotkey (default Ctrl+Shift+Space) anywhere to ask a question.
cd /d "%~dp0.."
start "" pythonw assistant.py
