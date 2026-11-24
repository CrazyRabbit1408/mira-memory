@echo off
REM ASCII-only wrapper. Logic lives in run-mira-win.ps1 (UTF-8).
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-mira-win.ps1"
exit /b %ERRORLEVEL%