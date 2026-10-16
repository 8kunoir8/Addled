@echo off
REM ============================================
REM  Addled - One-Click Build Pipeline
REM  Builds the static dashboard + Windows
REM  installer + portable exe into dist\
REM ============================================
setlocal
cd /d "%~dp0"

REM One source of truth for the version the summary prints and asserts on.
for /f "usebackq delims=" %%v in (`node -p "require('./package.json').version"`) do set "APPVER=%%v"
if not defined APPVER (
    echo [ERROR] could not read the version from package.json.
    pause
    exit /b 1
)

echo.
echo  === Addled Build Pipeline ===
echo.

REM ---- 1. Python + backend dependencies ------------------------------------
REM `where python` only proves a path exists. On a machine that has never had
REM Python installed, the first match is often the 0-byte Microsoft Store
REM execution alias, which exists and prints "Python was not found" (exit 49)
REM when run -- so the guard passed and every python call after it failed.
REM Ask the interpreter to run instead.
python -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] A working Python 3.11+ is required. `python` did not run.
    echo         If this opened the Microsoft Store, turn off the App execution
    echo         alias for python.exe, or install Python from https://python.org
    pause
    exit /b 1
)
REM Step 1 was `pip install -r requirements.txt`, which installed 2.4 GB
REM (torch, transformers, playwright) into this machine's user site-packages.
REM No build step reads it: bundle_python.py installs requirements-core.txt into
REM the embedded interpreter. It is a dev-setup command, not a build step:
REM     pip install -r requirements.txt

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

echo [1/6] Installing root JS dependencies ^(electron-builder, bots^)...
call npm install
if errorlevel 1 (
    echo [ERROR] Root npm install failed.
    pause
    exit /b 1
)

REM ---- 3. Static dashboard build -------------------------------------------
REM The dashboard's dependencies arrive via the root `npm install` above:
REM package.json's postinstall does `cd dashboard && npm install`, and npm runs
REM postinstall on every install, including a no-op re-run. Repeating it here
REM was a second full install of the same tree.
pushd dashboard
echo [2/6] Building static dashboard...
call npm run build
if errorlevel 1 (
    echo [ERROR] Dashboard build failed.
    popd
    pause
    exit /b 1
)
popd
REM ---- 4. Verify Python backend compiles ------------------------------------
echo [3/6] Verifying Python backend...
python scripts\verify_python.py
if errorlevel 1 (
    echo [ERROR] Backend Python did not compile - see above.
    pause
    exit /b 1
)

REM ---- 5. Bundle embedded Python runtime -----------------------------------
echo [4/6] Bundling embedded Python runtime for the installer...
python scripts\bundle_python.py
if errorlevel 1 (
    echo [ERROR] Python bundling failed - see errors above.
    pause
    exit /b 1
)

REM ---- 5b. Run the check suites -------------------------------------------
REM This gate was previously absent from the build, which is how a broken
REM installer script reached a release while check_packaging.py sat green.
REM It runs after the dashboard build -- check_guide.py asserts the BUILT
REM dashboard carries the version, and skips rather than fails when
REM dashboard/out is missing -- and after the bundle, so a model that did not
REM get fetched is caught. A suite that cannot run on this machine exits 2 and
REM is reported as skipped, not as a failure.
echo [5/6] Running the check suites...
.\python-bundle\python.exe -s scripts\check_all.py
if errorlevel 1 (
    echo [ERROR] Check suites failed - the build will not produce an installer.
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

if not exist "dist\Addled-%APPVER%-x64.exe" (
    echo [ERROR] dist\Addled-%APPVER%-x64.exe was not produced.
    pause
    exit /b 1
)
if not exist "dist\Addled-%APPVER%-portable.exe" (
    echo [ERROR] dist\Addled-%APPVER%-portable.exe was not produced.
    pause
    exit /b 1
)

echo.
echo  ==========================================
echo   BUILD COMPLETE
echo.
echo   dist\Addled-%APPVER%-x64.exe      ^(NSIS installer^)
echo   dist\Addled-%APPVER%-portable.exe ^(portable, no install^)
echo.
echo   SELF-CONTAINED: Python 3.14.7 and all core
echo   dependencies are bundled - no Python install
echo   needed on the target PC.
echo.
echo   Optional extras ^(install on the target PC^):
echo   - Local vision: torch + transformers + einops + timm
echo     ^(~400 MB, model downloads on first use^). Install from
echo     Settings - Providers - Local AI; the list lives in
echo     requirements-vision.txt.
echo   - Browser automation: pip install playwright ^&^&
echo     playwright install chromium
echo  ==========================================
echo.
pause
