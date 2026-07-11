@echo off
REM One-time git setup for Faerie Fire. Run on Windows (files intact here).
REM Requires Git for Windows installed: https://git-scm.com/download/win
cd /d "%~dp0.."

where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed. Install Git for Windows first:
  echo   https://git-scm.com/download/win
  pause & exit /b 1
)

REM ensure a commit identity exists (fallback if you haven't set one globally)
git config user.name >nul 2>nul || git config user.name "Living Computer User"
git config user.email >nul 2>nul || git config user.email "living-computer@example.com"
git config core.hooksPath .githooks

if exist ".git" (
  echo Git repo already initialized here.
) else (
  git init
  git branch -M main
  git add .
  git commit -m "Initial commit: Faerie Fire"
  echo.
  echo Local repo created. The .gitignore keeps your databases, screenshots,
  echo secret.salt, logs and config.toml OUT of git ^(data stays private^).
)

echo.
echo Agent handoff hook enabled from .githooks.
echo.
echo To back up to GitHub:
echo   1. Create an EMPTY private repo at https://github.com/new
echo   2. Run these two commands ^(paste your repo URL^):
echo        git remote add origin https://github.com/YOURNAME/living-computer.git
echo        git push -u origin main
echo.
echo After that, use "Git Push.bat" to back up your daily changes.
pause
