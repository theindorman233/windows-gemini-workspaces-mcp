#Requires -Version 7.0
param(
    [int]$Port = 8766,
    [string]$Path = '/mcp',
    [string]$ProposalStore = (Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP\live-state')
)

$ErrorActionPreference = 'Stop'
$logDir = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir 'live-server.log'

# Rotate live-server.log if it exceeds 10 MB
$maxLogBytes = 10MB
if (Test-Path -LiteralPath $logPath) {
    $logInfo = Get-Item -LiteralPath $logPath -ErrorAction SilentlyContinue
    if ($logInfo -and $logInfo.Length -gt $maxLogBytes) {
        $backupLogPath = "$logPath.1"
        Move-Item -LiteralPath $logPath -Destination $backupLogPath -Force
    }
}

# Prefer persistent state directory when it already holds a proposal database and
# ProposalStore was not explicitly passed (an empty directory would abort startup)
if (-not $PSBoundParameters.ContainsKey('ProposalStore')) {
    $persistentStore = Join-Path $logDir 'live-state'
    $persistentDatabase = Join-Path $persistentStore 'proposals.db'
    if ((Test-Path -LiteralPath $persistentStore -PathType Container) -and
        (Test-Path -LiteralPath $persistentDatabase -PathType Leaf)) {
        $ProposalStore = $persistentStore
    }
}

# Stop existing oauth_probe.py listeners on $Port; abort if occupied by an unknown process
$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$stoppedAny = $false
foreach ($conn in $listeners) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $($conn.OwningProcess)" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -like '*oauth_probe.py*') {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        if ($proc.ParentProcessId) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.ParentProcessId)" -ErrorAction SilentlyContinue
            if ($parent -and $parent.CommandLine -like '*oauth_probe.py*') {
                Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
        $stoppedAny = $true
    }
    else {
        $procName = if ($proc) { $proc.Name } else { 'unknown' }
        $msg = "ERROR: Port $Port is already in use by non-MCP process (PID=$($conn.OwningProcess), Name=$procName). Aborting startup."
        Write-Error $msg
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $msg"
        exit 1
    }
}

# Poll up to 5 seconds for port release
if ($stoppedAny) {
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($remaining.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 200
    }
    $stillBound = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($stillBound.Count -gt 0) {
        $pids = ($stillBound | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
        $msg = "ERROR: Port $Port was not released within 5 seconds (still held by PID: $pids). Aborting startup."
        Write-Error $msg
        Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $msg"
        exit 1
    }
}

if (-not $env:MCP_ALLOW_DCR) { $env:MCP_ALLOW_DCR = '0' }

$env:MCP_PROBE_PORT = [string]$Port
$env:MCP_PROBE_PATH = $Path

& (Join-Path $PSScriptRoot 'start_oauth_probe.ps1') -LiveWorkspaces -ProposalStore $ProposalStore *>> $logPath
