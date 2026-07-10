@echo off
setlocal enabledelayedexpansion

REM ---------------------------------------------------------------------
REM New Test Instance — makes a brand-new, disposable copy of Faerie Fire
REM Launch in a fresh, timestamped folder on the Desktop, with NO existing
REM data (db, blobs, onboarding marker, stored API key) so it boots straight
REM into onboarding as if brand new. This folder (the one you ran this
REM script from) is never modified — only read from.
REM ---------------------------------------------------------------------

set "SRC=%~dp0.."
for %%I in ("%SRC%") do set "SRC=%%~fI"

for /f "delims=" %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%I"
set "DEST=%USERPROFILE%\Desktop\Faerie Fire Test %STAMP%"

echo Creating fresh instance:
echo   from: %SRC%
echo   to:   %DEST%
echo.

robocopy "%SRC%" "%DEST%" /E /NFL /NDL /NJH /NJS ^
  /XD data diagnostics .git .githooks __pycache__ .pytest_cache ^
  /XF tray.lock secret.salt .env *.pyc *.pyo

if not exist "%DEST%\gui.py" (
  echo.
  echo Something went wrong - gui.py was not found in the new folder.
  pause
  exit /b 1
)

echo.
echo Done. Fresh instance created at:
echo   %DEST%
echo.
echo Launching it now. This window stays open on purpose so you can see
echo any Python error directly, instead of it disappearing silently.
echo.
cd /d "%DEST%"

REM Use the same interpreter preference as Install Dependencies.bat. Plain
REM "python" may point at a different install with a newer pywebview, which
REM can recreate the WebView2 freeze this launcher is meant to avoid.
set "PY="
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if errorlevel 1 goto nopython
  set "PY=python"
)

for /f "delims=" %%V in ('%PY% -c "import sys;print(sys.executable)"') do set "PYEXE=%%V"
echo Interpreter: %PYEXE%
%PY% -c "import sys, importlib.metadata as m; v=m.version('pywebview'); parts=tuple(int(p) for p in v.split('.')[:2]); print('pywebview '+v); sys.exit(0 if (5,0) <= parts < (5,3) else 1)"
if errorlevel 1 (
  echo.
  echo Repairing pywebview for this interpreter before launch...
  %PY% -m pip install --force-reinstall "pywebview>=5.0,<5.3"
  if errorlevel 1 goto depfail
)

%PY% -c "import webview; print('GUI bridge import ok')"
if errorlevel 1 goto depfail

%PY% gui.py --view command-center

echo.
echo Faerie Fire closed.
pause
exit /b 0

:depfail
echo.
echo [X] The selected Python could not load the pinned pywebview bridge.
echo     Run "bats\Install Dependencies.bat", then try this launcher again.
pause
exit /b 1

:nopython
echo.
echo [X] Python is not on PATH. Install Python 3, then try this launcher again.
pause
exit /b 1
