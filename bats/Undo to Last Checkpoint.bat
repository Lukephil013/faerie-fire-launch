@echo off
setlocal

REM ---------------------------------------------------------------------
REM Undo to Last Checkpoint — reverts every tracked file back to the last
REM "Save Checkpoint.bat" commit (or the last real commit if you've never
REM run a checkpoint). Use this any time a change breaks something and you
REM want it undone without waiting on anyone.
REM
REM Your data\ folder, config.toml, and anything else in .gitignore are
REM never touched by this. This resets files git already knows about; it
REM does NOT delete new files that were never checkpointed.
REM ---------------------------------------------------------------------

cd /d "%~dp0.."

echo This will undo any code changes made since your last checkpoint.
echo Your data folder and personal files are not touched.
echo.
set /p CONFIRM="Type YES to continue: "
if /i not "%CONFIRM%"=="YES" (
  echo Cancelled - nothing changed.
  pause
  exit /b 0
)

git reset --hard HEAD

echo.
echo Done - back to the last checkpoint.
pause
