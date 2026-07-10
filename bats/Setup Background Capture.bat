@echo off
title Faerie Fire - Setup Background Capture
cd /d "%~dp0.."

echo ============================================
echo   Faerie Fire - background capture setup
echo ============================================
echo.

REM --- Resolve ONE interpreter and derive its pythonw so the packages we
REM     install and the process we auto-start are the same Python. ---
where python >nul 2>nul
if errorlevel 1 goto nopython
for /f "delims=" %%V in ('python -c "import sys;print(sys.executable)"') do set "PYEXE=%%V"
set "PYW=%PYEXE:python.exe=pythonw.exe%"
echo [ok] Python: %PYEXE%
echo.

echo Installing dependencies (tray icon + capture)...
"%PYEXE%" -m pip install -r requirements-core.txt
echo   (for OCR/voice extras, run "Install Dependencies.bat")
echo.

echo Verifying the tray/capture imports...
"%PYEXE%" -c "import PIL, pystray" && goto verified
echo [X] pystray/PIL still won't import under this Python.
echo     Run "Install Dependencies.bat" for a full repair, then re-run this.
goto end

:verified
echo [ok] pystray + PIL import.
echo.

echo Registering capture to auto-start for the current user...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "FaerieFire-Capture" /t REG_SZ /d "\"%PYW%\" \"%~dp0..\tray.py\"" /f
echo.

echo Starting capture now (look for the Faerie Fire icon near the clock)...
start "" "%PYW%" tray.py
echo.
echo Done.
echo   - Stop now:         "Stop Background Capture.bat"
echo   - Check status:     "Capture Status.bat"
echo   - Remove auto-start: reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "FaerieFire-Capture" /f
echo.
echo If no tray icon appears, run this to see the error:   python tray.py
goto end

:nopython
echo [X] Python is not on PATH. Install Python (and tick "Add to PATH"), then re-run this.

:end
echo.
pause
