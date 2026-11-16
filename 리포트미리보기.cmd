@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m career_scout.cli run --offline --no-api
start "" "reports\latest.html"
pause
