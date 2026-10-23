"""熱鍵偵測：用 GetAsyncKeyState 輪詢，不裝任何鍵盤 hook。

只是每隔十幾毫秒「讀一下」按鍵狀態，不攔截、不吃掉任何按鍵，
所以不可能讓 Windows 的鍵盤輸入卡死。
"""

import logging
import threading
import time

from . import winapi
from .config import ConfigError

log = logging.getLogger(__name__)

KEY_NAMES = {
    "capslock": 0x14, "caps_lock": 0x14, "caps lock": 0x14,
    "right_alt": 0xA5, "left_alt": 0xA4,
    "right_ctrl": 0xA3, "left_ctrl": 0xA2,
    "right_shift": 0xA1, "left_shift": 0xA0,
    "mouse_x1": 0x05, "mouse_x2": 0x06,
    "scroll_lock": 0x91, "pause": 0x13, "insert": 0x2D,
}
KEY_NAMES.update({f"f{i}": 0x6F + i for i in range(1, 25)})  # F1 = 0x70 … F24 = 0x87

_ALT_VKS = {0xA4, 0xA5, winapi.VK_MENU}
_MODIFIER_VKS = {"shift": winapi.VK_SHIFT, "ctrl": winapi.VK_CONTROL, "control": winapi.VK_CONTROL}
# 翻譯沒有自己的鍵時，指令鍵每按一下就換下一個模式
_CYCLE = {"dictate": "command", "command": "translate", "translate": "dictate"}
_MOUSE_VKS = (0x01, 0x02, 0x04, 0x05, 0x06)   # 左、右、中鍵、側鍵 X1、X2

# 給使用者看的按鍵名稱
DISPLAY_NAMES = {
    0x14: "CapsLock", 0xA3: "右 Ctrl", 0xA2: "左 Ctrl", 0xA5: "右 Alt", 0xA4: "左 Alt",
    0xA1: "右 Shift", 0xA0: "左 Shift", 0x05: "滑鼠側鍵 X1", 0x06: "滑鼠側鍵 X2",
}


def resolve_key(name):
    """把設定檔裡的按鍵名稱轉成虛擬按鍵碼；不認得就回傳 None。"""
    key = str(name).strip().lower().replace("-", "_")
    if key.startswith("0x"):
        try:
            return int(key, 16)
        except ValueError:
            return None
    return KEY_NAMES.get(key) or KEY_NAMES.get(key.replace(" ", "_"))


class HotkeyPoller:
    """在背景執行緒輪詢熱鍵，透過回呼通知主程式。

    回呼都在輪詢執行緒裡被呼叫，必須立刻返回（耗時的事請丟到別的執行緒）。
      on_start()      ：開始錄音
      on_mode(mode)   ：錄音中偵測到換了模式（錄音中按了 Shift／Ctrl）
      on_stop(mode)   ：結束錄音，開始辨識；mode = "dictate"（聽寫）、"command"（指令）、"translate"（翻譯）
      on_abort()  ：短按或被取消，丟掉這段錄音
      on_escape() ：按了 Esc
      is_busy()   ：目前是否正在錄音或處理（決定要不要偵測 Esc）
    """

    def __init__(self, cfg, on_start, on_stop, on_abort, on_escape, is_busy, on_mode=None):
        self.vk = resolve_key(cfg["key"])
        if self.vk is None:
            raise ConfigError(
                f"設定檔裡的熱鍵「{cfg['key']}」看不懂，請改成 capslock、right_alt、f13、mouse_x2 這類名稱。"
            )
        self.key_name = str(cfg["key"])
        self.mode = cfg["mode"] if cfg["mode"] in ("hold", "toggle") else "hold"
        self.min_hold = float(cfg["min_hold_seconds"])
        self.interval = max(5, int(cfg["poll_interval_ms"])) / 1000.0
        self.esc_cancels = bool(cfg["esc_cancels"])
        self.max_record = float(cfg.get("max_record_seconds", 120) or 0)
        # 按一下模式：按著超過這個秒數才放開，不算「按一下」（例如按著 Ctrl 在做別的事）
        self.max_tap = float(cfg.get("max_tap_seconds", 1.0))
        self.display_name = DISPLAY_NAMES.get(self.vk, self.key_name)
        self.on_start = on_start
        self.on_mode = on_mode or (lambda mode: None)
        # 錄音中按一下哪個修飾鍵會切換成哪個模式。修飾鍵單獨按不會打出字，也不會動到選取的文字。
        # 翻譯鍵不能用（例如熱鍵本身就是右 Ctrl）時：指令鍵按第一下＝指令、第二下＝翻譯、第三下＝回到聽寫
        command_vk = self._modifier(cfg.get("command_modifier", "shift"))
        translate_vk = self._modifier(cfg.get("translate_modifier", "ctrl"))
        if translate_vk == command_vk:
            translate_vk = None
        self.mode_keys = []      # [(按鍵碼, 模式)]
        if command_vk is not None:
            self.mode_keys.append((command_vk, "command"))
        if translate_vk is not None:
            self.mode_keys.append((translate_vk, "translate"))
        self.cycle_vk = command_vk if translate_vk is None else None
        self.on_stop = on_stop
        self.on_abort = on_abort
        self.on_escape = on_escape
        self.is_busy = is_busy
        self._thread = None
        self._running = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="hotkey", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _modifier(self, name):
        """設定檔裡的修飾鍵名稱 → 按鍵碼；看不懂、或跟熱鍵衝突就回傳 None。"""
        vk = _MODIFIER_VKS.get(str(name or "").lower())
        return vk if vk is not None and not self._conflicts(vk) else None

    @property
    def translate_hint(self):
        """給使用者看的「怎麼切到翻譯模式」。"""
        for vk, mode in self.mode_keys:
            if mode == "translate":
                return "錄音中按一下 Ctrl" if vk == winapi.VK_CONTROL else "錄音中按一下 Shift"
        return "錄音中按兩下 Shift" if self.cycle_vk is not None else ""

    def _conflicts(self, modifier_vk):
        """熱鍵本身就是這個修飾鍵（或右 Alt 會被 Windows 當成 Ctrl+Alt）時，不能拿它來切換模式。"""
        if modifier_vk == winapi.VK_SHIFT and self.vk in (0xA0, 0xA1):
            return True
        if modifier_vk == winapi.VK_CONTROL and self.vk in (0xA2, 0xA3, 0xA5):
            return True
        return False

    def _chord_key_down(self):
        """熱鍵按著的時候，有沒有同時按了別的鍵或滑鼠（代表是快捷鍵，不是要錄音）。"""
        if winapi.other_key_down(self.vk):
            return True
        if self.vk not in _MOUSE_VKS:
            return any(winapi.is_key_down(vk) for vk in _MOUSE_VKS)
        return False

    def _next_mode(self, current, vk):
        """錄音中按下了修飾鍵 vk，接下來是哪個模式。"""
        if vk == self.cycle_vk:
            return _CYCLE.get(current, "dictate")
        for key, mode in self.mode_keys:
            if key == vk:
                return mode
        return current

    # ── 內部 ──

    def _restore_capslock(self, state_before):
        """長按 CapsLock 錄音時，Windows 已經切換了大小寫，這裡把它切回來。"""
        if self.vk != winapi.VK_CAPITAL:
            return
        if winapi.capslock_on() != state_before:
            winapi.tap_key(winapi.VK_CAPITAL)
            # 等我們自己送出的 CapsLock 放開，避免被當成下一次按下
            deadline = time.monotonic() + 0.3
            while winapi.is_key_down(winapi.VK_CAPITAL) and time.monotonic() < deadline:
                time.sleep(0.005)

    def _mask_alt_menu(self):
        """單獨按放 Alt 會讓視窗的選單列取得焦點；按下時補送一個沒用途的按鍵就能避免。"""
        if self.vk in _ALT_VKS:
            winapi.tap_key(winapi.VK_MENU_MASK)

    def _loop(self):
        log.info("熱鍵偵測已啟動：%s（%s 模式）", self.key_name, "按住說話" if self.mode == "hold" else "按一下開始／結束")
        pressed = False
        press_time = 0.0
        caps_before = False
        caps_idle = False
        toggled_on = False   # toggle 模式：目前是否在錄音
        rec_mode = "dictate"
        rec_started = 0.0
        chord = False
        esc_was_down = False
        mode_was_down = {}   # 修飾鍵上一輪是不是按著（只算「剛按下去」那一下）

        while self._running:
            try:
                down = winapi.is_key_down(self.vk)
                now = time.monotonic()
                if not down and not pressed and self.vk == winapi.VK_CAPITAL:
                    caps_idle = winapi.capslock_on()

                if down and not pressed:
                    pressed = True
                    press_time = now
                    chord = False
                    # 用「上一輪按鍵還沒按下時」記下的大小寫狀態，當作按之前的狀態
                    caps_before = caps_idle
                    self._mask_alt_menu()
                    if self.mode == "hold":
                        rec_mode = "dictate"
                        mode_was_down = {}
                        self.on_start()

                elif down and pressed and not chord and self._chord_key_down():
                    # 熱鍵跟別的鍵一起按（例如右 Ctrl+C、Ctrl+滑鼠點擊）＝一般快捷鍵，不是要錄音
                    chord = True
                    if self.mode == "hold":
                        self.on_abort(reason="chord")

                elif not down and pressed:
                    pressed = False
                    held = now - press_time
                    if self.mode == "hold":
                        if chord:
                            pass
                        elif held < self.min_hold:
                            # 短按：當作一般按鍵，CapsLock 照常切換大小寫
                            self.on_abort(reason="short")
                        else:
                            self._restore_capslock(caps_before)
                            self.on_stop(rec_mode)
                    elif chord or held > self.max_tap:
                        # 按一下模式：組合鍵、或按太久（例如按著 Ctrl 做別的事），都不算
                        pass
                    else:
                        # 按一下模式：放開的那一刻才開始／結束，才分得出是不是組合鍵
                        self._restore_capslock(caps_before)
                        if toggled_on:
                            toggled_on = False
                            self.on_stop(rec_mode)
                        else:
                            toggled_on = True
                            rec_mode = "dictate"
                            mode_was_down = {}
                            rec_started = now
                            self.on_start()

                # 按一下模式忘了按結束：錄太久就自動結束並辨識，下一次按鍵會是「重新開始」
                if toggled_on and not pressed and self.max_record and now - rec_started > self.max_record:
                    log.info("錄音超過 %.0f 秒，自動結束", self.max_record)
                    toggled_on = False
                    self.on_stop(rec_mode)

                # 錄音中按了 Shift／Ctrl：換模式（每按下去一次算一次，按著不放不會一直換）
                if pressed or toggled_on:
                    for vk, _mode in self.mode_keys:
                        is_down = winapi.is_key_down(vk)
                        # 錄音剛開始的第一輪只記狀態：開始前就按著的不算
                        if is_down and not mode_was_down.get(vk, True):
                            new_mode = self._next_mode(rec_mode, vk)
                            if new_mode != rec_mode:
                                rec_mode = new_mode
                                self.on_mode(rec_mode)
                        mode_was_down[vk] = is_down

                if self.esc_cancels:
                    esc_down = winapi.is_key_down(winapi.VK_ESCAPE)
                    if esc_down and not esc_was_down and self.is_busy():
                        toggled_on = False
                        self.on_escape()
                    esc_was_down = esc_down
            except Exception:
                log.exception("熱鍵偵測發生錯誤，0.5 秒後繼續")
                time.sleep(0.5)

            time.sleep(self.interval)
