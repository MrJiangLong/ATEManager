@echo off
chcp 65001 >nul
rem ============================================
rem  Frontend dev server (Vite + HMR) - run in frontend/
rem ============================================
cd /d "%~dp0..\frontend"

if not exist "node_modules" (
  echo [1/2] Installing deps...
  call npm install || exit /b 1
)

echo [2/2] Starting Vite http://localhost:5173 ...
call npm run dev
