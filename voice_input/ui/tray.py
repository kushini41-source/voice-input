"""系統匣圖示：顯示狀態（載入中／待機／錄音中／處理中／錯誤）、右鍵選單、跳通知。"""

import logging
import os
import subprocess

import pystray
from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

STATE_COLORS = {
    "loading": (150, 150, 150),
    "idle": (60, 120, 220),
    "recording": (220, 50, 50),
    "processing": (235, 170, 30),
    "error": (120, 60, 60),
}
STATE_LABELS = {
    "loading": "載入中…",
    "idle": "待機中",
    "recording": "錄音中",
    "processing": "處理中",
    "error": "發生問題",
}


def _make_icon(color):
    """畫一個圓形底＋白色麥克風的小圖示。"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=color)
    white = (255, 255, 255)
    d.rounded_rectangle((24, 12, 40, 38), radius=8, fill=white)      # 麥克風頭
    d.arc((17, 22, 47, 46), start=0, end=180, fill=white, width=4)   # 支架弧線
    d.line((32, 46, 32, 52), fill=white, width=4)                    # 立柱
    d.line((24, 53, 40, 53), fill=white, width=4)                    # 底座
    return img


class Tray:
    def __init__(self, hotkey_label, on_quit, files_to_open, toggles=(), actions=(), radios=()):
        """files_to_open：[(選單文字, 路徑), ...]，點了用記事本或檔案總管開啟。
        toggles：[(選單文字, 取得目前狀態的函式, 切換的函式), ...]，顯示成可以勾選的項目。
        actions：[(選單文字, 函式), ...]，點了就執行。
        radios：[(子選單文字, [(值, 顯示文字), ...], 取得目前值的函式, 選擇的函式), ...]，顯示成單選子選單。"""
        self._icons = {state: _make_icon(color) for state, color in STATE_COLORS.items()}
        self._state = "loading"
        self._detail = "正在載入辨識模型，第一次可能要一兩分鐘"
        self.hotkey_label = hotkey_label
        self._on_quit = on_quit

        items = [
            pystray.MenuItem(lambda _: self.status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        for label, options, getter, setter in radios:
            items.append(pystray.MenuItem(label, pystray.Menu(
                *[self._radio_item(value, text, getter, setter) for value, text in options]
            )))
        for label, fn in actions:
            items.append(self._action_item(label, fn))
        if actions:
            items.append(pystray.Menu.SEPARATOR)
        for label, getter, toggle in toggles:
            items.append(self._toggle_item(label, getter, toggle))
        if toggles:
            items.append(pystray.Menu.SEPARATOR)
        for label, path in files_to_open:
            items.append(pystray.MenuItem(label, self._opener(path)))
        items += [pystray.Menu.SEPARATOR, pystray.MenuItem("結束", self._quit)]

        self.icon = pystray.Icon("voice_input", self._icons["loading"], self._tooltip(), pystray.Menu(*items))

    @property
    def status_text(self):
        text = f"狀態：{STATE_LABELS.get(self._state, self._state)}"
        if self._detail:
            text += f"（{self._detail}）"
        return text

    def _tooltip(self):
        # Windows 的系統匣提示文字最多 127 字
        return f"語音輸入 - {self.status_text}"[:127]

    @staticmethod
    def _radio_item(value, text, getter, setter):
        def _on_click(icon, item):
            try:
                setter(value)
            except Exception:
                log.exception("選單「%s」執行失敗", text)

        def _is_checked(item):
            return getter() == value

        return pystray.MenuItem(text, _on_click, checked=_is_checked, radio=True)

    @staticmethod
    def _action_item(label, fn):
        def _on_click(icon, item):
            try:
                fn()
            except Exception:
                log.exception("選單「%s」執行失敗", label)

        return pystray.MenuItem(label, _on_click)

    @staticmethod
    def _toggle_item(label, getter, toggle):
        # pystray 會檢查回呼函式的參數個數，只接受 (icon, item) 這種形式，
        # 所以不能用帶預設參數的 lambda，改用閉包
        def _on_click(icon, item):
            toggle()

        def _is_checked(item):
            return bool(getter())

        return pystray.MenuItem(label, _on_click, checked=_is_checked)

    @staticmethod
    def _opener(path):
        def _open(icon, item):
            try:
                if os.path.isfile(path):
                    # 一律用記事本開：Windows 預設不知道 .yaml 要用什麼程式開，
                    # 會跳出「選擇應用程式」視窗，很容易開錯檔案
                    subprocess.Popen(["notepad.exe", path])
                else:
                    os.startfile(path)
            except Exception as e:
                log.error("開啟 %s 失敗：%s", path, e)
        return _open

    def _quit(self, icon, item):
        self._on_quit()

    def set_state(self, state, detail=""):
        self._state = state
        self._detail = detail
        try:
            self.icon.icon = self._icons.get(state, self._icons["idle"])
            self.icon.title = self._tooltip()
            self.icon.update_menu()
        except Exception:
            pass

    def update_menu(self):
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, title, message):
        log.info("通知：%s｜%s", title, message)
        try:
            self.icon.notify(message, title)
        except Exception as e:
            log.warning("跳通知失敗：%s", e)

    def run(self, setup):
        """在主執行緒跑系統匣（會一直卡在這裡直到結束）。setup 會在圖示出現後被呼叫。"""
        def _setup(icon):
            icon.visible = True
            setup()
        self.icon.run(setup=_setup)

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass
