@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m career_scout.cli health --no-api
if exist "%~dp0reports\latest-health.html" start "" "%~dp0reports\latest-health.html"
pause
