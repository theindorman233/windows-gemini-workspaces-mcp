#Requires -Version 7.0
# Watchdog: keep the live MCP service on 127.0.0.1:8766 alive.
# Registered as scheduled task "WindowsGeminiMcpLiveGuard" (fires every minute, watches for 55s).
$ErrorActionPreference = 'SilentlyContinue'
$taskName = 'WindowsGeminiMcpLive'
$logDir = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP'
$log = Join-Path $logDir 'guard.log'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-Listening {
    @(Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue).Count -gt 0
}

$stopAt = (Get-Date).AddSeconds(55)
while ((Get-Date) -lt $stopAt) {
    if (Test-Listening) {
        Start-Sleep -Seconds 5
        continue
    }

    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Add-Content -LiteralPath $log -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') task '$taskName' not found"
        exit 1
    }

    $state = $task.State
    if ($state -eq 'Running') {
        # Task claims to be running but nothing listens: allow 90s of startup, then force restart.
        $info = Get-ScheduledTaskInfo -TaskName $taskName
        $ageSeconds = ((Get-Date) - $info.LastRunTime).TotalSeconds
        if ($ageSeconds -lt 90) {
            Start-Sleep -Seconds 5
            continue
        }
        Stop-ScheduledTask -TaskName $taskName
        Start-Sleep -Seconds 2
    }

    Start-ScheduledTask -TaskName $taskName
    $deadline = (Get-Date).AddSeconds(25)
    $recovered = $false
    while ((Get-Date) -lt $deadline) {
        if (Test-Listening) { $recovered = $true; break }
        Start-Sleep -Milliseconds 500
    }
    Add-Content -LiteralPath $log -Value ("{0} restart from state={1} recovered={2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $state, $recovered)
    Start-Sleep -Seconds 5
}
