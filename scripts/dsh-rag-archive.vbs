' DSH-RAG archive watcher - silent autostart at user logon.
' Installed to %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
' Uninstall: delete this file from that folder.
' __PROJECT_DIR__ 是占位符，安装脚本会替换成实际仓库路径。
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "__PROJECT_DIR__"
sh.Run """__PROJECT_DIR__\.venv\Scripts\pythonw.exe"" ""__PROJECT_DIR__\scripts\watch_archive.py""", 0, False
