@echo off
REM Create a verified, encrypted full-profile Faerie Fire backup.
cd /d "%~dp0.."
py -3 tools\backup_instance.py create
if errorlevel 1 python tools\backup_instance.py create
pause
