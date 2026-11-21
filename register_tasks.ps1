#Requires -Version 7.0
<#
Re-registers both scheduled tasks for the Gemini MCP service. Requires an elevated
PowerShell session: the existing task was created by an administrator and its file
ACL refuses writes from a non-elevated token.

  WindowsGeminiMcpLive       logon + boot, launches wscript -> launch_hidden.vbs
  WindowsGeminiMcpLiveGuard  every minute, launches wscript -> guard_hidden.vbs

Both actions go through wscript.exe, a GUI-subsystem host, so Task Scheduler never
creates a console for the task. No RestartOnFailure is written: recovery is the
guard's job, which is far more informative than a blind retry.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'WindowsGeminiMcpLive',
    [string]$GuardTaskName = 'WindowsGeminiMcpLiveGuard'
)

$ErrorActionPreference = 'Stop'

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated (Run as administrator) PowerShell session.'
}

$root = $PSScriptRoot
$launchScript = Join-Path $root 'launch_hidden.vbs'
$guardScript = Join-Path $root 'guard_hidden.vbs'
foreach ($path in @($launchScript, $guardScript)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing launcher: $path" }
}

$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$userName = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$startBoundary = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')

function New-TaskXml {
    param(
        [string]$Description,
        [string]$Triggers,
        [string]$Arguments,
        [string]$Execute = 'wscript.exe'
    )
    @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$Description</Description>
  </RegistrationInfo>
  <Triggers>
$Triggers
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$sid</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$Execute</Command>
      <Arguments>$Arguments</Arguments>
      <WorkingDirectory>$root</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

$mainTriggers = @"
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$userName</UserId>
    </LogonTrigger>
    <BootTrigger>
      <Enabled>true</Enabled>
    </BootTrigger>
"@

$guardTriggers = @"
    <TimeTrigger>
      <StartBoundary>$startBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT1M</Interval>
        <Duration>P3650D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
"@

$mainXml = New-TaskXml -Description 'Hosts the Windows Gemini Workspaces Live MCP server on 127.0.0.1:8766.' `
    -Triggers $mainTriggers -Arguments ('//B //Nologo "{0}"' -f $launchScript)
$guardXml = New-TaskXml -Description 'Restarts the Gemini MCP service when nothing listens on 127.0.0.1:8766.' `
    -Triggers $guardTriggers -Arguments ('//B //Nologo "{0}"' -f $guardScript)

Register-ScheduledTask -TaskName $TaskName -Xml $mainXml -Force | Out-Null
Register-ScheduledTask -TaskName $GuardTaskName -Xml $guardXml -Force | Out-Null

$failures = @()
foreach ($name in @($TaskName, $GuardTaskName)) {
    $task = Get-ScheduledTask -TaskName $name
    $exec = $task.Actions[0]
    if ($exec.Execute -notlike '*wscript.exe') { $failures += "$name action is '$($exec.Execute)', expected wscript.exe" }
    if ($task.Settings.RestartCount -ne 0) { $failures += "$name RestartCount is $($task.Settings.RestartCount), expected 0" }
    if ($task.Settings.RestartInterval) { $failures += "$name RestartInterval is $($task.Settings.RestartInterval), expected empty" }
    $xml = Export-ScheduledTask -TaskName $name
    if ($xml -match 'RestartOnFailure') { $failures += "$name XML still contains RestartOnFailure" }
    "--- $name ---"
    "  Action    : $($exec.Execute) $($exec.Arguments)"
    "  Triggers  : $((@($task.Triggers | ForEach-Object { $_.CimClass.CimClassName })) -join ', ')"
    "  Repetition: $((@($task.Triggers | ForEach-Object { $_.Repetition.Interval } | Where-Object { $_ })) -join ', ')"
    "  Restart   : count=$($task.Settings.RestartCount) interval='$($task.Settings.RestartInterval)'"
    "  TimeLimit : $($task.Settings.ExecutionTimeLimit)  StartWhenAvailable=$($task.Settings.StartWhenAvailable)"
    "  State     : $($task.State)"
}

if ($failures.Count -gt 0) {
    "`nFAILED:"
    $failures | ForEach-Object { "  - $_" }
    exit 1
}
"`nAll checks passed."
