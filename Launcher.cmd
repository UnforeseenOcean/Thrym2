@echo off
setlocal enabledelayedexpansion

:: Run the PowerShell checker and capture its exit code
:: Exit code 0 = Python OK and deps installed, 1 = something failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0check_env.ps1"
set PS_EXIT=%ERRORLEVEL%

if %PS_EXIT% neq 0 (
    echo.
    echo [ERROR] Setup did not complete successfully.
    echo         Please read the message above and fix the issue, then run this again.
    echo.
    pause
    exit /b 1
)

echo.
echo [OK] Environment ready. Starting bot...
echo.

:: Find the python executable (same logic as PS script, but we just launch it)
for /f "delims=" %%P in ('powershell.exe -NoProfile -Command "& { $py = Get-Command python -ErrorAction SilentlyContinue; if ($py) { $py.Source } }" 2^>nul') do set PYTHON_EXE=%%P

if not defined PYTHON_EXE (
    echo [ERROR] Could not locate python.exe to launch the bot.
    pause
    exit /b 1
)

:: Pass any arguments straight through (e.g. --debug, --verbose, --config)
python "%~dp0thrym2.py" %*

if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Bot exited with an error (code %ERRORLEVEL%).
    pause
)
endlocal