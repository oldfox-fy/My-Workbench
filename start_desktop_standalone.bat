@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ============================================================
echo    My Workbench Standalone — 离线桌面版（免登录）
echo ============================================================
echo.

:: ============================================================
::  1. 检查 Python
:: ============================================================
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.12+
    pause
    exit /b 1
)

:: ============================================================
::  2. 激活虚拟环境（如果存在）
:: ============================================================
if exist ".venv\Scripts\activate.bat" (
    echo [1/3] 激活 Python 虚拟环境...
    call .venv\Scripts\activate.bat
) else (
    echo [1/3] 使用系统 Python（未检测到 .venv）
)

:: ============================================================
::  3. 检查后端依赖
:: ============================================================
echo [2/3] 检查后端依赖...
pip show fastapi >nul 2>&1
if %errorlevel% neq 0 (
    echo        正在安装后端依赖...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [警告] 部分依赖安装失败，尝试继续启动...
    )
)

:: ============================================================
::  4. 确保前端已构建（Standalone 需要静态文件）
:: ============================================================
if not exist "frontend\dist\index.html" (
    echo [3/3] 前端未构建，检查 Node.js...
    node --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo [错误] Standalone 模式需要前端构建产物 frontend\dist\
        echo        请先安装 Node.js 并运行: cd frontend ^&^& npm install ^&^& npm run build
        echo        或使用 start_desktop.bat 以开发模式启动
        pause
        exit /b 1
    )
    echo        正在构建前端（首次可能需要几分钟）...
    cd frontend
    if not exist "node_modules" (
        call npm install
    )
    call npm run build
    if %errorlevel% neq 0 (
        echo [错误] 前端构建失败
        cd ..
        pause
        exit /b 1
    )
    cd ..
) else (
    echo [3/3] 前端已构建，跳过
)

:: ============================================================
::  5. 启动
:: ============================================================
echo.
echo   ┌────────────────────────────────────────┐
echo   │  模式 : Standalone 离线单用户          │
echo   │  免登录，所有数据本地存储              │
echo   │  桌面窗口将自动打开                    │
echo   │  关闭桌面窗口即退出程序                │
echo   └────────────────────────────────────────┘
echo.

python main_standalone.py

echo.
echo My Workbench 已退出。
exit /b 0
