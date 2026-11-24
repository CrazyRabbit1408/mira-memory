@echo off
chcp 65001 >nul
REM ASCII-only wrapper. Russian messages live in start-mira.ps1 (UTF-8).
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-mira.ps1"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo.
  pause
)
exit /b %RESULT%