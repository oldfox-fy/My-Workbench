@echo off
echo ============================================================
echo    TEST — 如果你看到这行，说明 bat 文件能正常执行
echo ============================================================
echo.
echo 当前目录: %CD%
echo 脚本目录: %~dp0
echo.

:: 测试 Python 是否存在
"%~dp0.venv\Scripts\python.exe" --version 2>&1
echo Python exit code: %errorlevel%
echo.

:: 测试 start_desktop.py 是否存在
if exist "%~dp0start_desktop.py" (
    echo start_desktop.py 存在
) else (
    echo [错误] start_desktop.py 不存在！
)
echo.

:: 测试能否 import
"%~dp0.venv\Scripts\python.exe" -c "print('Python print OK'); import sys; sys.stdout.write('stdout OK\n')" 2>&1
echo import test exit code: %errorlevel%
echo.

pause
