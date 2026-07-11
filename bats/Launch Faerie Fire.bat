@echo off
REM Double-click to launch Faerie Fire (Command Center + Growth).
REM Opens gui.py directly — no tray icon, no optional dependencies required.
REM First run walks you through onboarding (API key + naming your Soul) inside the app.
REM If the window doesn't appear, run "py -3 gui.py" from this project folder in a
REM terminal instead, so you can see the error.
REM pyw -3 = the interpreter New Test Instance.bat validates (Python 3.14,
REM pywebview 5.2). Plain "pythonw" resolves to Python 3.11 on this machine,
REM whose pywebview never initializes WebView2 (black window / freeze).
cd /d "%~dp0.."
start "" pyw -3 gui.py --view command-center
if errorlevel 1 start "" pythonw gui.py --view command-center
