' Copyright (c) iGroup. All rights reserved.
' @author Kai @date 2026/10/26 11:00
' @description 兼容旧快捷入口，仅启动含看板的单 EXE。
Option Explicit
Dim shell, files, folder
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
shell.Run Chr(34) & folder & "\AttendanceHelper.exe" & Chr(34), 0, False
