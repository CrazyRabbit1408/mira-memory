@echo off
setlocal
pushd "%~dp0" >nul 2>&1
call "%CD%\CALL-MIRA.bat" start-attached
set "RESULT=%ERRORLEVEL%"
echo.
echo Отладочный запуск завершён с кодом %RESULT%.
pause
popd
exit /b %RESULT%