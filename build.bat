@echo off
REM ============================================
REM  Addled - One-Click Build Pipeline
REM  Builds the static dashboard + Windows
REM  installer + portable exe into dist\
REM ============================================
setlocal
cd /d "%~dp0"

echo.
echo  === Addled Build Pipeline ===
echo.

REM ---- 1. Python + backend dependencies ------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11+ from https://python.org
    pause
    exit /b 1
)
echo [1/4] Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo [WARN] Some Python packages failed to install - see errors above.
)

REM ---- 2. Node.js -----------------------------------------------------------
where node >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found. Install from https://nodejs.org
    pause
    exit /b 1
)

REM ---- 3. Static dashboard build -------------------------------------------
echo [2/4] Installing dashboard dependencies...
pushd dashboard
call npm install
if errorlevel 1 (
    echo [ERROR] npm install failed in dashboard.
    popd
    pause
    exit /b 1
)
echo [3/4] Building static dashboard...
call npm run build
if errorlevel 1 (
    echo [ERROR] Dashboard build failed.
    popd
    pause
    exit /b 1
)
popd

REM ---- 4. Electron packaging ------------------------------------------------
echo [4/4] Packaging Windows installer + portable exe...
call npm run build:win
if errorlevel 1 (
    echo [ERROR] electron-builder failed - see errors above.
    pause
    exit /b 1
)

echo.
echo  ==========================================
echo   BUILD COMPLETE
echo.
echo   dist\Addled-1.0.0-x64.exe      ^(NSIS installer^)
echo   dist\Addled-1.0.0-portable.exe ^(portable, no install^)
echo.
echo   IMPORTANT: the app uses system Python.
echo   On the target PC, install Python 3.11+ and run:
echo       pip install -r requirements.txt
echo  ==========================================
echo.
pause
