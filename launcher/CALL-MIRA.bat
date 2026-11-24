@echo off
setlocal EnableExtensions
chcp 65001 >nul

pushd "%~dp0" >nul 2>&1
if errorlevel 1 (
  echo Не удалось открыть папку запускатора.
  exit /b 1
)

set "MODE=%~1"
if not defined MODE set "MODE=start-detached"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%CD%\invoke-wsl.ps1" -Mode "%MODE%"
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="9009" (
  echo.
  echo Не найден PowerShell или wsl.exe. Проверьте установку WSL.
)
if not "%RESULT%"=="0" (
  echo.
  echo Команда WSL завершилась с кодом %RESULT%.
)
popd
exit /b %RESULT%