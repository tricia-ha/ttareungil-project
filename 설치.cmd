@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  call :create_environment
  if errorlevel 1 goto fail
)
.venv\Scripts\python.exe scripts\install_project.py
if errorlevel 1 goto fail
echo Setup complete. Open notebook launcher next.
pause
exit /b 0
:fail
echo Setup failed. Read the error above before starting the notebook.
echo If Python is missing, install Python 3.11 x64 and retry.
echo If the project was moved, recreate its .venv in the new location.
pause
exit /b 1
:create_environment
py -3.11 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>&1
if not errorlevel 1 (
  py -3.11 scripts\create_environment.py
  exit /b
)
python -c "import sys; assert sys.version_info >= (3,11)" >nul 2>&1
if not errorlevel 1 (
  python scripts\create_environment.py
  exit /b
)
py -3 -c "import sys; assert sys.version_info >= (3,11)" >nul 2>&1
if not errorlevel 1 (
  py -3 scripts\create_environment.py
  exit /b
)
echo No usable Python installation found. Install Python 3.11 x64 with PATH enabled.
exit /b 1
