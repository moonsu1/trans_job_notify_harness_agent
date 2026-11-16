param([switch]$ResetCatchUp)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if ((Get-TimeZone).Id -ne 'Korea Standard Time') {
    throw 'Set the Windows time zone to Korea Standard Time before registering.'
}
$taskRunner = Join-Path $PSScriptRoot 'run-daily.ps1'
$taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $taskRunner + '"') -WorkingDirectory $taskRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$schedulePolicy = (& $taskPython -c "import json; from career_scout.core import load_config; s=load_config()[0]; print(json.dumps({'interval':s.get('schedule_days_interval',3),'anchor':s.get('schedule_anchor','2026-11-09')}))") | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read schedule policy' }
$scheduleAnchor = [datetime]::ParseExact($schedulePolicy.anchor, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
$nextAt = $scheduleAnchor.Date.AddHours(13)
while ($nextAt -le (Get-Date)) { $nextAt = $nextAt.AddDays([int]$schedulePolicy.interval) }
$taskTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval ([int]$schedulePolicy.interval) -At $nextAt
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskData = (& $taskPython -c "from career_scout.core import data_dir; print(data_dir())").Trim()
$scheduleFile = Join-Path $taskData 'schedule.json'
$installedAt = (Get-Date).ToString('o')
if ((Test-Path -LiteralPath $scheduleFile) -and -not $ResetCatchUp) {
    try { $installedAt = (Get-Content -LiteralPath $scheduleFile -Raw -Encoding UTF8 | ConvertFrom-Json).installed_at } catch {}
}
@{anchor=$schedulePolicy.anchor; interval=[int]$schedulePolicy.interval; root=$taskRoot; python=$taskPython; installed_at=$installedAt; paused=$false} | ConvertTo-Json | Set-Content -LiteralPath $scheduleFile -Encoding UTF8
Register-ScheduledTask -TaskName 'CareerScout-Daily-1300' -Action $taskAction -Trigger $taskTrigger -Settings $taskSettings -Principal $taskPrincipal -Description 'AI career scouting and Gmail report every 3 days at 13:00 KST' -Force | Select-Object TaskName,State
$maintenanceAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'run-maintenance.ps1')+'"') -WorkingDirectory $taskRoot
$maintenanceTrigger = New-ScheduledTaskTrigger -Daily -At '14:00'
Register-ScheduledTask -TaskName 'CareerScout-Maintenance-1400' -Action $maintenanceAction -Trigger $maintenanceTrigger -Settings $taskSettings -Principal $taskPrincipal -Description 'Daily source health and bounded isolated repair; no separate email' -Force | Select-Object TaskName,State
$watchAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'run-watchdog.ps1')+'"') -WorkingDirectory $taskRoot
$watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 30)
$watchSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'CareerScout-Watchdog-30m' -Action $watchAction -Trigger $watchTrigger -Settings $watchSettings -Principal $taskPrincipal -Description 'Check missing/interrupted operations every 30 minutes' -Force | Select-Object TaskName,State
$resumeAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'setup-isolation.ps1')+'" -Resume') -WorkingDirectory $taskRoot
$resumeTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
Register-ScheduledTask -TaskName 'CareerScout-SandboxResume' -Action $resumeAction -Trigger $resumeTrigger -Settings $taskSettings -Principal $taskPrincipal -Description 'Resume Docker/parser setup after Windows restart' -Force | Select-Object TaskName,State
Write-Output ('Registered every ' + $schedulePolicy.interval + ' days at 13:00 KST. Next: ' + $nextAt.ToString('yyyy-MM-dd HH:mm'))
