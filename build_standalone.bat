@echo off
setlocal enabledelayedexpansion

echo.
echo ========================================================
echo   My Workbench Standalone - Build Script
echo ========================================================
echo.

:: ---- Check Python ----
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.12+
    pause
    exit /b 1
)

:: ---- Check Node.js ----
node --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Node.js not found, skipping frontend build
) else (
    :: ---- Install frontend deps ----
    echo [1/4] Installing frontend dependencies...
    cd frontend
    call npm install
    if %errorlevel% neq 0 (
        echo [ERROR] npm install failed
        pause
        exit /b 1
    )
    cd ..

    :: ---- Build frontend ----
    echo [2/4] Building frontend...
    cd frontend
    call npm run build
    if %errorlevel% neq 0 (
        echo [ERROR] Frontend build failed
        pause
        exit /b 1
    )
    cd ..
)

:: ---- Install Python dependencies ----
echo [3/5] Installing Python dependencies (from requirements.txt)...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [WARN] Some Python packages failed - attempting individual installs...
    pip install Pillow pytesseract pdf2image jieba
)

:: ---- Check frontend dist ----
if not exist "frontend\dist\index.html" (
    echo [ERROR] frontend\dist\index.html not found. Build frontend first.
    pause
    exit /b 1
)

:: ---- Install PyInstaller ----
echo [4/5] Checking PyInstaller...
pip show pyinstaller >nul 2>&1
if %errorlevel% neq 0 (
    pip install pyinstaller
)

:: ---- Build ----
echo [5/5] Building exe...
echo.
echo   Output: dist\MyWorkbench.exe
echo   Mode: single-file, no console window
echo   Time: 3-10 minutes (longer for first build)
echo.

pyinstaller build_standalone.spec --clean --noconfirm

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Build failed. Check errors above.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   BUILD SUCCESS
echo.
echo   Output: dist\MyWorkbench.exe
echo.
echo   Usage:
echo     1. Copy dist\MyWorkbench.exe to any folder
echo     2. Double-click to run
echo     3. On first run, a data/ folder will be created
echo        next to the exe (db, uploads, logs etc.)
echo.
echo   Notes:
echo     - Target PC needs Windows 10+
echo     - First startup takes 5-10s for backend init
echo     - Users need to configure their own LLM API keys
echo ========================================================
echo.

pause
