@echo off
title Faerie Fire - Reset Capture
cd /d "%~dp0.."
echo Resetting only Faerie Fire capture/tray Python processes...
echo.
python reset_capture.py
echo.
pause
