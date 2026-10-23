@echo off
rem 測試用：會開一個視窗即時顯示辨識紀錄，關掉視窗就結束程式
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=py"
"%PY%" main.py
pause
