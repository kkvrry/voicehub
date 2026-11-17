' VoiceHub - start the tray build from source, without a console window.
'
' Double-click this file. It replaces start.bat for daily use:
'   - runs .venv\Scripts\pythonw.exe (no console subsystem, no black window)
'   - loads packaging\voicehub_launcher.py (single instance + tray + autostart)
'   - keeps data in <repo>\data, same as start.bat
'
' Everything is resolved relative to this file, so the folder can be moved or
' copied to another machine. That machine needs the venv first:
'   uv sync --group package
'
' Start.bat is still the debugging entry: it shows the console and logs.

Option Explicit

Dim fso, sh, root, exe, launcher, dataDir, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
exe = root & "\.venv\Scripts\pythonw.exe"
launcher = root & "\packaging\voicehub_launcher.py"
dataDir = root & "\data"

If Not fso.FileExists(exe) Then
    MsgBox "Missing .venv\Scripts\pythonw.exe" & vbCrLf & vbCrLf & _
           "Run in the project root:" & vbCrLf & "    uv sync --group package", _
           vbExclamation, "VoiceHub"
    WScript.Quit 1
End If

If Not fso.FileExists(launcher) Then
    MsgBox "Missing packaging\voicehub_launcher.py" & vbCrLf & vbCrLf & _
           "The checkout looks incomplete.", vbCritical, "VoiceHub"
    WScript.Quit 1
End If

If Not fso.FolderExists(dataDir) Then
    fso.CreateFolder dataDir
End If

sh.CurrentDirectory = root
cmd = """" & exe & """ """ & launcher & """ --data-dir """ & dataDir & """"

On Error Resume Next
sh.Run cmd, 0, False
If Err.Number <> 0 Then
    MsgBox "Failed to start VoiceHub:" & vbCrLf & Err.Description, vbCritical, "VoiceHub"
    WScript.Quit 1
End If
On Error GoTo 0
