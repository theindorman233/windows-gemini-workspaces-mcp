#Requires -Version 7.0
# One-shot watchdog for the live MCP service on 127.0.0.1:8766.
# Registered as scheduled task "WindowsGeminiMcpLiveGuard", fires every minute and
# exits immediately while the port answers, so a healthy machine pays almost nothing.
param(
    [int]$Port = 8766,
    [string]$TaskName = 'WindowsGeminiMcpLive',
    [int]$StartupGraceSeconds = 60
)

$ErrorActionPreference = 'SilentlyContinue'
$logDir = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'guard.log'

function Write-GuardLog {
    param([string]$Message)
    Add-Content -LiteralPath $log -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

# Same reason as start_live_task.ps1: the OS listener table gives an unambiguous
# answer, while Get-NetTCPConnection silently yields nothing when the CIM query fails.
function Test-PortListening {
    param([int]$TargetPort)
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return @($listeners | Where-Object { $_.Port -eq $TargetPort }).Count -gt 0
}

if (Test-PortListening -TargetPort $Port) { exit 0 }

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-GuardLog "task '$TaskName' not found"
    exit 1
}

# Let an in-flight start finish instead of killing it mid-boot.
if ($task.State -eq 'Running') {
    $ageSeconds = ((Get-Date) - (Get-ScheduledTaskInfo -TaskName $TaskName).LastRunTime).TotalSeconds
    if ($ageSeconds -lt $StartupGraceSeconds) { exit 0 }
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

Start-ScheduledTask -TaskName $TaskName

$deadline = (Get-Date).AddSeconds(60)
$recovered = $false
while ((Get-Date) -lt $deadline) {
    if (Test-PortListening -TargetPort $Port) { $recovered = $true; break }
    Start-Sleep -Milliseconds 500
}

Write-GuardLog ("restart from state={0} recovered={1}" -f $task.State, $recovered)
if (-not $recovered) { exit 1 }
