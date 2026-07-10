@echo off
REM Signal the background capture to stop (it exits within a couple seconds).
echo stop > "%~dp0..\.capture_stop"
echo Stop signal sent. Background capture will exit shortly.
timeout /t 3 >nul
