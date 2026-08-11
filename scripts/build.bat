@echo off
REM ============================================
REM  Addled — Full Build Pipeline
REM  Produces: dist/Addled Setup x.y.z.exe
REM ============================================
cd /d "%~dp0.."

echo.
echo  === Addled Build Pipeline ===
echo.

REM ── 0. Check dependencies ──────────────────────────────────
echo [1/5] Checking dependencies...

where node >nul 2>&1 || (echo ERROR: Node.js not found & exit /b 1)
where npm >nul 2>&1 || (echo ERROR: npm not found & exit /b 1)
where python >nul 2>&1 || (echo ERROR: Python not found & exit /b 1)

node --version
call npm --version
python --version
echo   All dependencies found.

REM ── 1. Install JS deps ─────────────────────────────────────
echo.
echo [2/5] Installing Node.js dependencies...

cd dashboard
call npm install 2>&1
if %ERRORLEVEL% neq 0 (echo ERROR: npm install dashboard failed & exit /b 1)
echo   Dashboard deps installed.

cd ..\bots
call npm install 2>&1
if %ERRORLEVEL% neq 0 (echo WARNING: bot deps install had issues)
echo   Bot bridge deps installed.

cd ..

REM ── 2. Build Next.js dashboard ─────────────────────────────
echo.
echo [3/5] Building Next.js dashboard...

cd dashboard
call npx next build 2>&1
if %ERRORLEVEL% neq 0 (echo ERROR: Next.js build failed & exit /b 1)
echo   Dashboard built.

cd ..

REM ── 3. Verify Python backend ───────────────────────────────
echo.
echo [4/5] Verifying Python backend...

python scripts\verify_python.py 2>&1
if %ERRORLEVEL% neq 0 (echo ERROR: Python syntax errors found & exit /b 1)
echo   All Python files compile cleanly.

REM ── 4. Package with electron-builder ───────────────────────
echo.
echo [5/5] Building Windows installer...

REM Install electron-builder if missing
call npx electron-builder --version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo   Installing electron-builder...
    call npm install --save-dev electron-builder 2>&1
)

call npx electron-builder --win --x64 2>&1
if %ERRORLEVEL% neq 0 (echo ERROR: electron-builder failed & exit /b 1)

REM ── Done ───────────────────────────────────────────────────
echo.
echo  === Build Complete ===
echo.
echo  Output: dist\
dir /b dist\*.exe 2>nul
echo.
echo  Files are ready for distribution.
echo  Run: dist\Addled Setup *.exe  to install
echo.
