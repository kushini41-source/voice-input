"""詞庫自動學習的卡片，兩種：
- 詢問：「你把 A 改成 B，要加入詞庫嗎？」（加入詞庫／修改…／不要）
- 已學會：有把握的已經自動加進詞庫了，只告訴你一聲（好／復原）

跟回答卡片一樣不會搶走輸入焦點，放在螢幕右下角（不會擋到浮動小視窗和回答卡片）。
一次只顯示一張，其他的排隊；放著不管會自己收起來（詢問卡片當作「之後再說」，不算拒絕）。
"""

import ctypes
import logging

from .answer_card import BG, BTN, BTN_HOVER, FG, SUB
from .overlay import (GWL_EXSTYLE, HWND_TOPMOST, MONITOR_DEFAULTTOPRIMARY, MONITORINFO, SWP_NOACTIVATE,
                      SWP_SHOWWINDOW, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, user32)
from .tkui import FONT

log = logging.getLogger(__name__)

ACCENT = "#2e7d32"
ACCENT_HOVER = "#388e3c"
HIGHLIGHT = "#ffd666"
AUTO_CLOSE_MS = 30_000
LEARNED_CLOSE_MS = 12_000   # 「已學會」只是告知，短一點

_card = None
_pending = []      # [(suggestion, callbacks)]


def suggest(ui, suggestion, on_accept, on_modify, on_decline):
    """必須在 UIThread 裡呼叫。已經有卡片在顯示就排隊，同一組詞不重複排。"""
    if _card is not None and getattr(_card, "suggestion_key", None) == suggestion.key:
        return
    if any(s.key == suggestion.key for s, _ in _pending):
        return
    _pending.append((suggestion, (on_accept, on_modify, on_decline)))
    if _card is None:
        _show_next(ui)


def _close(ui):
    global _card
    if _card is not None:
        try:
            _card.destroy()
        except Exception:
            pass
        _card = None
    if _pending:
        ui.root.after(400, lambda: _show_next(ui))


def _show_next(ui):
    if _card is not None or not _pending:
        return
    suggestion, callbacks = _pending.pop(0)
    try:
        _show(ui, suggestion, *callbacks)
    except Exception:
        log.exception("詞庫學習卡片顯示失敗")


def _show(ui, s, on_accept, on_modify, on_decline):
    global _card
    import tkinter as tk

    px = ui.px
    win = tk.Toplevel(ui.root)
    _card = win
    win.suggestion_key = s.key
    win.withdraw()
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg=BG, highlightthickness=1, highlightbackground="#3a3f47")
    width = px(380)

    learned = getattr(s, "auto", False)     # 已經自動加進詞庫了：只顯示結果，給「復原」按鈕
    header = tk.Frame(win, bg=BG)
    header.pack(fill="x", padx=px(14), pady=(px(10), px(4)))
    tk.Label(header, text="✓ 已學會" if learned else "✎ 加入詞庫？", bg=BG, fg=FG,
             font=(FONT, 11, "bold")).pack(side="left")
    close_btn = tk.Label(header, text="✕", bg=BG, fg=SUB, font=(FONT, 11), cursor="hand2")
    close_btn.pack(side="right")
    close_btn.bind("<Button-1>", lambda e: _close(ui))

    if learned:
        why = "你剛剛改過，以後會自動改對" if s.source == "edit" else f"AI 已經 {s.count} 次把它改對，以後直接改"
    else:
        why = f"AI 已經第 {s.count} 次把它改對了" if s.source == "ai" else "你剛剛自己改掉了"
    tk.Label(win, text=why, bg=BG, fg=SUB, font=(FONT, 9), anchor="w").pack(fill="x", padx=px(14))

    pair = tk.Frame(win, bg=BG)
    pair.pack(fill="x", padx=px(14), pady=(px(6), px(2)))
    tk.Label(pair, text=s.wrong, bg=BG, fg=SUB, font=(FONT, 13, "overstrike")).pack(side="left")
    tk.Label(pair, text="  →  ", bg=BG, fg=SUB, font=(FONT, 12)).pack(side="left")
    tk.Label(pair, text=s.right, bg=BG, fg=HIGHLIGHT, font=(FONT, 13, "bold")).pack(side="left")

    hint = (f"聽到「{s.wrong}」會直接打成「{s.right}」。學錯了就按「復原」。" if learned else
            f"加入後，聽到「{s.wrong}」或唸起來很像的，都會直接打成「{s.right}」。")
    tk.Label(win, text=hint, bg=BG, fg=SUB, font=(FONT, 9), anchor="w", justify="left",
             wraplength=width - px(28)).pack(fill="x", padx=px(14), pady=(px(4), px(8)))

    def make_button(parent, text, command, bg=BTN, hover=BTN_HOVER):
        b = tk.Label(parent, text=text, bg=bg, fg=FG, font=(FONT, 10), padx=px(12), pady=px(4), cursor="hand2")
        b.bind("<Button-1>", lambda e: command())
        b.bind("<Enter>", lambda e: b.configure(bg=hover))
        b.bind("<Leave>", lambda e: b.configure(bg=bg))
        return b

    def accept():
        _close(ui)
        on_accept(s)

    def modify():
        _close(ui)
        on_modify(s)

    def decline():
        _close(ui)
        on_decline(s)

    buttons = tk.Frame(win, bg=BG)
    buttons.pack(fill="x", padx=px(14), pady=(0, px(12)))
    if learned:
        make_button(buttons, "好", lambda: _close(ui), ACCENT, ACCENT_HOVER).pack(side="right")
        make_button(buttons, "復原", decline).pack(side="right", padx=(0, px(8)))
    else:
        make_button(buttons, "不要", decline).pack(side="right")
        make_button(buttons, "修改…", modify).pack(side="right", padx=(0, px(8)))
        make_button(buttons, "加入詞庫", accept, ACCENT, ACCENT_HOVER).pack(side="right", padx=(0, px(8)))

    win.update_idletasks()
    height = win.winfo_reqheight()
    win.geometry(f"{width}x{height}+0+0")
    win.update_idletasks()

    # 放在目前螢幕的右下角
    hwnd = int(win.wm_frame(), 16)
    mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), MONITOR_DEFAULTTOPRIMARY)
    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        w = info.rcWork
        x, y = w.right - width - px(16), w.bottom - height - px(16)
    else:
        x, y = px(200), px(200)
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)
    # 視窗是直接叫 Windows 顯示的（tkinter 以為它還藏著，win.geometry 搬位置不會生效），位置也要直接叫 Windows 設定
    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def auto_close():
        if _card is win:
            _close(ui)
    win.after(LEARNED_CLOSE_MS if learned else AUTO_CLOSE_MS, auto_close)
    return win
