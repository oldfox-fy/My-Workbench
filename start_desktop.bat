@echo off
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0start_desktop.py"
) else (
    python "%~dp0start_desktop.py"
)

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Start failed, check errors above
    pause
)
