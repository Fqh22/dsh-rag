' DSH-RAG archive watcher - silent autostart at user logon.
' Installed to %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
' Uninstall: delete this file from that folder.
' Paths below are rewritten by install-autostart.ps1 at install time.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "__PROJECT_DIR__"
sh.Run """__PROJECT_DIR__\.venv\Scripts\pythonw.exe"" ""__PROJECT_DIR__\scripts\watch_archive.py""", 0, False
