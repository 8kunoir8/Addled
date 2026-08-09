@echo off
echo ============================================
echo   Addled — Development Launcher
echo ============================================
echo.

echo [1/3] Starting Python backend...
start "Addled Backend" cmd /k "cd /d %~dp0..\backend && python main.py"

echo Waiting for backend to initialize...
timeout /t 3 /nobreak >nul

echo [2/3] Starting Next.js dashboard...
start "Addled Dashboard" cmd /k "cd /d %~dp0..\dashboard && npm run dev"

echo Waiting for dashboard...
timeout /t 5 /nobreak >nul

echo [3/3] Starting Electron shell...
start "Addled Electron" cmd /k "cd /d %~dp0.. && npx electron ."

echo.
echo All processes started! Check the three terminal windows.
echo Close them or press Ctrl+C in each to stop.
echo.
pause
