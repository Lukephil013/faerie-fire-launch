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

REM --- One interpreter for BOTH pip and launching. Prefer the py launcher,
REM     because the launch bats ("py -3"/"pyw -3") use it too. Installing with
REM     plain "python" while launching with "py -3" (or vice versa) can split
REM     packages across two different Python installs - the #1 cause of the
REM     black-window/frozen-app problem on multi-Python machines. ---
set "INSTALL_TRIED="
:detect
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

echo ------------------------------------------------------------
echo [CHECK] WebView2 Runtime (renders the app window)
echo ------------------------------------------------------------
set "WV2="
reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul && set "WV2=1"
if not defined WV2 reg query "HKLM\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul && set "WV2=1"
if not defined WV2 reg query "HKCU\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" /v pv >nul 2>nul && set "WV2=1"
if defined WV2 (
  echo    [ok] WebView2 Runtime detected.
) else (
  echo    [!] WebView2 Runtime NOT detected. The app window will stay black
  echo        without it. It ships with Windows 11 and most Windows 10 PCs;
  echo        if the window never renders on this PC, install it from:
  echo        https://developer.microsoft.com/microsoft-edge/webview2/
)
echo.

echo Upgrading pip...
%PY% -m pip install --upgrade pip
echo.

echo ------------------------------------------------------------
echo [CORE] required packages (capture + companion window + chat)
echo ------------------------------------------------------------
for /f "usebackq eol=# tokens=* delims=" %%P in ("requirements-core.txt") do echo. & echo Installing %%P & %PY% -m pip install "%%P"
echo.

echo ------------------------------------------------------------
echo [BROWSER] dedicated Chromium runtime for approved form filling
echo ------------------------------------------------------------
%PY% -m playwright install chromium
if errorlevel 1 goto verifyfail
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
echo [VERIFY] imports the app depends on
echo ------------------------------------------------------------
%PY% -c "import PIL, pystray, webview; print('   [ok] PIL, pystray, webview (pywebview) all import')"
if errorlevel 1 goto verifyfail
%PY% -c "import anthropic, cryptography; print('   [ok] anthropic (chat) + cryptography (key storage) import')"
if errorlevel 1 goto verifyfail
%PY% -c "import playwright; print('   [ok] Playwright browser assistant import')"
if errorlevel 1 goto verifyfail
%PY% -c "import importlib.metadata as m; v=m.version('pywebview'); parts=tuple(int(p) for p in v.split('.')[:2]); import sys; print('   [ok] pywebview '+v+' (pinned range)' if (5,0)<=parts<(5,3) else '   [X] pywebview '+v+' is OUTSIDE the pinned >=5.0,<5.3 range'); sys.exit(0 if (5,0)<=parts<(5,3) else 1)"
if errorlevel 1 goto verifyfail

echo.
echo ============================================
echo   Success - Faerie Fire will run on this PC.
echo ============================================
echo.
%PY% -c "import importlib.util as u; mods=['rapidocr_onnxruntime','pyttsx3','faster_whisper','sounddevice','keyboard']; [print('   optional',m,'->', 'installed' if u.find_spec(m) else 'MISSING (feature disabled)') for m in mods]"
echo.
echo Next:
echo   - Launch the app:        "Launch Faerie Fire.bat"  (first run = onboarding)
echo   - Tray icon + capture:   "Start Background Capture.bat"
echo   - Control panel:         "Memory GUI.bat"
goto end

:verifyfail
echo.
echo [X] A CORE package still won't import (or is the wrong version) under
echo     %PYEXE%.
echo     Most likely pywebview's backend (pythonnet) had no wheel. Try:
echo        %PY% -m pip install "pythonnet>=3.1.0" "pywebview>=5.0,<5.3"
echo     If that fails, install Python 3.12 from python.org (best wheel
echo     coverage), then re-run this script.
goto end

:nopython
if defined INSTALL_TRIED goto installfailed
set "INSTALL_TRIED=1"
echo ------------------------------------------------------------
echo Python 3 was not found on this PC - installing it automatically.
echo (One-time step; about a 26 MB download.)
echo ------------------------------------------------------------
echo.
echo   [1/2] Trying winget (Windows package manager)...
winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
set "PATH=%LOCALAPPDATA%\Programs\Python\Launcher;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto detect
echo.
echo   [2/2] winget unavailable or failed - downloading the official
echo         installer from python.org instead...
curl -L -o "%TEMP%\faerie-python-3.12.10.exe" https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
if errorlevel 1 goto installfailed
echo   Running the installer silently (per-user, includes the "py" launcher)...
start /wait "" "%TEMP%\faerie-python-3.12.10.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
set "PATH=%LOCALAPPDATA%\Programs\Python\Launcher;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts;%PATH%"
goto detect

:installfailed
echo.
echo [X] Automatic Python install did not work. Install Python 3.12 manually
echo     from https://www.python.org/downloads/ (tick "Add python.exe to
echo     PATH" and keep the "py launcher" option), then re-run this script.

:end
echo.
pause
