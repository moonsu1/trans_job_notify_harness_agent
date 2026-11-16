@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage-tasks.ps1" -Action Disable
set "CAREER_SCOUT_BATCH_EXIT=%ERRORLEVEL%"
echo.
if not "%CAREER_SCOUT_BATCH_EXIT%"=="0" (echo 배치 중지에 실패했습니다. 위 오류를 확인하세요.) else (echo 자동 배치와 감시를 껐습니다. 이미 실행 중인 작업은 완료될 수 있습니다.)
pause
exit /b %CAREER_SCOUT_BATCH_EXIT%
