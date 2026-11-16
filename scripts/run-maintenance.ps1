$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$env:PYTHONUTF8 = '1'
& (Join-Path $taskRoot '.venv\Scripts\python.exe') -m career_scout.watchdog launch --kind maintenance
exit $LASTEXITCODE
