@echo off
title Faerie Fire - one-time maintenance (cleanup + clean git history)
cd /d "%~dp0"
echo Close any open Faerie Fire test windows before continuing.
pause

echo [1/8] Clearing the stale git lock (left by an interrupted git command)...
if exist ".git\index.lock" ( del /f ".git\index.lock" & echo    cleared .git\index.lock ) else ( echo    no lock present )
git config core.hooksPath .githooks

echo.
echo [2/8] Archiving the current state (everything, pre-cleanup) to a safety branch...
git add -A
git commit -m "Pre-cleanup snapshot: unified bilingual build + this session's scratch files"
git branch -f archive-pre-cleanup
git push origin archive-pre-cleanup
if errorlevel 1 echo    (archive push failed - continuing; the branch still exists locally)

echo.
echo [3/8] Removing this session's dev-scratch files...
for %%F in ("port_korean_layer.py" "RUN PORT.bat" "port_result.txt" "debug_draft.py" "RUN DEBUG DRAFT.bat" "draft_debug.txt" "smoke_result.txt" "mini_test.py" "livingpc\ui\memory.html.pre-port.bak") do (
  if exist "%%~F" ( del /f "%%~F" & echo    removed %%~F )
)

echo.
echo [4/8] Removing personal-profile entry points the launch app never uses...
REM (libraries under livingpc\ stay - only standalone launchers/scripts go;
REM  agent_window.py stays: the app itself opens it.)
for %%F in ("assistant.py" "companion.py" "capture_control.py" "capture_status.py" "reset_capture.py" "run.py" "run_triage.py" "tray.py" "view_activity.py" "collect_diagnostics.py" "collect_companion_diagnostics.py" "push_to_github.bat" "living_computer_design.md") do (
  if exist "%%~F" ( del /f "%%~F" & echo    removed %%~F )
)

echo.
echo [5/8] Removing bats for features that do not exist in this profile...
for %%F in ("Ask Assistant.bat" "Backfill Inferences.bat" "Capture Control.bat" "Collect Companion Diagnostics.bat" "Collect Diagnostics.bat" "Companion.bat" "File Idea.bat" "Import Journals.bat" "Living Computer.bat" "Memory GUI.bat" "Reset Capture.bat" "Setup Background Capture.bat" "Start Background Capture.bat" "Stop Background Capture.bat" "View Activity.bat") do (
  if exist "bats\%%~F" ( del /f "bats\%%~F" & echo    removed bats\%%~F )
)
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "FaerieFire-Capture" /f >nul 2>nul && echo    removed the FaerieFire-Capture login autostart registry entry

echo.
echo [6/8] Removing historical design docs and prototypes...
for %%F in ("docs\command_center_plan.md" "docs\growth_tree_plan.md" "docs\filing_plan.md" "docs\inference_engine_plan.md" "docs\db_lock_fix_plan.md" "prototypes\growth_tree_prototype.html") do (
  if exist "%%~F" ( del /f "%%~F" & echo    removed %%~F )
)
if exist "prototypes" rd "prototypes" 2>nul

echo.
echo [7/8] Removing the stray API-key file everywhere, then the disposable test folders...
for /d %%D in ("%USERPROFILE%\Desktop\Faerie Fire Korean" "%USERPROFILE%\Desktop\Faerie Fire Test*") do (
  if exist "%%~D\APisk-ant-*.txt" ( del /f "%%~D\APisk-ant-*.txt" & echo    removed key file from %%~D )
)
for /d %%D in ("%USERPROFILE%\Desktop\Faerie Fire Test*") do (
  rd /s /q "%%~D" 2>nul && echo    deleted %%~D || echo    [!] could not fully delete %%~D (a window from it may still be open)
)

echo.
echo [8/8] Starting main over as ONE clean commit (old history stays on archive-pre-cleanup)...
git checkout --orphan clean-start
git add -A
git commit -m "Faerie Fire - unified bilingual (EN/KO) launch build"
git branch -M clean-start main
git push -f origin main
if errorlevel 1 (
  echo    [!] Force-push failed. Your local main IS the clean single commit;
  echo        fix the connection and run: git push -f origin main
)

echo.
echo Done. GitHub main = one clean commit. Full old history = branch
echo "archive-pre-cleanup" (local + pushed). Korean folder NOT deleted -
echo retire it yourself once you trust the unified build. If that API key
echo was real, rotate it at console.anthropic.com.
echo This maintenance script now deletes itself.
pause
(goto) 2>nul & del "%~f0"
