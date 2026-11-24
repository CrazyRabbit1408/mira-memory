@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" >nul 2>&1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%CD%\install-shortcuts.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo Ярлыки созданы в папке «Mira Memory» на рабочем столе.
) else (
  echo Не удалось создать ярлыки.
)
pause
popd
exit /b %RESULT%