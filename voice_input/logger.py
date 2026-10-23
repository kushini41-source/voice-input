"""日誌：寫到 logs\\voice_input.log，每天午夜換一個新檔，保留 7 天。"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from .paths import LOG_DIR, LOG_PATH


class _StreamToLogger:
    """用 pythonw 執行時沒有終端機，sys.stdout／stderr 是 None，
    第三方套件一 print 就可能出錯，所以把它們導到日誌裡。"""

    def __init__(self, logger, level):
        self._logger = logger
        self._level = level

    def write(self, message):
        message = message.rstrip()
        if message:
            self._logger.log(self._level, message)

    def flush(self):
        pass

    def isatty(self):
        return False


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = TimedRotatingFileHandler(
        LOG_PATH, when="midnight", backupCount=7, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s：%(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    # 有終端機時同時印在螢幕上，方便測試
    if sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)():
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        root.addHandler(console)
    else:
        sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO)
        sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.WARNING)

    # 這些套件的日誌太囉嗦
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "huggingface_hub", "PIL", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
