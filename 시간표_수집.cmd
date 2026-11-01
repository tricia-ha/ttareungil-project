@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 먼저 설치.cmd를 실행해 주세요.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B scripts\collect_map_timetables.py --compile %*
pause
