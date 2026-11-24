@echo off
setlocal
pushd "%~dp0" >nul 2>&1
call "%CD%\CALL-MIRA.bat" open
set "RESULT=%ERRORLEVEL%"
popd
exit /b %RESULT%