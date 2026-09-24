@echo off
chcp 65001 >nul
rem Thin wrapper - all logic lives in tools\client\sim_local.py.
rem Keep this file ASCII-only: cmd parses .bat with the ANSI codepage (936),
rem so UTF-8 Chinese comments get mangled into bogus commands.
set "PY=%~dp0..\backend\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] backend\.venv not found. Run scripts\setup.bat first.
  exit /b 1
)
"%PY%" "%~dp0..\tools\client\sim_local.py" %*
exit /b %errorlevel%
