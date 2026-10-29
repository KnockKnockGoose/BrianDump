@echo off
rem Double-click to install BrianDump. It runs install.ps1 from this folder (a plain script you can read first).
rem Options pass through, for example:  "Install BrianDump.cmd" -All -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
