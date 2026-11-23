@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run prepare.ps1. Python 3.12 x64 is required.
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -m drivemods_bot doctor --online
pause

