"""所有 tkinter 視窗（浮動小視窗、加入詞庫、最近紀錄）都在這條專屬執行緒裡跑。

tkinter 規定：視窗只能在建立它的那條執行緒裡操作。所以其他執行緒要動視窗，
一律用 UIThread.call(函式) 丟進佇列，由這條執行緒依序執行。
"""

import ctypes
import logging
import queue
import threading

log = logging.getLogger(__name__)

FONT = "Microsoft JhengHei UI"


class UIThread:
    def __init__(self):
        self._queue = queue.Queue()
        self._ready = threading.Event()
        self.root = None
        self.scale = 1.0
        self.available = True
        threading.Thread(target=self._run, name="tk-ui", daemon=True).start()
        self._ready.wait(10)

    def _run(self):
        try:
            import tkinter as tk
            user32 = ctypes.windll.user32
            previous = user32.GetForegroundWindow()
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.update_idletasks()
            # 建立 tkinter 主視窗的瞬間會搶走輸入焦點（即使馬上隱藏），把焦點還給原本的視窗，
            # 不然開機自動啟動時，使用者正在打字的視窗會突然沒反應
            if previous and user32.GetForegroundWindow() != previous:
                user32.SetForegroundWindow(previous)
            # 依螢幕 DPI 換算尺寸（150% 縮放的螢幕上，同樣的設計要畫大 1.5 倍）
            self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
            try:
                from tkinter import ttk
                ttk.Style(self.root).theme_use("vista")
            except Exception:
                pass
            try:
                # 所有視窗左上角都用跟系統匣一樣的麥克風圖示
                from PIL import ImageTk
                from .tray import STATE_COLORS, _make_icon
                self._icon = ImageTk.PhotoImage(_make_icon(STATE_COLORS["idle"]), master=self.root)
                self.root.iconphoto(True, self._icon)
            except Exception:
                pass
        except Exception:
            log.exception("無法建立視窗介面（浮動視窗與對話框將無法使用，不影響語音輸入）")
            self.available = False
            self._ready.set()
            return
        self._ready.set()
        self.root.after(30, self._poll)
        self.root.mainloop()

    def _poll(self):
        try:
            while True:
                fn = self._queue.get_nowait()
                try:
                    fn()
                except Exception:
                    log.exception("視窗介面執行時發生錯誤")
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    def call(self, fn):
        if self.available:
            self._queue.put(fn)

    def px(self, n):
        return int(round(n * self.scale))
