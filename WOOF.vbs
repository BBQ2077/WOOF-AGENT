' 雙擊開啟 WOOF：有打包好的 WOOF.exe 就用它（工作列顯示狗掌圖示），沒有就用開發模式
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
exe = root & "\release\WOOF-win32-x64\WOOF.exe"
If fso.FileExists(exe) Then
  sh.CurrentDirectory = fso.GetParentFolderName(exe)
  sh.Run """" & exe & """", 1, False
Else
  sh.CurrentDirectory = root & "\desktop"
  sh.Run """" & root & "\desktop\node_modules\electron\dist\electron.exe"" .", 1, False
End If
