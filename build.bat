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
echo [1/6] Installing Python dependencies...
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
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm not found. Install from https://nodejs.org
    pause
    exit /b 1
)

echo [2/6] Installing root JS dependencies ^(electron-builder, bots^)...
call npm install
if errorlevel 1 (
    echo [ERROR] Root npm install failed.
    pause
    exit /b 1
)

REM ---- 3. Static dashboard build -------------------------------------------
echo [3/6] Installing dashboard dependencies...
pushd dashboard
call npm install
if errorlevel 1 (
    echo [ERROR] npm install failed in dashboard.
    popd
    pause
    exit /b 1
)
echo [4/6] Building static dashboard...
call npm run build
if errorlevel 1 (
    echo [ERROR] Dashboard build failed.
    popd
    pause
    exit /b 1
)
popd

REM ---- 4. Verify Python backend compiles ------------------------------------
echo   Verifying Python backend...
python scripts\verify_python.py
if errorlevel 1 (
    echo [WARN] verify_python.py reported issues - see above.
)

REM ---- 5. Bundle embedded Python runtime -----------------------------------
echo [5/6] Bundling embedded Python runtime for the installer...
python scripts\bundle_python.py
if errorlevel 1 (
    echo [ERROR] Python bundling failed - see errors above.
    pause
    exit /b 1
)

REM ---- 6. Electron packaging ------------------------------------------------
echo [6/6] Packaging Windows installer + portable exe...
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
echo   SELF-CONTAINED: Python 3.14.7 and all core
echo   dependencies are bundled - no Python install
echo   needed on the target PC.
echo.
echo   Optional extras ^(install on the target PC^):
echo   - Local vision: torch + transformers ^(~400 MB, model
echo     downloads on first use^)
echo   - Browser automation: pip install playwright ^&^&
echo     playwright install chromium
echo  ==========================================
echo.
pause
