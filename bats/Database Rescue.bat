@echo off
title Faerie Fire - Database Rescue
cd /d "%~dp0.."
echo Checking Faerie Fire databases...
echo.
python tools\db_rescue.py --unlock
echo.
echo If the result still says locked, close extra Faerie/Agent windows or restart
echo the app, then run this again.
echo.
pause
