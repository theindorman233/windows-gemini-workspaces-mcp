' Invisible launcher for the Windows Gemini Workspaces MCP watchdog.
' wscript.exe is a GUI-subsystem host: no console window appears.
Dim fso, shell, scriptDir, guardScript, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
guardScript = fso.BuildPath(scriptDir, "guard_live_task.ps1")
cmd = """C:\Program Files\PowerShell\7\pwsh.exe"" -NoProfile -NonInteractive -WindowStyle Hidden -File """ & guardScript & """"
shell.Run cmd, 0, True