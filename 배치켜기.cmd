@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage-tasks.ps1" -Action Enable
set "CAREER_SCOUT_BATCH_EXIT=%ERRORLEVEL%"
echo.
if not "%CAREER_SCOUT_BATCH_EXIT%"=="0" (echo 배치 등록에 실패했습니다. 위 오류를 확인하세요.) else (echo 배치를 켰습니다. 다음 예약일 오후 1시부터 자동 실행합니다.)
pause
exit /b %CAREER_SCOUT_BATCH_EXIT%
