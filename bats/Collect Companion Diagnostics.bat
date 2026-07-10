@echo off
title Faerie Fire - Collect Companion Diagnostics
cd /d "%~dp0.."
echo This captures three cropped images of the Faerie Fire companion window.
echo The rendered conversation is included in JSON and in the window images.
echo No database, OCR text, clipboard data, or full-desktop image is included.
echo.
echo Leave the companion visible and unobscured, then press any key.
pause >nul
python collect_companion_diagnostics.py
echo.
pause
