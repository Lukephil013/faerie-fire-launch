@echo off
setlocal

set "REMOTE_URL=https://github.com/Lukephil013/faerie-fire-launch.git"

cd /d "%~dp0"
echo Faerie Fire Launch - Git setup and push
echo Working in: %CD%
echo.

rem --- clear a stale lock file if one is sitting around ---
if exist ".git\index.lock" (
    echo Removing stale .git\index.lock ...
    del /f /q ".git\index.lock"
)

rem --- init the repo if it isn't one yet ---
if not exist ".git" (
    echo Initializing git repo...
    git init
) else (
    echo Git repo already exists.
)

rem --- make sure this repo has a name/email to commit as ---
git config user.name >nul 2>&1
if errorlevel 1 git config user.name "Luke"
git config user.email >nul 2>&1
if errorlevel 1 git config user.email "ltphilips013@gmail.com"

rem --- standardize on main ---
git branch -M main

rem --- point origin at your GitHub repo ---
git remote get-url origin >nul 2>&1
if errorlevel 1 (
    echo Adding remote origin...
    git remote add origin %REMOTE_URL%
) else (
    echo Updating remote origin url...
    git remote set-url origin %REMOTE_URL%
)

rem --- make sure the handoff-generating hook is wired up ---
git config core.hooksPath .githooks

echo.
echo Staging files...
git add -A

rem --- regenerate docs\HANDOFF.md from what's staged (same thing the
rem --- pre-commit hook does; run it explicitly so it's guaranteed even
rem --- if hook execution misbehaves on this machine) ---
echo Updating docs\HANDOFF.md...
python tools\update_handoff.py --staged 2>nul
if errorlevel 1 py tools\update_handoff.py --staged
git add docs\HANDOFF.md

git diff --cached --quiet
if errorlevel 1 (
    echo Committing...
    git commit -m "Initial commit"
) else (
    echo Nothing new to commit.
)

echo.
echo Pushing to origin/main...
git push -u origin main

echo.
echo Done. If this is the first push, Windows may pop up a browser or
echo credential prompt to sign in to GitHub - use your GitHub username
echo and a Personal Access Token (not your account password).
pause
