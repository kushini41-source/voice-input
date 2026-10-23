"""開機自動啟動：在 Windows 的「啟動」資料夾放一個小啟動檔，指向程式資料夾裡的 启动语音输入.vbs。

拿掉那個檔案就不會自動啟動。系統匣選單的「開機自動啟動」就是在切換這個檔案。
"""

import logging
import os

from .paths import BASE_DIR

log = logging.getLogger(__name__)

LAUNCHER_NAME = "启动语音输入.vbs"
STARTUP_FILE_NAME = "語音輸入（自動啟動）.vbs"


def startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def startup_file():
    return os.path.join(startup_dir(), STARTUP_FILE_NAME)


def is_enabled():
    return os.path.isfile(startup_file())


def enable():
    launcher = os.path.join(BASE_DIR, LAUNCHER_NAME)
    content = (
        "' 語音輸入：開機自動啟動用。刪掉這個檔案就不會自動啟動。\r\n"
        f"' 程式資料夾：{BASE_DIR}\r\n"
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run """{launcher}""", 0, False\r\n'
    )
    os.makedirs(startup_dir(), exist_ok=True)
    # VBScript 讀中文路徑需要 UTF-16（含 BOM）
    with open(startup_file(), "w", encoding="utf-16", newline="") as f:
        f.write(content)
    log.info("已開啟開機自動啟動：%s", startup_file())


def disable():
    try:
        os.remove(startup_file())
        log.info("已關閉開機自動啟動")
    except FileNotFoundError:
        pass


def toggle():
    if is_enabled():
        disable()
    else:
        enable()
    return is_enabled()
