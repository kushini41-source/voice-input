@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title 語音輸入 - 一鍵安裝

echo.
echo ============================================
echo   語音輸入 一鍵安裝
echo   會自動完成：建立獨立環境、安裝套件、
echo   準備顯示卡加速、下載辨識模型
echo   第一次大約需要 10 ~ 30 分鐘（看網路速度）
echo ============================================
echo.

rem ---- 0. 如果語音輸入正在執行，先請使用者關掉 ----
tasklist /FI "IMAGENAME eq pythonw.exe" /V 2>nul | find /I "pythonw.exe" >nul
if not errorlevel 1 (
    echo [提醒] 如果語音輸入正在執行，請先在右下角系統匣的麥克風圖示按右鍵 →「結束」，
    echo        再按任意鍵繼續安裝。
    pause >nul
)

rem ---- 1. 找 Python ----
set "PY="
if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not defined PY (
    py -3 --version >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [錯誤] 這台電腦還沒有安裝 Python。
    echo        即將打開 Python 官網，請下載安裝，安裝時記得勾選「Add Python to PATH」，
    echo        裝好之後再雙擊一次 install.bat。
    start "" https://www.python.org/downloads/windows/
    goto :fail
)
echo [1/6] 找到 Python：%PY%

rem ---- 2. 建立獨立環境（.venv），不會影響電腦上其他 Python 程式 ----
rem 從別台電腦複製過來的 .venv 記的是那台電腦的 Python 位置，在這台通常不能用：測試一下，不能用就重建
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo [2/6] 原本的獨立環境 .venv 在這台電腦不能用（可能是從別台電腦複製來的），刪掉重建……
        rmdir /s /q ".venv"
    )
)
if not exist ".venv\Scripts\python.exe" (
    echo [2/6] 建立獨立環境 .venv ……
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [錯誤] 建立獨立環境失敗。請把這個視窗的內容截圖給開發者。
        goto :fail
    )
) else (
    echo [2/6] 獨立環境 .venv 已經存在，略過
)
set "VPY=%~dp0.venv\Scripts\python.exe"

rem ---- 3. 安裝套件 ----
echo [3/6] 安裝需要的套件（第一次會下載約 1GB，請耐心等候）……
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
"%VPY%" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo [錯誤] 套件安裝失敗。常見原因是網路中斷，請確認網路後再雙擊一次 install.bat。
    goto :fail
)

rem ---- 4. 顯示卡加速 ----
echo [4/6] 準備顯示卡加速……
"%VPY%" -X utf8 tools\install_helper.py cuda

rem ---- 5. 下載辨識模型 ----
echo [5/6] 下載辨識模型……
"%VPY%" -X utf8 tools\install_helper.py model
if errorlevel 1 goto :fail

rem ---- 6. 檢查 ----
echo [6/6] 檢查電腦環境……
"%VPY%" -X utf8 tools\install_helper.py check

echo.
echo ============================================
echo   安裝完成！
echo ============================================
echo.
choice /C YN /M "要在桌面建立「語音輸入」捷徑嗎"
if not errorlevel 2 "%VPY%" -X utf8 tools\install_helper.py shortcut
choice /C YN /M "要設定開機自動啟動嗎"
if not errorlevel 2 "%VPY%" -X utf8 tools\install_helper.py autostart
choice /C YN /M "要現在啟動語音輸入嗎"
if not errorlevel 2 (
    start "" wscript.exe "%~dp0启动语音输入.vbs"
    echo 已啟動，大約 10 秒後右下角會出現藍色麥克風圖示。按一下右邊的 Ctrl 開始說話，說完再按一下。
)
echo.
echo 按任意鍵關閉這個視窗。
pause >nul
exit /b 0

:fail
echo.
echo 安裝沒有完成。按任意鍵關閉這個視窗。
pause >nul
exit /b 1
