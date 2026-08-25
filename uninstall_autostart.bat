@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ============================================================
echo    My Workbench — 取消开机自启动
echo ============================================================
echo.

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0autostart.py" uninstall
) else (
    python "%~dp0autostart.py" uninstall
)

echo.
pause
