#Requires -Version 7.0
param([switch]$SyntheticRead, [switch]$SyntheticEdit, [switch]$Workspaces, [switch]$LiveWorkspaces, [string]$ProposalStore)

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
    $plaintext | & $python (Join-Path $PSScriptRoot 'oauth_probe.py') @probeArguments
    exit $LASTEXITCODE
}
finally {
    Remove-Variable plaintext -ErrorAction SilentlyContinue
}
