#Requires -Version 7.0
# Dot-sourced by start_oauth_probe.ps1 -Detach.
#
# Two console rules have to hold or a terminal window appears:
#   1. This process must not be a console-subsystem child of a hidden console.
#      CreateNoWindow=TRUE gives the child no console at all, so Windows Terminal
#      (the default terminal on this machine) has nothing to attach a window to.
#   2. The child must not inherit the caller's stdout/stderr handles, which would
#      keep the caller's console alive after the launcher exits.
# Logging is therefore routed by the server itself through MCP_LOG_FILE.
param(
    [Parameter(Mandatory)][string]$Python,
    [string[]]$ProbeArguments = @(),
    [Parameter(Mandatory)][string]$Secret,
    [Parameter(Mandatory)][string]$WorkingDirectory,
    [string]$Port,
    [string]$Path,
    [string]$AllowDcr,
    [string]$LogPath,
    [Parameter(Mandatory)][string]$PidFile
)

$ErrorActionPreference = 'Stop'

$psi = [System.Diagnostics.ProcessStartInfo]::new($Python)
# The script path must precede its own arguments
$psi.ArgumentList.Add((Join-Path $WorkingDirectory 'oauth_probe.py')) | Out-Null
foreach ($arg in $ProbeArguments) { $psi.ArgumentList.Add($arg) }
$psi.WorkingDirectory = $WorkingDirectory
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardInput = $true
# An unset StandardInputEncoding falls back to the OEM/ANSI code page, which is
# GBK on a Chinese Windows install. The secret is ASCII today, but pin UTF-8 so a
# non-ASCII value can never be silently mangled.
$psi.StandardInputEncoding = [System.Text.UTF8Encoding]::new($false)
if ($Port) { $psi.EnvironmentVariables['MCP_PROBE_PORT'] = $Port }
if ($Path) { $psi.EnvironmentVariables['MCP_PROBE_PATH'] = $Path }
if ($AllowDcr) { $psi.EnvironmentVariables['MCP_ALLOW_DCR'] = $AllowDcr }
if ($LogPath) { $psi.EnvironmentVariables['MCP_LOG_FILE'] = $LogPath }

$proc = [System.Diagnostics.Process]::Start($psi)
$proc.StandardInput.WriteLine($Secret)
$proc.StandardInput.Close()

$pidDir = Split-Path -Parent $PidFile
if (-not (Test-Path -LiteralPath $pidDir)) { New-Item -ItemType Directory -Force -Path $pidDir | Out-Null }
[IO.File]::WriteAllText($PidFile, [string]$proc.Id)
