@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "OCR_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "OCR_PYTHON=%cd%\.venv\Scripts\python.exe"
"%OCR_PYTHON%" -m pip install -r requirements-ocr.txt
if errorlevel 1 goto fail
"%OCR_PYTHON%" scripts\prepare_ocr.py
if errorlevel 1 goto fail
echo OCR setup complete.
pause
exit /b 0
:fail
echo OCR setup failed. Read the error above and use the project Python environment.
pause
exit /b 1
