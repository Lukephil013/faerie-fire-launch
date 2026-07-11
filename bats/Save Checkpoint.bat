@echo off
setlocal

REM ---------------------------------------------------------------------
REM Save Checkpoint — commits the current state of the code as a restore
REM point. Run this whenever things are working, before trying something
REM risky. Pairs with "Undo to Last Checkpoint.bat".
REM
REM Only tracked/trackable files are touched — data\, config.toml, secrets,
REM and anything else listed in .gitignore are never included or affected.
REM ---------------------------------------------------------------------

cd /d "%~dp0.."

for /f "delims=" %%I in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm:ss\""') do set "STAMP=%%I"

git add -A
git commit -m "checkpoint: %STAMP%"

if errorlevel 1 (
  echo.
  echo Nothing new to save right now ^(or git isn't set up yet in this folder^).
) else (
  echo.
  echo Checkpoint saved: %STAMP%
)

pause
