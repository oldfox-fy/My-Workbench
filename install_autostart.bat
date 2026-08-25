@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================================
echo    My Workbench — 注册开机自启动
echo ============================================================
echo.

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0autostart.py" install
) else (
    python "%~dp0autostart.py" install
)

echo.
pause
