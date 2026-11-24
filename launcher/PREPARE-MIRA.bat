@echo off
setlocal
pushd "%~dp0" >nul 2>&1
call "%CD%\CALL-MIRA.bat" prepare
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo Подготовка завершена.
) else (
  echo Подготовка не завершена.
)
pause
popd
exit /b %RESULT%