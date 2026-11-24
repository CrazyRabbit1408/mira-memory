@echo off
setlocal
pushd "%~dp0" >nul 2>&1
call "%CD%\CALL-MIRA.bat" start-detached
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo.
  echo Запуск не удался. Нажмите любую клавишу, чтобы закрыть окно.
  pause >nul
)
popd
exit /b %RESULT%