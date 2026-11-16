$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
foreach ($configName in @('settings', 'profile')) {
    $configPath = Join-Path $taskRoot "config\$configName.yaml"
    if (-not (Test-Path -LiteralPath $configPath)) {
        Copy-Item -LiteralPath (Join-Path $taskRoot "config\$configName.example.yaml") -Destination $configPath
        Write-Host "Created config/$configName.yaml from example. Edit it before running the scout."
    }
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & py install 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 installation failed' }
    & py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}
if (Test-Path -LiteralPath 'requirements.lock.txt') {
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed' }
}
& '.\.venv\Scripts\python.exe' -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
& '.\.venv\Scripts\python.exe' -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Chromium installation failed' }
& '.\.venv\Scripts\python.exe' -m career_scout.cli doctor
