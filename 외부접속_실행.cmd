@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "WEB_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "WEB_PYTHON=%cd%\.venv\Scripts\python.exe"
"%WEB_PYTHON%" -B "%cd%\scripts\public_tunnel.py"
if errorlevel 1 pause
