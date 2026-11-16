param([switch]$SystemFeaturesOnly, [switch]$Resume, [string]$StateFile)
$ErrorActionPreference = 'Stop'
$setupRoot = Split-Path -Parent $PSScriptRoot
$setupData = Join-Path $env:LOCALAPPDATA 'CareerScout\setup'
New-Item -ItemType Directory -Force -Path $setupData | Out-Null
if (-not $StateFile) { $StateFile = Join-Path $setupData 'isolation-state.json' }
function Save-SetupState($stage, $detail) {
    @{stage=$stage; detail=$detail; at=(Get-Date).ToString('o')} | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8
    Write-Output ($stage + ': ' + $detail)
}
function Invoke-SetupNative($executable, $nativeArgs, $logName) {
    $process = Start-Process -FilePath $executable -ArgumentList $nativeArgs -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput (Join-Path $setupData ($logName+'.log')) -RedirectStandardError (Join-Path $setupData ($logName+'-error.log'))
    return $process.ExitCode
}
if ($SystemFeaturesOnly) {
    $needRestart = $false
    foreach ($feature in @('VirtualMachinePlatform','Microsoft-Windows-Subsystem-Linux')) {
        $currentFeature = Get-WindowsOptionalFeature -Online -FeatureName $feature
        if ($currentFeature.State -ne 'Enabled') {
            $enabled = Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart
            $needRestart = $needRestart -or $enabled.RestartNeeded
        }
    }
    $wslResult = Invoke-SetupNative (Join-Path $env:WINDIR 'system32\wsl.exe') @('--install','--no-distribution','--web-download') 'wsl-install'
    Save-SetupState 'system_features' ('WSL exit=' + $wslResult + '; restart_needed=' + $needRestart)
    if ($needRestart -or $wslResult -eq 3010) { exit 3010 }
    if ($wslResult -ne 0) { exit 1 }
    exit 0
}
$dockerExe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
$dockerDesktop = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'
$installer = Join-Path $setupData 'DockerDesktopInstaller.exe'
$wslStatus = Invoke-SetupNative (Join-Path $env:WINDIR 'system32\wsl.exe') @('--status') 'wsl-status'
if ($wslStatus -ne 0 -and -not $Resume) {
    Save-SetupState 'system_setup' 'Requesting Windows optional-feature setup; no automatic reboot'
    try {
        $elevatedArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -SystemFeaturesOnly -StateFile "' + $StateFile + '"'
        $featureProcess = Start-Process powershell.exe -ArgumentList $elevatedArgs -Verb RunAs -WindowStyle Hidden -Wait -PassThru
        if ($featureProcess.ExitCode -eq 3010) { Save-SetupState 'restart_required' 'Restart Windows to finish WSL2; setup will resume at next login'; exit 3 }
        if ($featureProcess.ExitCode -ne 0) { Save-SetupState 'system_setup_failed' ('Windows setup exit='+$featureProcess.ExitCode); exit 2 }
    } catch {
        Save-SetupState 'administrator_required' 'Windows system-feature setup was not completed'
        exit 2
    }
}
if ($wslStatus -ne 0 -and $Resume) { Save-SetupState 'administrator_required' 'Run isolation setup manually to enable Windows features'; exit 2 }
if (-not (Test-Path -LiteralPath $dockerExe)) {
    if (-not (Test-Path -LiteralPath $installer)) {
        Save-SetupState 'download' 'Downloading official Docker Desktop installer'
        Invoke-WebRequest -Uri 'https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe' -OutFile $installer
    }
    $signature = Get-AuthenticodeSignature -FilePath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Docker') { throw 'Docker installer signature verification failed' }
    Save-SetupState 'install' 'Installing per-user Docker Desktop with WSL2 backend'
    $installProcess = Start-Process -FilePath $installer -ArgumentList @('install','--user','--quiet','--accept-license','--backend=wsl-2','--no-windows-containers') -WindowStyle Hidden -Wait -PassThru
    if ($installProcess.ExitCode -notin @(0,3010)) { Save-SetupState 'install_failed' ('exit=' + $installProcess.ExitCode); exit 1 }
}
if (-not (Test-Path -LiteralPath $dockerDesktop)) { throw 'Docker Desktop executable missing' }
Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
$engineReady = $false
for ($attempt=0; $attempt -lt 30; $attempt++) {
    $engineCode = Invoke-SetupNative $dockerExe @('info','--format','{{.ServerVersion}}') 'docker-status'
    if ($engineCode -eq 0) { $engineReady=$true; break }
    Start-Sleep -Seconds 2
}
if (-not $engineReady) {
    Save-SetupState 'restart_or_engine_required' 'Docker engine unavailable; restart Windows if optional features were enabled, then rerun this setup'
    exit 3
}
Save-SetupState 'build' 'Building the trusted parser image'
$buildCode = Invoke-SetupNative $dockerExe @('build','--pull','-t','career-scout-parser:1',('"'+(Join-Path $setupRoot 'sandbox')+'"')) 'image-build'
if ($buildCode -ne 0) { Save-SetupState 'build_failed' 'Parser image build failed'; exit 1 }
Save-SetupState 'validate' 'Testing the actual isolated worker and protected regressions'
$setupPython = Join-Path $setupRoot '.venv\Scripts\python.exe'
Push-Location -LiteralPath $setupRoot
try { $verifyCode = Invoke-SetupNative $setupPython @(('"'+(Join-Path $PSScriptRoot 'verify-isolation.py')+'"')) 'isolation-validation' }
finally { Pop-Location }
if ($verifyCode -ne 0) { Save-SetupState 'validation_failed' 'Isolated runtime validation failed; automatic code repair stays disabled'; exit 1 }
Save-SetupState 'ready' 'Isolated parser image available; automatic repair can run'
