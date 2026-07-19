@echo off
REM Validate and restore a portable Faerie Fire backup. Close Faerie Fire first.
cd /d "%~dp0.."
py -3 tools\backup_instance.py restore %*
if errorlevel 1 python tools\backup_instance.py restore %*
pause
