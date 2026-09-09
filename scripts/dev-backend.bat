@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem ============================================
rem  Backend dev server (hot reload) - run in backend/
rem ============================================
cd /d "%~dp0..\backend"

if not exist ".venv\Scripts\python.exe" (
  echo [0/2] First run: creating venv and installing deps...
  python -m venv .venv || (echo [ERROR] Failed to create venv, please install Python 3.8+ & exit /b 1)
  .venv\Scripts\pip install -r requirements.txt -q || exit /b 1
)

echo [1/2] Installing / updating deps...
.venv\Scripts\pip install -r requirements.txt -q || exit /b 1

echo [2/2] Starting uvicorn (reload) http://localhost:8000 ...
.venv\Scripts\uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
