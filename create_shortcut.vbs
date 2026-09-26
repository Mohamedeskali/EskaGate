Set WshShell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
desktop = WshShell.SpecialFolders("Desktop")
lnkPath = desktop & "\API Test Console.lnk"
Set lnk = WshShell.CreateShortcut(lnkPath)
lnk.TargetPath = fs.GetParentFolderName(WScript.ScriptFullName) & "\Run API Dashboard.bat"
lnk.WorkingDirectory = fs.GetParentFolderName(WScript.ScriptFullName)
lnk.IconLocation = fs.GetParentFolderName(WScript.ScriptFullName) & "\assets\api_dashboard_icon.ico, 0"
lnk.Description = "Run the local API dashboard and open it in the browser"
lnk.Save
MsgBox "Shortcut created on desktop:" & vbCrLf & lnkPath, vbInformation, "API Test Console"
