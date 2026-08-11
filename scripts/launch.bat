@echo off
REM ============================================
REM  Addled — One-Click Launcher
REM  Starts Python backend + Electron app
REM ============================================
cd /d "%~dp0"

echo.
echo  === Addled ===
echo  Starting backend then dashboard...
echo.

REM Find the backend directory
set BACKEND_DIR=resources\backend
if exist "%BACKEND_DIR%\main.py" (
    echo Backend found: %BACKEND_DIR%
) else if exist "..\backend\main.py" (
    set BACKEND_DIR=..\backend
    echo Backend found: ..\backend
) else if exist "backend\main.py" (
    set BACKEND_DIR=backend
    echo Backend found: backend
) else (
    echo ERROR: Cannot find backend/main.py
    echo Looked in: resources\backend, ..\backend, backend
    pause
    exit /b 1
)

REM Check Python
where python >nul 2>&1 || (
    echo ERROR: Python not found. Install Python 3.11+ from https://python.org
    pause
    exit /b 1
)

REM Start Python backend in a new window
echo Starting Python backend...
start "Addled Backend" cmd /k "cd /d %~dp0\%BACKEND_DIR% && echo Addled Backend && python main.py"

REM Wait for backend to start
echo Waiting for backend to start...
timeout /t 3 /nobreak >nul

REM Start Electron app
echo Starting Addled...
start "" "%~dp0Addled.exe"

echo.
echo Addled is running!
echo The dashboard will appear in the app window.
echo Close this window or keep it open to see backend logs.
echo.
pause
