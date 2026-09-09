@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem ============================================
rem  Backend regression tests (SQLite in-memory of file db)
rem ============================================
cd /d "%~dp0..\backend"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] venv not found. Run scripts\setup.bat first
  pause
  exit /b 1
)

.venv\Scripts\python tests\test_backend.py
pause
