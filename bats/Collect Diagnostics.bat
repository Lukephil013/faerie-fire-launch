@echo off
title Faerie Fire - Collect Diagnostics
cd /d "%~dp0.."
echo Creating a safe diagnostics bundle...
echo.
python collect_diagnostics.py
echo.
pause
