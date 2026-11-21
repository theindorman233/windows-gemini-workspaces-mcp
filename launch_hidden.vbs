' Invisible launcher for the Windows Gemini Workspaces MCP service.
' wscript.exe is a GUI-subsystem host: it never allocates a console, so Task
' Scheduler running this file produces no console at all. The child pwsh is started
' with window style 0 (SW_HIDE), which keeps its own console hidden even when
' Windows Terminal is the system default terminal.
' -WindowStyle Hidden alone is not enough when Task Scheduler launches pwsh
' directly: Windows creates the console window first and the flag only hides it
' afterwards, which flashes a terminal on screen.
Option Explicit

Dim fso, shell, q, scriptDir, scriptPath, command, exitCode
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

q = Chr(34)
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(scriptDir, "start_live_task.ps1")
If Not fso.FileExists(scriptPath) Then
    WScript.Quit 1
End If

command = q & "C:\Program Files\PowerShell\7\pwsh.exe" & q & _
          " -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden" & _
          " -File " & q & scriptPath & q & " -Detach"

' Run(command, 0, True): hidden window, and wait so the task keeps a real exit code.
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
