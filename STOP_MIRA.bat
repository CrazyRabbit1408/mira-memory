@echo off
chcp 65001 >nul
REM ASCII-only wrapper. Russian messages live in stop-mira.ps1 (UTF-8).
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-mira.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%