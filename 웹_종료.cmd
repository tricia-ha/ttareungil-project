@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%cd%\scripts\stop_web.ps1" -ProjectRoot "%cd%"
pause
