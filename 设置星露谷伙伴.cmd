@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0tools\setup-companion.ps1" %*
set "setupExit=%errorlevel%"
if "%setupExit%"=="0" exit /b 0
echo Setup could not finish. Please see the message above.
pause
exit /b %setupExit%
