@echo off
setlocal
pushd "%~dp0" >nul 2>&1
call "%CD%\CALL-MIRA.bat" status
set "RESULT=%ERRORLEVEL%"
echo.
pause
popd
exit /b %RESULT%