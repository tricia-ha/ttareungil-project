@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "CHECK_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "CHECK_PYTHON=%cd%\.venv\Scripts\python.exe"
if "%~1"=="--clean" (
  "%CHECK_PYTHON%" -B scripts\check_project_clean.py
) else (
  "%CHECK_PYTHON%" -B scripts\check_project.py --dependencies
)
set "CHECK_RESULT=%errorlevel%"
if not "%CHECK_RESULT%"=="0" (
  echo Project check failed. Check the listed local files.
) else (
  echo Project check complete.
)
pause
exit /b %CHECK_RESULT%
