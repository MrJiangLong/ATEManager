@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem ============================================
rem  One-shot init: create venv + install deps + seed data
rem ============================================
cd /d "%~dp0..\backend"

echo [1/3] Installing backend deps...
if not exist ".venv\Scripts\python.exe" (
  echo       First run: creating venv...
  python -m venv .venv || (echo [ERROR] Failed to create venv, please install Python 3.8+ & pause & exit /b 1)
)
.venv\Scripts\pip install -r requirements.txt -q || exit /b 1

echo [2/3] Checking config...
if not exist ".env" (
  echo       No .env found, copying from .env.example
  copy /y ".env.example" ".env" >nul
  echo       [HINT] Edit backend\.env to set DATABASE_URL / V1_API_KEY / JWT_SECRET
)

echo [3/3] Seeding static process rules + random WIP data (idempotent)...
.venv\Scripts\python -m app.seed

echo.
echo ============================================
echo  Done! Start the apps with:
echo    scripts\dev-backend.bat     (backend http://localhost:8000)
echo    scripts\dev-frontend.bat    (frontend http://localhost:5173)
echo    scripts\seed.bat            (reset + reseed demo data)
echo    scripts\test-backend.bat    (run backend regression tests)
echo ============================================
pause
