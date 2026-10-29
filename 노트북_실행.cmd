@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run setup first: install Python 3.11 x64, then run the setup launcher.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m jupyterlab notebooks\01_기능실행.ipynb
pause
