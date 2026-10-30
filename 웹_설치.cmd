@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run 설치.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements-web.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -B scripts\check_web.py
if errorlevel 1 goto fail
echo Web setup complete. For screenshot recognition, also run OCR_설치.cmd.
pause
exit /b 0
:fail
echo Web setup failed. Check the error above.
pause
exit /b 1
