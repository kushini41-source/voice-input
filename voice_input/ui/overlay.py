"""浮動小視窗：錄音時在螢幕下方中央顯示「● 錄音中 00:03」和音量條，處理中顯示「整理中…」，完成後淡出。

絕對不能搶走輸入焦點（不然文字會貼到錯的地方），所以：
- 視窗加上 WS_EX_NOACTIVATE（不會被啟用）、WS_EX_TRANSPARENT（滑鼠點擊直接穿透）、
  WS_EX_TOOLWINDOW（不出現在工作列和 Alt+Tab）
- 只在啟動時用「顯示但不啟用」的方式顯示一次，之後一直留著，平常透明度是 0；
  要出現、消失都只是改透明度，搬位置也用「不啟用」的方式
"""

import ctypes
import logging
import time
from ctypes import wintypes

from .tkui import FONT

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.MonitorFromWindow.restype = wintypes.HANDLE
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTOPRIMARY = 1

KEY_COLOR = "#010203"   # 透明色：畫成這個顏色的地方會變成完全透明（用來做圓角）
BG = "#1f2329"
FG = "#ffffff"
RED = "#ff4d4f"
PURPLE = "#b07cff"
CYAN = "#36cfc9"
_KIND_COLORS = {"dictate": RED, "command": PURPLE, "translate": CYAN}
_KIND_LABELS = {"dictate": "錄音中", "command": "指令模式", "translate": "翻譯模式"}
AMBER = "#f5b041"
GREEN = "#52c41a"
GRAY = "#9aa0a6"
BAR_OFF = "#3a3f47"

OPACITY = 0.93
WIDTH, HEIGHT = 230, 44    # 設計尺寸（100% 縮放時的像素）
BOTTOM_MARGIN = 90         # 距離工作列上緣


class Overlay:
    """必須在 UIThread 裡建立和操作。"""

    def __init__(self, ui, level_fn=lambda: 0.0):
        import tkinter as tk
        self.ui = ui
        self.level_fn = level_fn
        self.w, self.h = ui.px(WIDTH), ui.px(HEIGHT)
        self.top = tk.Toplevel(ui.root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.configure(bg=KEY_COLOR)
        self.top.attributes("-transparentcolor", KEY_COLOR)
        self.top.attributes("-alpha", 0.0)
        self.top.attributes("-topmost", True)
        self.top.geometry(f"{self.w}x{self.h}+0+0")
        self.canvas = tk.Canvas(self.top, width=self.w, height=self.h, bg=KEY_COLOR,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        self.top.update_idletasks()

        self.hwnd = int(self.top.wm_frame(), 16)
        ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE,
                                 ex | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        # 重新套一次透明設定（改過視窗樣式後 Windows 可能需要重新套用）
        self.top.attributes("-transparentcolor", KEY_COLOR)
        self.top.attributes("-alpha", 0.0)

        self.mode = "hidden"
        self.started = 0.0
        self.text = ""
        self.color = FG
        self._alpha = 0.0
        self._tick_job = None
        self._fade_job = None

    # ── 對外（都在 UIThread 裡呼叫）──

    def show_recording(self):
        self.mode = "recording"
        self.rec_kind = "dictate"
        self.rec_label = None
        self.started = time.monotonic()
        self._appear()

    def set_record_kind(self, kind, label=None):
        """錄音中切換成指令模式／翻譯模式時，換文字和顏色。label：要顯示的文字（例如「翻成英文」）。"""
        self.rec_kind = kind
        self.rec_label = label
        if self.mode == "recording":
            self._draw()

    def show_processing(self, text="整理中…"):
        self.mode = "processing"
        self.text = text
        self._appear()

    def show_result(self, text, color=GREEN, hold=0.6):
        """顯示結果（例如「✓ 已輸入」「已取消」），停一下再淡出。"""
        if self.mode == "hidden" and self._alpha == 0:
            return
        self.mode = "result"
        self.text = text
        self.color = color
        self._draw()
        self._cancel_fade()
        self._fade_job = self.ui.root.after(int(hold * 1000), self._fade_out)

    def hide(self):
        self._fade_out()

    # ── 內部 ──

    def _move_to_active_monitor(self):
        mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTOPRIMARY)
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            work = info.rcWork
            x = work.left + (work.right - work.left - self.w) // 2
            y = work.bottom - self.h - self.ui.px(BOTTOM_MARGIN)
        else:
            x = (self.top.winfo_screenwidth() - self.w) // 2
            y = self.top.winfo_screenheight() - self.h - self.ui.px(BOTTOM_MARGIN + 40)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _appear(self):
        self._cancel_fade()
        if self._alpha < OPACITY:
            self._move_to_active_monitor()
            self._set_alpha(OPACITY)
        self._draw()
        if self._tick_job is None:
            self._tick()

    def _set_alpha(self, a):
        self._alpha = a
        self.top.attributes("-alpha", a)

    def _cancel_fade(self):
        if self._fade_job is not None:
            self.ui.root.after_cancel(self._fade_job)
            self._fade_job = None

    def _fade_out(self, step=0):
        self._fade_job = None
        steps = 6
        if step >= steps:
            self._set_alpha(0.0)
            self.mode = "hidden"
            return
        self._set_alpha(OPACITY * (1 - (step + 1) / steps))
        self._fade_job = self.ui.root.after(40, lambda: self._fade_out(step + 1))

    def _tick(self):
        if self.mode in ("recording", "processing"):
            self._draw()
            self._tick_job = self.ui.root.after(80, self._tick)
        else:
            self._tick_job = None

    def _round_rect(self, x1, y1, x2, y2, r, **kw):
        c = self.canvas
        c.create_oval(x1, y1, x1 + 2 * r, y2, **kw)
        c.create_oval(x2 - 2 * r, y1, x2, y2, **kw)
        c.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)

    def _draw(self):
        px = self.ui.px
        c = self.canvas
        c.delete("all")
        self._round_rect(0, 0, self.w - 1, self.h - 1, self.h // 2, fill=BG, outline=BG)
        cy = self.h // 2
        font = (FONT, 11, "bold")

        if self.mode == "recording":
            elapsed = time.monotonic() - self.started
            # 紅點一閃一閃
            if int(elapsed * 2) % 2 == 0:
                r = px(6)
                dot = _KIND_COLORS.get(getattr(self, "rec_kind", "dictate"), RED)
                c.create_oval(px(20) - r, cy - r, px(20) + r, cy + r, fill=dot, outline=dot)
            mm, ss = divmod(int(elapsed), 60)
            label = (getattr(self, "rec_label", None)
                     or _KIND_LABELS.get(getattr(self, "rec_kind", "dictate"), "錄音中"))
            c.create_text(px(36), cy, text=f"{label} {mm:02d}:{ss:02d}", fill=FG, font=font, anchor="w")
            self._draw_level_bars(px(150), cy)
        elif self.mode == "processing":
            phase = int(time.monotonic() * 4) % 3
            for i in range(3):
                r = px(4)
                x = px(16) + i * px(11)
                c.create_oval(x - r, cy - r, x + r, cy + r, fill=AMBER if i == phase else BAR_OFF, outline="")
            c.create_text(px(48), cy, text=self.text, fill=FG, font=font, anchor="w")
        else:
            c.create_text(self.w // 2, cy, text=self.text, fill=self.color, font=font)

    def _draw_level_bars(self, x0, cy):
        px = self.ui.px
        try:
            level = max(0.0, min(1.0, float(self.level_fn())))
        except Exception:
            level = 0.0
        n = 10
        lit = int(round(level * n))
        bar_w, gap = px(4), px(3)
        for i in range(n):
            # 中間高、兩邊低，看起來像音量波形
            h = px(6 + 14 * (1 - abs(i - (n - 1) / 2) / (n / 2)))
            x = x0 + i * (bar_w + gap)
            color = GREEN if i < lit else BAR_OFF
            self.canvas.create_rectangle(x, cy - h // 2, x + bar_w, cy + h // 2, fill=color, outline="")
