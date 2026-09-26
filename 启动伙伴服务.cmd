@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo ================================================================
echo  Stardew AI Companion - Background Chat Service
echo ================================================================
echo  [Instructions]
echo  1. Start Stardew Valley with SMAPI and load your save.
echo  2. Press F8 in-game to open the Companion Chat Window.
echo  3. Type natural language commands in Chinese or English.
echo ================================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\start-companion.ps1" %*
set "exitCode=%errorlevel%"
if not "%exitCode%"=="0" (
    echo.
    echo [ERROR] Chat service exited with code %exitCode%.
    pause
)
exit /b %exitCode%
