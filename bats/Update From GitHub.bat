@echo off
title Faerie Fire - Update From GitHub
cd /d "%~dp0.."

where git >nul 2>nul
if errorlevel 1 ( echo Git not installed - install it from git-scm.com first. & pause & exit /b 1 )

REM Clear a stale lock left by an interrupted git command (safe: only the
REM lock, never repo data). If git is genuinely running, close it first.
if exist ".git\index.lock" del /f ".git\index.lock"

echo Pulling the latest version...
git pull --ff-only
if errorlevel 1 (
  echo.
  echo [X] Pull failed. If you edited files locally, run "Git Push.bat" first,
  echo     or ask Luke. Nothing was changed.
  pause
  exit /b 1
)

echo.
echo Updated. If the app misbehaves after an update, run
echo "Install Dependencies.bat" once, then "RUN SMOKE TEST.bat" to check health.
pause
