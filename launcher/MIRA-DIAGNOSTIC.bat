@echo off
setlocal
pushd "%~dp0" >nul 2>&1
call "%CD%\CALL-MIRA.bat" diagnostic
echo.
pause
popd
exit /b %ERRORLEVEL%