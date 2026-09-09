@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem ============================================
rem  Reset + reseed: static process rules + random WIP data
rem  Usage: scripts\seed.bat  [--products 80]
rem ============================================
cd /d "%~dp0..\backend"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] venv not found. Run scripts\setup.bat first
  pause
  exit /b 1
)

echo Rebuilding demo data...
.venv\Scripts\python -m app.seed --reset %*
pause
