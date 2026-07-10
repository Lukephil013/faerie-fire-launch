@echo off
REM File a brain dump into your project docs (projects\) without opening the
REM companion. Pass the dump as arguments, or run bare for the prompt below.
REM Examples:
REM   File Idea.bat "half an essay about my Etsy SEO idea..."
REM   File Idea.bat --list
REM   File Idea.bat --undo <entry-id>
REM   File Idea.bat --distill <slug>
cd /d "%~dp0.."
if "%~1"=="" (
    set /p DUMP="What's the idea? > "
    python tools\file_dump.py "%DUMP%"
) else (
    python tools\file_dump.py %*
)
echo.
pause
