@echo off
REM Daily backup: stage everything (data is excluded by .gitignore), commit
REM with today's date, and push. Run after a day's work.
cd /d "%~dp0.."

where git >nul 2>nul
if errorlevel 1 ( echo Git not installed. & pause & exit /b 1 )

REM Keep the tracked agent handoff current for every commit, even if setup was old.
git config core.hooksPath .githooks

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set dt=%%I
set TODAY=%dt:~0,4%-%dt:~4,2%-%dt:~6,2%

git add .
git commit -m "Update %TODAY%"
git push
echo.
echo Pushed (if a remote is configured). If push failed, run "Git Setup.bat"
echo and follow the GitHub steps first.
pause
