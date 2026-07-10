@echo off
title Faerie Fire - Install Dependencies
cd /d "%~dp0.."

echo ============================================
echo   Faerie Fire - install / repair dependencies
echo ============================================
echo.
echo Installs each package INDEPENDENTLY so one that has no wheel for your
echo Python version (common on brand-new releases like 3.14) can't block the
echo rest. Core packages must succeed; optional ones (OCR/voice) are best-effort.
echo.

REM --- One interpreter for BOTH pip and launching. Prefer the py launcher. ---
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
%PY% -c "import sys;print('Python '+'.'.join(map(str,sys.version_info[:3])))"
echo.

echo Upgrading pip...
%PY% -m pip install --upgrade pip
echo.

echo ------------------------------------------------------------
echo [CORE] required packages (capture + companion window)
echo ------------------------------------------------------------
for /f "usebackq eol=# tokens=* delims=" %%P in ("requirements-core.txt") do echo. & echo Installing %%P & %PY% -m pip install "%%P"
echo.

echo ------------------------------------------------------------
echo [REPAIR] pywebview specifically - pip's "already satisfied" check
echo trusts installed metadata even if the actual package files are
echo missing or corrupted (this is what caused past freezes/crashes).
echo Force a clean reinstall every time to rule that out.
echo ------------------------------------------------------------
%PY% -m pip install --force-reinstall --no-deps "pywebview>=5.0,<5.3"
echo.

echo ------------------------------------------------------------
echo [OPTIONAL] extras - failures here are OK (feature just degrades)
echo ------------------------------------------------------------
for /f "usebackq eol=# tokens=* delims=" %%P in ("requirements-optional.txt") do echo. & echo Installing %%P & %PY% -m pip install "%%P"
echo.

echo ------------------------------------------------------------
echo [VERIFY] imports the tray icon + companion window depend on
echo ------------------------------------------------------------
%PY% -c "import PIL, pystray, webview; print('   [ok] PIL, pystray, webview (pywebview) all import')"
if errorlevel 1 goto verifyfail

echo.
echo ============================================
echo   Success - the tray icon and companion window will work.
echo ============================================
echo.
%PY% -c "import importlib.util as u; mods=['rapidocr_onnxruntime','pyttsx3','faster_whisper','sounddevice','keyboard']; [print('   optional',m,'->', 'installed' if u.find_spec(m) else 'MISSING (feature disabled)') for m in mods]"
echo.
echo Next:
echo   - Tray icon + capture:   "Start Background Capture.bat"
echo   - Companion window:      "Companion.bat"
echo   - Control panel:         "Memory GUI.bat"
goto end

:verifyfail
echo.
echo [X] A CORE package still won't import under %PYEXE%.
echo     Most likely pywebview's backend (pythonnet) had no wheel. Try:
echo        %PY% -m pip install "pythonnet>=3.1.0" pywebview
echo     If that fails, install Python 3.12 from python.org (best wheel
echo     coverage), then re-run this script.
goto end

:nopython
echo [X] Python is not on PATH. Install Python 3 from python.org
echo     (tick "Add python.exe to PATH"), then re-run this.

:end
echo.
pause
