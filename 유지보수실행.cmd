@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m career_scout.cli maintain
echo.
echo 유지보수는 별도 메일을 보내지 않습니다. 결과는 수집 상태 리포트에 저장합니다.
pause
