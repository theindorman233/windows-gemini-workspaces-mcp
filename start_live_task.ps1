#Requires -Version 7.0
param(
    [int]$Port = 8766,
    [string]$Path = '/mcp',
    [string]$ProposalStore = (Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP\live-state'),
    [switch]$Detach
)

$ErrorActionPreference = 'Stop'
$logDir = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir 'live-server.log'
$serverLogPath = Join-Path $logDir 'server.log'
# Per-port, so an instance on another port is never treated as this one's predecessor.
$pidFile = Join-Path $logDir "live-server-${Port}.pid"

function Write-Log {
    param([string]$Message)
    Add-Content -LiteralPath $logPath -Value ("{0} {1}" -f (Get-Date -Format o), $Message)
}

# Read the listener table straight from the OS. Get-NetTCPConnection goes through
# CIM and returns an empty result when the query itself is refused, which looks
# exactly like "nothing is listening" - that ambiguity is why it is not used here.
function Test-PortListening {
    param([int]$TargetPort)
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return @($listeners | Where-Object { $_.Port -eq $TargetPort }).Count -gt 0
}

# The listener table above carries no owning PID, so netstat supplies it.
# TCP and LISTENING are not localised, which makes this parse safe on any UI language.
function Get-PortOwnerPids {
    param([int]$TargetPort)
    $owners = @()
    foreach ($line in (netstat -ano -p TCP)) {
        $parts = @(($line -split '\s+') | Where-Object { $_ })
        if ($parts.Count -ge 5 -and $parts[0] -eq 'TCP' -and $parts[3] -eq 'LISTENING' -and
            [int]($parts[1].Split(':')[-1]) -eq $TargetPort) {
            $owners += [int]$parts[4]
        }
    }
    return @($owners | Sort-Object -Unique)
}

# Stop only processes that are demonstrably this server, and only after reading the
# parent PID (the CIM lookup fails once the child is gone).
function Stop-StaleProbe {
    param([int[]]$CandidateIds)
    $stopped = $false
    foreach ($candidateId in $CandidateIds) {
        if ($candidateId -le 0) { continue }
        $info = Get-CimInstance Win32_Process -Filter "ProcessId = $candidateId" -ErrorAction SilentlyContinue
        if (-not $info -or $info.CommandLine -notlike '*oauth_probe.py*') { continue }
        $parentId = $info.ParentProcessId
        Stop-Process -Id $info.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped = $true
        if ($parentId) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $parentId" -ErrorAction SilentlyContinue
            if ($parent -and $parent.CommandLine -like '*oauth_probe.py*') {
                Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    }
    return $stopped
}

# Rotate live-server.log if it exceeds 10 MB
$maxLogBytes = 10MB
if (Test-Path -LiteralPath $logPath) {
    $logInfo = Get-Item -LiteralPath $logPath -ErrorAction SilentlyContinue
    if ($logInfo -and $logInfo.Length -gt $maxLogBytes) {
        Move-Item -LiteralPath $logPath -Destination "$logPath.1" -Force
    }
}

# Clear whatever currently holds $Port: the PID recorded by the previous run, plus
# anything netstat reports as listening. Abort if the holder is not this server.
$candidateIds = @()
if (Test-Path -LiteralPath $pidFile) {
    $recorded = Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue
    if ($recorded -and $recorded.Trim() -match '^\d+$') { $candidateIds += [int]$recorded.Trim() }
}
if (Test-PortListening -TargetPort $Port) { $candidateIds += Get-PortOwnerPids -TargetPort $Port }
$candidateIds = @($candidateIds | Sort-Object -Unique)

if ($candidateIds.Count -gt 0) {
    $stoppedAny = Stop-StaleProbe -CandidateIds $candidateIds
    if (-not $stoppedAny -and (Test-PortListening -TargetPort $Port)) {
        $owners = (Get-PortOwnerPids -TargetPort $Port) -join ', '
        $msg = "ERROR: port $Port is held by a process that is not oauth_probe.py (PID: $owners). Aborting startup."
        Write-Log $msg
        Write-Error $msg
        exit 1
    }
    if ($stoppedAny) {
        $releaseDeadline = [DateTime]::UtcNow.AddSeconds(10)
        while ([DateTime]::UtcNow -lt $releaseDeadline -and (Test-PortListening -TargetPort $Port)) {
            Start-Sleep -Milliseconds 200
        }
        if (Test-PortListening -TargetPort $Port) {
            $owners = (Get-PortOwnerPids -TargetPort $Port) -join ', '
            $msg = "ERROR: port $Port was not released within 10 seconds (still held by PID: $owners). Aborting startup."
            Write-Log $msg
            Write-Error $msg
            exit 1
        }
    }
}

if (-not $env:MCP_ALLOW_DCR) { $env:MCP_ALLOW_DCR = '0' }
$env:MCP_PROBE_PORT = [string]$Port
$env:MCP_PROBE_PATH = $Path

# Foreground mode: the server owns this console and logs to live-server.log. This is
# what the README quick start uses. Use -Detach - launch_hidden.vbs already does -
# whenever the caller is a scheduled task.
if (-not $Detach) {
    & (Join-Path $PSScriptRoot 'start_oauth_probe.ps1') -LiveWorkspaces -ProposalStore $ProposalStore *>> $logPath
    exit $LASTEXITCODE
}

if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile -Force }

try {
    & (Join-Path $PSScriptRoot 'start_oauth_probe.ps1') -LiveWorkspaces -Detach -ProposalStore $ProposalStore *>> $logPath
}
catch {
    # Report the real reason; without this the PID check below would mask it with a
    # misleading "no PID recorded".
    $msg = "ERROR: detached launch failed: $($_.Exception.Message)"
    Write-Log $msg
    Write-Error $msg
    exit 1
}

# launch_detached.ps1 writes the PID after Process.Start returns, so poll instead of
# reading the file once - a single read can arrive before the write.
$serverPid = 0
$pidDeadline = [DateTime]::UtcNow.AddSeconds(5)
while ([DateTime]::UtcNow -lt $pidDeadline -and $serverPid -le 0) {
    if (Test-Path -LiteralPath $pidFile) {
        $recorded = Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue
        if ($recorded -and $recorded.Trim() -match '^\d+$') { $serverPid = [int]$recorded.Trim() }
    }
    if ($serverPid -le 0) { Start-Sleep -Milliseconds 100 }
}
if ($serverPid -le 0) {
    $msg = "ERROR: detached launch did not record a PID within 5 seconds. See $logPath"
    Write-Error $msg
    Write-Log $msg
    exit 1
}

Write-Log "Started oauth_probe PID=$serverPid"

# A cold start imports fastmcp and opens the state store, so wait for the port
# rather than sleeping for a fixed time. Distinguish "exited" from "slow to bind".
$bindDeadline = [DateTime]::UtcNow.AddSeconds(30)
while ([DateTime]::UtcNow -lt $bindDeadline -and -not (Test-PortListening -TargetPort $Port)) {
    if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}

if (-not (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)) {
    $msg = "ERROR: server process $serverPid exited before binding port $Port. See $serverLogPath"
    Write-Error $msg
    Write-Log $msg
    exit 1
}

if (-not (Test-PortListening -TargetPort $Port)) {
    $msg = "WARNING: server process $serverPid is alive but port $Port is still closed after 30 seconds. See $serverLogPath"
    Write-Log $msg
}
