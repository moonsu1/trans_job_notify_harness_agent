@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage-tasks.ps1" -Action Status
set "CAREER_SCOUT_BATCH_EXIT=%ERRORLEVEL%"
pause
exit /b %CAREER_SCOUT_BATCH_EXIT%
