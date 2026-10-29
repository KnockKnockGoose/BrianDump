@echo off
rem Double-click to remove BrianDump from Windows. It runs uninstall.ps1 from this folder (a plain script you can read first).
rem Your dumps, reports and agenda in Documents\BrianDump are never deleted.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
echo.
pause
