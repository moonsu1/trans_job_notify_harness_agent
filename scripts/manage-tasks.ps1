[CmdletBinding(SupportsShouldProcess=$true)]
param([ValidateSet('Enable','Disable','Status')][string]$Action='Status')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskNames = @('CareerScout-Daily-1300','CareerScout-Maintenance-1400','CareerScout-Watchdog-30m','CareerScout-SandboxResume')
$taskData = if ($env:CAREER_SCOUT_DATA_DIR) { $env:CAREER_SCOUT_DATA_DIR } else { Join-Path $env:LOCALAPPDATA 'CareerScout' }
$scheduleFile = Join-Path $taskData 'schedule.json'

if ($Action -eq 'Disable' -and $PSCmdlet.ShouldProcess('CareerScout scheduled tasks','Pause all future automatic runs')) {
    if (Test-Path -LiteralPath $scheduleFile) {
        $schedule = Get-Content -LiteralPath $scheduleFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $schedule | Add-Member -NotePropertyName paused -NotePropertyValue $true -Force
        $schedule | Add-Member -NotePropertyName paused_at -NotePropertyValue (Get-Date).ToString('o') -Force
        $temporaryFile = Join-Path $taskData ('schedule-'+[guid]::NewGuid().ToString('N')+'.tmp')
        $schedule | ConvertTo-Json | Set-Content -LiteralPath $temporaryFile -Encoding UTF8
        Move-Item -LiteralPath $temporaryFile -Destination $scheduleFile -Force
    }
    foreach ($name in $taskNames) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) { $existing | Disable-ScheduledTask | Out-Null }
    }
    Write-Output 'Paused report, maintenance, watchdog and setup-resume tasks.'
    Write-Output 'An already-running job is allowed to finish. Manual runs remain available.'
}
if ($Action -eq 'Enable' -and $PSCmdlet.ShouldProcess('CareerScout scheduled tasks','Register or resume from the next scheduled time')) {
    Push-Location -LiteralPath $taskRoot
    try { & (Join-Path $PSScriptRoot 'register-task.ps1') -ResetCatchUp }
    finally { Pop-Location }
    Write-Output 'Automatic runs enabled. Paused dates will not be replayed.'
}

$schedule = if (Test-Path -LiteralPath $scheduleFile) { Get-Content -LiteralPath $scheduleFile -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
Write-Output ('Automatic schedule paused: '+[bool]$schedule.paused)
$taskRows = foreach ($name in $taskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($task) {
        $info = $task | Get-ScheduledTaskInfo
        [pscustomobject]@{Task=$name; Enabled=$task.Settings.Enabled; State=$task.State; NextRun=$info.NextRunTime}
    } else {
        [pscustomobject]@{Task=$name; Enabled=$false; State='Not registered'; NextRun=$null}
    }
}
$taskRows | Format-Table -AutoSize
