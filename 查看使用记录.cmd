@echo off
setlocal
cd /d "%~dp0"
set "PYTHON_EXE=%~dp0runtime\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    where python.exe >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python executable not found. Please install Python or run setup first.
        pause
        exit /b 1
    )
    set "PYTHON_EXE=python.exe"
)

echo [Stardew AI Companion] Collecting Token Usage Records...
"%PYTHON_EXE%" "%~dp0tools\view-usage.py" --html "%~dp0artifacts\reports\usage-report.html" %*
set "usageExit=%errorlevel%"
if exist "%~dp0artifacts\reports\usage-report.html" (
    start "" "%~dp0artifacts\reports\usage-report.html"
)
if "%usageExit%"=="0" (
    echo.
    echo [OK] Usage report generated at: artifacts\reports\usage-report.html
    echo Opened interactive dashboard in your default browser.
    pause
    exit /b 0
)
echo.
echo [ERROR] Failed to generate token usage report.
pause
exit /b %usageExit%
