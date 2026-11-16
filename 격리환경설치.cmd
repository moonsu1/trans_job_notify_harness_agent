@echo off
chcp 65001 >nul
echo Windows 기능 설치에는 관리자 확인 창에서 '예'를 눌러야 합니다.
echo 재부팅이 필요하면 작업을 저장한 뒤 직접 재부팅하세요. 자동 재부팅은 하지 않습니다.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-isolation.ps1"
echo.
echo 설치 상태는 설정진단.cmd에서도 확인할 수 있습니다.
pause
