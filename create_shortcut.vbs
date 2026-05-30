Set oWS = WScript.CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sLinkFile = scriptDir & "\一键启动WebUI.lnk"
Set oLink = oWS.CreateShortcut(sLinkFile)
oLink.TargetPath = scriptDir & "\一键启动WebUI.bat"
oLink.WorkingDirectory = scriptDir
oLink.Description = "一键启动 DingtalkChatbot 并打开 WebUI"
oLink.IconLocation = oWS.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\SHELL32.dll,220"
oLink.Save
