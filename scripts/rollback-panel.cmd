@echo off
REM Rollback dsh-rag-panel from the desktop profile.
REM Double-click this file if the DSH UI fails to start after installing the panel.
setlocal
set PROFILE=%USERPROFILE%\.dsh\profiles\desktop

if exist "%PROFILE%\node_modules\dsh-rag-panel" (
  rmdir "%PROFILE%\node_modules\dsh-rag-panel"
  echo Removed junction.
) else (
  echo No junction found.
)

REM Restore the newest package.json backup, if any.
set BAK=
for %%f in ("%PROFILE%\package.json.bak-*") do set BAK=%%~ff
if defined BAK (
  copy /Y "%BAK%" "%PROFILE%\package.json" >nul
  echo Restored package.json from %BAK%
) else (
  echo No package.json backup found; restore it manually.
)

echo.
echo Done. Please restart DeepSeek Harness.
pause
