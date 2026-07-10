@echo off
REM Start always-on capture in the background now (no window).
REM Safe to double-click repeatedly — the lock prevents a second copy.
cd /d "%~dp0.."
start "" pythonw tray.py
echo Background capture started — look for the Faerie Fire icon in the system tray
echo (near the clock). Click it to open the Review GUI; right-click for Companion/pause/quit.
timeout /t 3 >nul
