"""指令模式的回答卡片：顯示 AI 的回答，可以複製、插入到游標位置、或關掉。

卡片不會搶走輸入焦點（跟浮動小視窗一樣用「不啟用」的視窗樣式），
所以使用者原本的游標還在原處，按「插入」就能直接打進去；滑鼠點卡片上的按鈕照常有效。
"""

import ctypes
import logging

from .overlay import (GWL_EXSTYLE, HWND_TOPMOST, MONITOR_DEFAULTTOPRIMARY, MONITORINFO, SWP_NOACTIVATE,
                      SWP_SHOWWINDOW, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, user32)
from .tkui import FONT

log = logging.getLogger(__name__)

BG = "#1f2329"
FG = "#f2f3f5"
SUB = "#9aa0a6"
BTN = "#2f343c"
BTN_HOVER = "#3d434d"
ACCENT = "#4c8dff"

_card = None   # 同一時間只顯示一張


def close_card():
    global _card
    if _card is not None:
        try:
            _card.destroy()
        except Exception:
            pass
        _card = None


def show_answer_card(ui, instruction, answer, on_insert=None):
    """必須在 UIThread 裡呼叫。on_insert(text)：按「插入」時呼叫（由主程式負責打字）。"""
    global _card
    import tkinter as tk

    close_card()
    px = ui.px
    win = tk.Toplevel(ui.root)
    _card = win
    win.withdraw()
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=BG, highlightthickness=1, highlightbackground="#3a3f47")

    width = px(520)
    header = tk.Frame(win, bg=BG)
    header.pack(fill="x", padx=px(14), pady=(px(10), px(4)))
    tk.Label(header, text="✨ AI 回答", bg=BG, fg=FG, font=(FONT, 11, "bold")).pack(side="left")
    close_btn = tk.Label(header, text="✕", bg=BG, fg=SUB, font=(FONT, 11), cursor="hand2")
    close_btn.pack(side="right")
    close_btn.bind("<Button-1>", lambda e: close_card())

    tk.Label(win, text=f"你說：{instruction}", bg=BG, fg=SUB, font=(FONT, 9), anchor="w", justify="left",
             wraplength=width - px(28)).pack(fill="x", padx=px(14))

    lines = max(3, min(14, sum(len(l) // 34 + 1 for l in answer.split("\n"))))
    body = tk.Text(win, bg=BG, fg=FG, font=(FONT, 11), wrap="word", height=lines, relief="flat",
                   bd=0, highlightthickness=0, padx=0, pady=px(6), insertbackground=FG)
    body.insert("1.0", answer)
    body.configure(state="disabled")
    body.pack(fill="both", expand=True, padx=px(14), pady=(px(4), px(6)))

    status = tk.Label(win, text="", bg=BG, fg=SUB, font=(FONT, 9))

    def make_button(parent, text, command):
        b = tk.Label(parent, text=text, bg=BTN, fg=FG, font=(FONT, 10), padx=px(12), pady=px(4), cursor="hand2")
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.configure(bg=BTN_HOVER))
        b.bind("<Leave>", lambda e: b.configure(bg=BTN))
        return b

    def copy():
        win.clipboard_clear()
        win.clipboard_append(answer)
        status.configure(text="已複製")

    def insert():
        if on_insert:
            on_insert(answer)
        close_card()

    buttons = tk.Frame(win, bg=BG)
    buttons.pack(fill="x", padx=px(14), pady=(0, px(12)))
    status.pack(in_=buttons, side="left")
    make_button(buttons, "關閉", close_card).pack(side="right")
    make_button(buttons, "插入到游標位置", insert).pack(side="right", padx=(0, px(8)))
    make_button(buttons, "複製", copy).pack(side="right", padx=(0, px(8)))

    win.update_idletasks()
    height = win.winfo_reqheight()
    win.geometry(f"{width}x{height}+0+0")
    win.update_idletasks()

    # 放在目前螢幕的下方中央（浮動小視窗的上面）
    hwnd = int(win.wm_frame(), 16)
    mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTOPRIMARY)
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        w = info.rcWork
        x = w.left + (w.right - w.left - width) // 2
        y = w.bottom - height - px(150)
    else:
        x, y = px(200), px(200)
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
    # 視窗是直接叫 Windows 顯示的（tkinter 以為它還藏著，win.geometry 搬位置不會生效），位置也要直接叫 Windows 設定
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)
    return win
