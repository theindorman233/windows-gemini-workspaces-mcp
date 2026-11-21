#Requires -Version 7.0
param([switch]$SyntheticRead, [switch]$SyntheticEdit, [switch]$Workspaces, [switch]$LiveWorkspaces, [switch]$Detach, [string]$ProposalStore)

$ErrorActionPreference = 'Stop'
if (@($SyntheticRead, $SyntheticEdit, $Workspaces, $LiveWorkspaces).Where({ $_ }).Count -gt 1) { throw 'Select one server mode.' }
if ($ProposalStore -and -not $LiveWorkspaces) { throw 'ProposalStore requires LiveWorkspaces.' }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$probeArguments = @(
    if ($SyntheticRead) { '--synthetic-read' }
    if ($SyntheticEdit) {
        $workspace = & $python (Join-Path $PSScriptRoot 'synthetic_edits.py') init
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Write-Host "Synthetic workspace: $workspace"
        '--edit-workspace'
        $workspace
    }
    if ($Workspaces -or $LiveWorkspaces) {
        $workspace = $ProposalStore
        if (-not $workspace) {
            $workspace = & $python (Join-Path $PSScriptRoot 'synthetic_edits.py') init --workspaces
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        Write-Host "Proposal store: $workspace"
        if ($LiveWorkspaces) { '--live-workspaces-store' } else { '--workspace-store' }
        $workspace
    }
)
$secretPath = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP\github-client-secret.dpapi'
$secure = ConvertTo-SecureString -String ([IO.File]::ReadAllText($secretPath))
$plaintext = ConvertFrom-SecureString -SecureString $secure -AsPlainText
try {
    if ($Detach) {
        # The helper is a separate script on purpose: capturing a child process
        # launched from this scope would also capture the Process object's output
        # streams, which breaks the $LASTEXITCODE/pid handoff back to the caller.
        $helper = @{
            Python = $python
            ProbeArguments = $probeArguments
            Secret = $plaintext
            WorkingDirectory = $PSScriptRoot
            Port = [string]$env:MCP_PROBE_PORT
            Path = [string]$env:MCP_PROBE_PATH
            AllowDcr = [string]$env:MCP_ALLOW_DCR
            LogPath = Join-Path $env:LOCALAPPDATA 'WindowsGeminiMCP\server.log'
            PidFile = Join-Path $env:LOCALAPPDATA "WindowsGeminiMCP\live-server-$($env:MCP_PROBE_PORT).pid"
        }
        . (Join-Path $PSScriptRoot 'launch_detached.ps1') @helper
        exit 0
    }

    # The non-detached path is for manual debugging and the synthetic/workspace test
    # modes only. It pipes into python in the foreground, so running it from a
    # scheduled task would still put a console on screen. Use -Detach for hosting.
    $plaintext | & $python (Join-Path $PSScriptRoot 'oauth_probe.py') @probeArguments
    exit $LASTEXITCODE
}
finally {
    Remove-Variable plaintext -ErrorAction SilentlyContinue
}
