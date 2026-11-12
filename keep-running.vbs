' Keeps Zoomollo running in the background, with no window.
' A shortcut-style copy of this file sits in the Windows Startup folder, so it starts when you log in.
' If Zoomollo ever stops (a crash, or an update restarting it), it is started again within half a minute.
' To stop it for good: delete "Zoomollo.vbs" from the Startup folder, then end "wscript.exe" and
' "zoomollo.exe" in Task Manager (or just restart the PC after deleting the file).
Set sh = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = folder
Do
  sh.Run """" & folder & "\.venv\Scripts\zoomollo.exe"" serve", 0, True
  WScript.Sleep 30000
Loop
