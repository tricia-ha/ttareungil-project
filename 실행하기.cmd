@echo off
cd /d "%~dp0"
if not exist .venv (
  py -3.11 -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m uvicorn api:app --app-dir package --host 127.0.0.1 --port 8000
pause
